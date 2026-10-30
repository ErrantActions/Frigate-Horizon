"""Bounded SOAP attachment encoding and parsing without external references."""

import secrets
from email import policy
from email.parser import BytesParser

from lxml import etree
from zeep.exceptions import Fault

from frigate.util.onvif_backup import MAX_BACKUP_BYTES, MAX_BACKUP_FILES

MAX_WIRE_BYTES = 64 * 1024 * 1024
SOAP_NAMESPACES = {
    "http://www.w3.org/2003/05/soap-envelope",
    "http://schemas.xmlsoap.org/soap/envelope/",
}
DEVICE_NAMESPACE = "http://www.onvif.org/ver10/device/wsdl"


def encode_mtom(envelope: bytes, attachments: dict[str, bytes]) -> tuple[bytes, str]:
    """Preserve attachment bytes exactly, including arbitrary binary newlines."""
    if not attachments or len(attachments) > MAX_BACKUP_FILES:
        raise ValueError("Invalid SOAP attachment count")
    if sum(len(value) for value in attachments.values()) > MAX_BACKUP_BYTES:
        raise ValueError("SOAP attachments exceed the backup limit")
    boundary = "horizon-" + secrets.token_hex(24)
    root_id = "horizon-soap"
    parts = [
        b"--" + boundary.encode() + b"\r\n"
        b'Content-Type: application/xop+xml; charset=UTF-8; type="application/soap+xml"\r\n'
        b"Content-Transfer-Encoding: binary\r\n"
        b"Content-ID: <horizon-soap>\r\n\r\n" + envelope + b"\r\n"
    ]
    for identifier, data in attachments.items():
        if not identifier or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
            for character in identifier
        ):
            raise ValueError("Invalid server-owned SOAP attachment identity")
        parts.append(
            b"--" + boundary.encode() + b"\r\n"
            b"Content-Type: application/octet-stream\r\n"
            b"Content-Transfer-Encoding: binary\r\n"
            b"Content-ID: <" + identifier.encode() + b">\r\n\r\n" + data + b"\r\n"
        )
    parts.append(b"--" + boundary.encode() + b"--\r\n")
    return (
        b"".join(parts),
        f'multipart/related; boundary="{boundary}"; type="application/xop+xml"; '
        f'start="<{root_id}>"; start-info="application/soap+xml"',
    )


def parse_soap_response(content: bytes, content_type: str, operation: str):
    """Require the matching SOAP response and resolve only included CID data."""
    if not content or len(content) > MAX_WIRE_BYTES:
        raise ValueError("The SOAP response is empty or exceeds the transfer limit")
    if "\r" in content_type or "\n" in content_type:
        raise ValueError("Invalid SOAP content type")
    attachments = {}
    envelope = content
    if content_type.lower().startswith("multipart/"):
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: "
            + content_type.encode("ascii")
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + content
        )
        if message.defects or not message.is_multipart():
            raise ValueError("Invalid SOAP multipart response")
        parts = list(message.iter_parts())
        if not 1 <= len(parts) <= MAX_BACKUP_FILES + 1:
            raise ValueError("Invalid SOAP multipart count")
        start = message.get_param("start")
        start = start.strip("<>") if start else None
        root = None
        total = 0
        for part in parts:
            identifier = str(part.get("Content-ID", "")).strip("<>")
            data = part.get_payload(decode=True)
            if part.defects or not identifier or not isinstance(data, bytes):
                raise ValueError("Invalid SOAP attachment")
            if identifier in attachments:
                raise ValueError("Duplicate SOAP attachment identity")
            attachments[identifier] = data
            total += len(data)
            if total > MAX_WIRE_BYTES:
                raise ValueError("SOAP attachments exceed the response limit")
            if (start and identifier == start) or (not start and root is None):
                root = identifier
        if root is None:
            raise ValueError("SOAP root attachment is missing")
        envelope = attachments.pop(root)
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    try:
        document = etree.fromstring(envelope, parser=parser)
    except etree.XMLSyntaxError:
        raise ValueError("Invalid SOAP response XML") from None
    if document.getroottree().docinfo.doctype:
        raise ValueError("SOAP document types are forbidden")
    name = etree.QName(document)
    if name.localname != "Envelope" or name.namespace not in SOAP_NAMESPACES:
        raise ValueError("Invalid SOAP envelope")
    body = document.find(f"{{{name.namespace}}}Body")
    if body is None or len(body) != 1:
        raise ValueError("Invalid SOAP response body")
    if etree.QName(body[0]).localname == "Fault":
        values = body[0].findall(
            f"./{{{name.namespace}}}Code//{{{name.namespace}}}Value"
        )
        allowed = {
            "Sender",
            "Receiver",
            "NotAuthorized",
            "ActionNotSupported",
            "NotSupported",
            "OptionalActionNotImplemented",
            "InvalidArgVal",
        }
        codes = [
            (item.text or "").split(":")[-1]
            for item in values
            if (item.text or "").split(":")[-1] in allowed
        ]
        raise Fault(
            "The camera rejected the protected transfer",
            code=codes[0] if codes else "Receiver",
            subcodes=[
                etree.QName("http://www.onvif.org/ver10/error", code)
                for code in codes[1:]
            ],
        )
    if body[0].tag != f"{{{DEVICE_NAMESPACE}}}{operation}Response":
        raise ValueError("The camera returned a different SOAP operation")
    return body[0], attachments


def backup_response_files(response, attachments) -> list[dict]:
    """Read actual ONVIF BackupFile/AttachmentData XOP references."""
    files = []
    referenced = set()
    for item in response:
        if etree.QName(item).localname != "BackupFiles":
            raise ValueError("Unexpected camera backup response field")
        values = {etree.QName(child).localname: child for child in item}
        name = values.get("Name")
        data = values.get("Data")
        if name is None or data is None or len(data) != 1:
            raise ValueError("Incomplete camera backup attachment")
        include = data[0]
        if include.tag != "{http://www.w3.org/2004/08/xop/include}Include":
            raise ValueError("Unsupported camera backup attachment format")
        reference = include.get("href", "")
        if not reference.startswith("cid:") or reference[4:] not in attachments:
            raise ValueError("The backup references unavailable attachment data")
        identifier = reference[4:]
        referenced.add(identifier)
        files.append({"Name": name.text, "Data": {"Data": attachments[identifier]}})
    if referenced != set(attachments):
        raise ValueError("The backup contains unreferenced attachments")
    return files
