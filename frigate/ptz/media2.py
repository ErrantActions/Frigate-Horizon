"""Explicit Media2 SOAP adapter for the SDK's missing Media2 WSDL binding.

Only encoder read/options/write operations are exposed. Unknown configuration
XML is preserved rather than dropped by an invented partial encoder schema.
"""

import copy
import uuid

import httpx
from lxml import etree
from onvif.client import UsernameDigestTokenDtDiff

NAMESPACE = "http://www.onvif.org/ver20/media/wsdl"
SCHEMA = "http://www.onvif.org/ver10/schema"
SOAP = "http://www.w3.org/2003/05/soap-envelope"


def _decode(element):
    if not len(element):
        text = element.text or ""
        try:
            return float(text) if "." in text else int(text)
        except ValueError:
            return text
    result = dict(element.attrib)
    for child in element:
        key = etree.QName(child).localname
        value = _decode(child)
        if key == "ResolutionsAvailable":
            result.setdefault(key, []).append(value)
        else:
            result[key] = value
    return result


class Media2Service:
    """Use discovered Media2 endpoints only, with bounded, non-retrying requests."""

    def __init__(self, xaddr, connection, camera):
        self.xaddr = xaddr
        self._token = UsernameDigestTokenDtDiff(
            connection.username,
            connection.password.get_secret_value(),
            use_digest=camera.encrypt,
            dt_diff=camera.dt_diff,
        )
        self._client = httpx.AsyncClient(
            verify=not connection.tls_insecure,
            timeout=10,
            auth=httpx.DigestAuth(
                connection.username,
                connection.password.get_secret_value(),
            )
            if connection.auth_type == "digest"
            else None,
        )

    async def close(self):
        """Close the adapter's sole HTTP client."""
        await self._client.aclose()

    def _envelope(self, name):
        envelope = etree.Element(
            f"{{{SOAP}}}Envelope", nsmap={"s": SOAP, "tr2": NAMESPACE, "tt": SCHEMA}
        )
        addressing = "http://www.w3.org/2005/08/addressing"
        header = etree.SubElement(envelope, f"{{{SOAP}}}Header")
        for key, value in (
            ("Action", f"{NAMESPACE}/{name}"),
            ("MessageID", f"urn:uuid:{uuid.uuid4()}"),
            ("To", self.xaddr),
        ):
            etree.SubElement(header, f"{{{addressing}}}{key}").text = value
        body = etree.SubElement(envelope, f"{{{SOAP}}}Body")
        return envelope, etree.SubElement(body, f"{{{NAMESPACE}}}{name}")

    async def _request(self, name, parameters):
        envelope, operation = self._envelope(name)
        for key, value in parameters.items():
            child = etree.SubElement(operation, f"{{{NAMESPACE}}}{key}")
            child.text = str(value)
        return await self._send(name, envelope)

    async def _send(self, name, envelope):
        self._token.apply(envelope, {})
        size = 0
        chunks = []
        async with self._client.stream(
            "POST",
            self.xaddr,
            content=etree.tostring(envelope),
            headers={
                "Content-Type": f'application/soap+xml; charset=utf-8; action="{NAMESPACE}/{name}"'
            },
        ) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 2_000_000:
                    raise ValueError("Media2 response exceeds the supported size")
                chunks.append(chunk)
        root = etree.fromstring(
            b"".join(chunks),
            etree.XMLParser(resolve_entities=False, no_network=True),
        )
        if root.find(f".//{{{SOAP}}}Fault") is not None:
            raise ValueError("The camera rejected the Media2 operation")
        result = root.find(f"{{{SOAP}}}Body/{{{NAMESPACE}}}{name}Response")
        if result is None:
            raise ValueError("The camera returned an invalid Media2 response")
        return result

    async def GetProfiles(self):
        """Read reported Media2 profiles and their explicit configurations."""
        response = await self._request("GetProfiles", {"Type": "All"})
        return [
            _decode(child)
            for child in response
            if etree.QName(child).localname == "Profiles"
        ]

    async def GetVideoEncoderConfigurations(self, parameters):
        """Keep the exact original configuration for a minimal, lossless update."""
        response = await self._request("GetVideoEncoderConfigurations", parameters)
        result = []
        for child in response:
            if etree.QName(child).localname == "Configurations":
                value = _decode(child)
                value["_xml"] = copy.deepcopy(child)
                result.append(value)
        return result

    async def GetVideoEncoderConfigurationOptions(self, parameters):
        """Return actual Media2 per-codec options without a Media1 fallback."""
        response = await self._request(
            "GetVideoEncoderConfigurationOptions", parameters
        )
        return [
            _decode(child)
            for child in response
            if etree.QName(child).localname == "Options"
        ]

    async def SetVideoEncoderConfiguration(self, parameters):
        """Media2 persistence is mandatory and must be confirmed by the caller."""
        configuration = parameters["Configuration"]
        original = copy.deepcopy(configuration["_xml"])
        original.tag = f"{{{NAMESPACE}}}Configuration"
        for path in (
            ("Encoding",),
            ("Resolution", "Width"),
            ("Resolution", "Height"),
            ("Quality",),
            ("RateControl", "FrameRateLimit"),
            ("RateControl", "BitrateLimit"),
        ):
            element = original
            value = configuration
            for key in path:
                element = (
                    element.find(f"{{{SCHEMA}}}{key}") if element is not None else None
                )
                value = value.get(key) if isinstance(value, dict) else None
            if element is not None and value is not None:
                element.text = str(value)
        for name in ("GovLength", "Profile"):
            if configuration.get(name) is not None:
                original.set(name, str(configuration[name]))
            else:
                original.attrib.pop(name, None)
        # Reuse the same signed, bounded SOAP transport without serializing private XML
        # through an application-facing model.
        envelope, operation = self._envelope("SetVideoEncoderConfiguration")
        operation.append(original)
        await self._send("SetVideoEncoderConfiguration", envelope)
