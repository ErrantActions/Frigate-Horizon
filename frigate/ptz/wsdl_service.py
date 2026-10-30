"""Offline, unmodified official schema bindings for SDK-missing ONVIF services."""

import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

import httpx
from zeep import AsyncClient, Settings
from zeep.exceptions import Error as ZeepError
from lxml.etree import Error as XmlError
from zeep.proxy import AsyncServiceProxy
from zeep.wsa import WsAddressingPlugin

from frigate.util.onvif_transport import PrivateSoapTransport

SCHEMA_ROOT = Path(__file__).parent / "schemas"
MEDIA2_NAMESPACE = "http://www.onvif.org/ver20/media/wsdl"
MEDIA2_BINDING = f"{{{MEDIA2_NAMESPACE}}}Media2Binding"


class OfflineTransport(PrivateSoapTransport):
    """Load only checksum-pinned local schemas, never remote WSDL imports."""

    def load(self, url):
        """Resolve both original absolute imports and local relative imports offline."""
        parsed = urlsplit(str(url))
        if Path(str(url)).is_absolute():
            candidate = Path(url)
        elif parsed.scheme in ("http", "https"):
            if parsed.query or parsed.fragment or parsed.username or parsed.password:
                raise ValueError("Invalid schema import")
            candidate = (
                SCHEMA_ROOT / (parsed.hostname or "") / unquote(parsed.path).lstrip("/")
            )
        elif parsed.scheme == "file":
            candidate = Path(url2pathname(parsed.path))
        elif not parsed.scheme:
            candidate = Path(url)
        else:
            raise ValueError("Unsupported schema import scheme")
        candidate = candidate.resolve()
        root = SCHEMA_ROOT.resolve()
        if not candidate.is_relative_to(root):
            raise ValueError("Schema imports must remain inside the verified bundle")
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8-sig"))
        expected = {
            (
                root.joinpath(*item["path"].replace("\\", "/").split("/"))
            ).resolve(): item["sha256"]
            for item in manifest
        }
        digest = expected.get(candidate)
        if digest is None:
            raise ValueError("The schema import is not in the verified bundle")
        data = candidate.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("The bundled schema checksum does not match")
        return data

    async def aclose(self):
        """Release both owned HTTP clients even though schema loading is local."""
        try:
            await super().aclose()
        finally:
            await asyncio.to_thread(self.wsdl_client.close)


class SchemaService:
    """Expose all operations from the actual bound WSDL, without transport retries."""

    def __init__(self, client, transport, endpoint, binding_name):
        self.zeep_client = client
        self.transport = transport
        self.xaddr = endpoint
        self.binding_name = binding_name
        self.ws_client = AsyncServiceProxy(
            client, client.wsdl.bindings[binding_name], address=endpoint
        )

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        operation = self.ws_client[name]

        async def invoke(parameters=None):
            values = parameters or {}
            if (
                name == "GetProfiles"
                and self.binding_name == MEDIA2_BINDING
                and not values
            ):
                values = {"Type": ["All"]}
            return await operation(**values)

        return invoke

    async def close(self):
        """Close the service's single non-retrying transport."""
        await self.transport.aclose()


async def media2_service(endpoint, connection, wsse):
    """Bind the licensed official Media2 WSDL and all pinned schema dependencies."""
    transport = OfflineTransport(
        client=httpx.AsyncClient(
            auth=httpx.DigestAuth(
                connection.username, connection.password.get_secret_value()
            )
            if connection.auth_type == "digest"
            else None,
            verify=not connection.tls_insecure,
            timeout=10,
        ),
        wsdl_client=httpx.Client(timeout=10),
        operation_timeout=10,
    )
    path = SCHEMA_ROOT / "www.onvif.org" / "ver20" / "media" / "wsdl" / "media.wsdl"
    try:
        client = await asyncio.to_thread(
            AsyncClient,
            str(path),
            transport=transport,
            wsse=wsse,
            plugins=[WsAddressingPlugin(address_url=endpoint)],
            settings=Settings(strict=True, forbid_dtd=True, forbid_entities=True),
        )
        return SchemaService(client, transport, endpoint, MEDIA2_BINDING)
    except (asyncio.CancelledError, ValueError, OSError, ZeepError, XmlError):
        await transport.aclose()
        raise
