"""Bounded WSDL-derived forms, strict inputs, and secret-safe ONVIF results."""

import base64
import binascii
import math
import re
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit

from isodate import Duration, duration_isoformat
from lxml import etree
from zeep import xsd
from zeep.exceptions import Fault, ValidationError

MAX_BINARY_BYTES = 8 * 1024 * 1024
INLINE_CERTIFICATE_OPERATIONS = frozenset(
    ("LoadCertificates", "LoadCACertificates", "LoadCertificateWithPrivateKey")
)
_SECRET = re.compile(
    r"password|passwd|passphrase|secret|private.?key|credential|nonce|security.?token|auth.?token|access.?token|pre.?shared.?key|encryption.?key|^key$",
    re.IGNORECASE,
)
_URL = re.compile(r"(?:https?|rtsps?)://[^\s<>\"']+", re.IGNORECASE)


def sensitive_field(name):
    """Treat authentication and private-key fields as write-only, including nested ones."""
    return bool(_SECRET.search(name))


def pinned_service_url(address, saved_host, *, allow_query=False):
    """Accept advertised SOAP endpoints only on the saved camera, without credentials."""
    parsed = urlsplit(address)
    if "://" in saved_host:
        saved = urlsplit(saved_host)
        host = saved.hostname
        secure = saved.scheme == "https"
    else:
        host = saved_host.strip("[]").lower()
        secure = False
    if (
        parsed.scheme not in ("http", "https")
        or parsed.hostname != host
        or (secure and parsed.scheme != "https")
        or parsed.username is not None
        or parsed.password is not None
        or (
            parsed.query
            and (
                not allow_query
                or len(parsed.query) > 1024
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in address
                )
            )
        )
        or parsed.fragment
        or not parsed.hostname
    ):
        raise ValueError(
            "The advertised service endpoint is not pinned to the saved camera"
        )
    if parsed.port is not None and not 0 < parsed.port <= 65535:
        raise ValueError("The advertised service port is invalid")
    return address


def schema_for_type(value, depth=0):
    """Describe actual Zeep schema elements, never invent vendor or wildcard schemas."""
    if depth > 7:
        return {"type": "object", "x-unavailable": "recursive_schema"}
    elements = getattr(value, "elements", None)
    attributes = getattr(value, "attributes", [])
    if isinstance(value, xsd.ComplexType) or elements:
        properties, required, omitted = {}, [], []
        for name, element in [*(elements or []), *attributes]:
            element_type = getattr(element, "type", None)
            child = schema_for_type(element_type, depth + 1)
            mandatory = getattr(element, "min_occurs", 0) > 0 or getattr(
                element, "required", False
            )
            if not schema_ready(child) and not mandatory:
                omitted.append(name)
                continue
            if getattr(element, "accepts_multiple", False):
                child = {"type": "array", "items": child, "maxItems": 128}
            if sensitive_field(name):
                child["writeOnly"] = True
                if child.get("type") == "string" and not child.get("x-binary"):
                    child["format"] = "password"
            properties[name] = child
            if mandatory:
                required.append(name)
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
            "x-omitted": omitted,
        }
    # An empty complex type is a real, parameterless request, not an arbitrary object.
    if elements == [] and getattr(value, "qname", None) is None:
        return {"type": "object", "properties": {}, "additionalProperties": False}
    item_type = getattr(value, "item_type", None)
    if item_type is not None:
        return {
            "type": "array",
            "items": schema_for_type(item_type, depth + 1),
            "maxItems": 128,
        }
    qname = getattr(value, "_default_qname", None) or getattr(value, "qname", None)
    name = getattr(qname, "localname", "")
    namespace = getattr(qname, "namespace", "")
    if namespace != "http://www.w3.org/2001/XMLSchema":
        return {"type": "object", "x-unavailable": "unresolved_schema_type"}
    if name in ("base64Binary", "hexBinary"):
        return {
            "type": "string",
            "format": "data-url",
            "writeOnly": True,
            "x-binary": name,
        }
    if name == "boolean":
        return {"type": "boolean"}
    if name in ("decimal", "float", "double"):
        return {"type": "number"}
    if name in (
        "integer",
        "int",
        "long",
        "short",
        "byte",
        "nonNegativeInteger",
        "positiveInteger",
        "unsignedLong",
        "unsignedInt",
        "unsignedShort",
        "unsignedByte",
    ):
        bounds = {
            "long": (-(2**63), 2**63 - 1),
            "int": (-(2**31), 2**31 - 1),
            "short": (-(2**15), 2**15 - 1),
            "byte": (-(2**7), 2**7 - 1),
            "unsignedLong": (0, 2**64 - 1),
            "unsignedInt": (0, 2**32 - 1),
            "unsignedShort": (0, 2**16 - 1),
            "unsignedByte": (0, 2**8 - 1),
        }
        schema = {"type": "integer"}
        if name in bounds:
            schema["minimum"], schema["maximum"] = bounds[name]
        elif name in ("nonNegativeInteger", "positiveInteger"):
            schema["minimum"] = 1 if name == "positiveInteger" else 0
        return schema
    if name in (
        "string",
        "normalizedString",
        "token",
        "anyURI",
        "duration",
        "dateTime",
        "date",
        "time",
        "QName",
        "language",
        "NCName",
        "Name",
        "NMTOKEN",
        "ID",
        "IDREF",
    ):
        return {"type": "string", "maxLength": 16384}
    return {"type": "object", "x-unavailable": "opaque_schema_type"}


def schema_ready(schema):
    """Only representable, non-wildcard request schemas can be executed."""
    return (
        not schema.get("x-unavailable")
        and all(schema_ready(item) for item in schema.get("properties", {}).values())
        and ("items" not in schema or schema_ready(schema["items"]))
    )


def binary_schema(schema):
    """Identify actual binary schema fields that require the protected file workflow."""
    return (
        bool(schema.get("x-binary"))
        or any(binary_schema(item) for item in schema.get("properties", {}).values())
        or ("items" in schema and binary_schema(schema["items"]))
    )


def describe_operations(
    service, readonly, managed_services, managed_operations, protected_output, name
):
    """Describe each real bound operation, keeping client readiness distinct from support."""
    client = getattr(service, "zeep_client", None)
    binding_name = getattr(service, "binding_name", None)
    if client is None or not binding_name:
        return None
    binding = client.wsdl.bindings.get(binding_name)
    if binding is None:
        return None
    result = []
    for operation_name, operation in sorted(binding.all().items()):
        body = getattr(getattr(operation, "input", None), "body", None)
        schema = (
            schema_for_type(body.type)
            if body is not None
            else {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            }
        )
        output_body = getattr(getattr(operation, "output", None), "body", None)
        output_schema = (
            schema_for_type(output_body.type) if output_body is not None else {}
        )
        read = operation_name in readonly
        inline_certificate = (
            name == "devicemgmt"
            and operation_name in INLINE_CERTIFICATE_OPERATIONS
            and not binary_schema(output_schema)
        )
        managed = (
            not read
            and (name in managed_services or operation_name in managed_operations)
        ) or (
            name in ("recording", "search", "replay")
            and operation_name not in ("GetServiceCapabilities", "GetCapabilities")
        )
        result.append(
            {
                "name": operation_name,
                "effect": "read" if read else "mutating",
                "typed_confirmation": not read,
                "schema": schema,
                "device_support": "unverified",
                "binary_input": binary_schema(schema),
                "binary_output": binary_schema(output_schema),
                "transfer_protocol": "device_upload_uri"
                if operation_name
                in ("StartFirmwareUpgrade", "StartSystemRestore", "AddAudioClip")
                else "soap_binary"
                if binary_schema(schema) or binary_schema(output_schema)
                else None,
                "readiness": "managed_workflow"
                if managed
                else "schema_unavailable"
                if not schema_ready(schema)
                else "file_transfer_required"
                if (binary_schema(schema) and not inline_certificate)
                or binary_schema(output_schema)
                or operation_name
                in ("StartFirmwareUpgrade", "StartSystemRestore", "AddAudioClip")
                else "protected_output"
                if operation_name in protected_output
                else "ready",
            }
        )
    return result


async def invoke_bound_operation(
    service, operation, parameters, confirmation, before_write
):
    """Validate against a real bound request and invoke exactly once without SDK retries."""
    if operation["readiness"] != "ready":
        raise ValueError("Use the protected or managed workflow for this operation")
    if operation["effect"] != "read" and confirmation != operation["name"]:
        raise ValueError("Type the exact operation name to confirm this device change")
    values = validate_parameters(operation["schema"], parameters)
    if binary_input_size(values) > MAX_BINARY_BYTES:
        raise ValueError("The combined uploaded files exceed eight MiB")
    binding = service.zeep_client.wsdl.bindings[service.binding_name]
    body = getattr(binding.get(operation["name"]).input, "body", None)
    if body is not None:
        try:
            request = body.type(**values)
            body.type.validate(request, required=True)
        except (TypeError, ValueError, ValidationError):
            raise ValueError("The request does not match the installed WSDL") from None
    if operation["effect"] != "read":
        before_write()
    try:
        result = await service.ws_client[operation["name"]](**values)
    except Fault as error:
        if any(
            str(code).split("}")[-1].split(":")[-1]
            in ("ActionNotSupported", "NotSupported", "OptionalActionNotImplemented")
            for code in [error.code, *(error.subcodes or [])]
        ):
            return {"state": "unsupported", "result": None}
        raise
    return {
        "state": "read" if operation["effect"] == "read" else "acknowledged_unverified",
        "result": safe_result(result),
    }


def validate_parameters(schema, value, depth=0):
    """Reject extra fields, unsupported structures and oversized binary uploads."""
    if depth > 10 or not schema_ready(schema):
        raise ValueError("This operation requires an unsupported request schema")
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict):
            raise ValueError("Use a WSDL-defined request object")
        properties = schema.get("properties", {})
        if set(value) - set(properties) or set(schema.get("required", [])) - set(value):
            raise ValueError("The request does not match the WSDL fields")
        return {
            key: validate_parameters(properties[key], item, depth + 1)
            for key, item in value.items()
        }
    if kind == "array":
        if not isinstance(value, list) or len(value) > 128:
            raise ValueError("Use a bounded WSDL-defined array")
        return [validate_parameters(schema["items"], item, depth + 1) for item in value]
    if kind == "boolean":
        if type(value) is not bool:
            raise ValueError("Use a boolean value")
    elif kind in ("integer", "number"):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("Use a finite numeric value")
        if kind == "integer" and not isinstance(value, int):
            raise ValueError("Use a whole-number value")
        if ("minimum" in schema and value < schema["minimum"]) or (
            "maximum" in schema and value > schema["maximum"]
        ):
            raise ValueError("The numeric value is outside the WSDL type range")
    elif kind == "string":
        if not isinstance(value, str):
            raise ValueError("Use a string value")
        if schema.get("x-binary"):
            if len(value) > MAX_BINARY_BYTES * 4 // 3 + 1024:
                raise ValueError("The uploaded file exceeds eight MiB")
            if not value.startswith("data:") or ";base64," not in value:
                raise ValueError("Choose a binary upload file")
            try:
                decoded = base64.b64decode(value.split(";base64,", 1)[1], validate=True)
            except (ValueError, binascii.Error):
                raise ValueError("Invalid binary upload encoding") from None
            if len(decoded) > MAX_BINARY_BYTES:
                raise ValueError("The uploaded file exceeds eight MiB")
            return decoded
        if len(value) > schema.get("maxLength", 16384):
            raise ValueError("The request string is too long")
    return value


def binary_input_size(value):
    """Count decoded files across the whole bounded request, not per field."""
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, Mapping):
        return sum(binary_input_size(item) for item in value.values())
    if isinstance(value, list):
        return sum(binary_input_size(item) for item in value)
    return 0


def _safe_text(value):
    def clean(match):
        try:
            parsed = urlsplit(match.group())
            if not parsed.hostname:
                return "[redacted URI]"
            host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
            port = parsed.port
        except ValueError:
            return "[redacted URI]"
        if port:
            host += f":{port}"
        # Subscription and upload bearer tokens can also appear in URL paths.
        return urlunsplit((parsed.scheme, host, "", "", ""))

    # Arbitrary embedded authentication text is never reflected.
    if _SECRET.search(value):
        return "[redacted]"
    return _URL.sub(clean, value[:16384])


def safe_result(value, depth=0):
    """Never reflect XML, binary/key material, credentials, or URI authentication."""
    if depth > 10:
        return {"redacted": True}
    if value is None or type(value) in (bool, int):
        return value
    if isinstance(value, float):
        return (
            value if math.isfinite(value) else {"redacted": True, "kind": "non_finite"}
        )
    if isinstance(value, Decimal):
        return (
            str(value)
            if value.is_finite()
            else {"redacted": True, "kind": "non_finite"}
        )
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, (bytes, bytearray, etree._Element)):
        return {"redacted": True, "kind": "binary_or_xml"}
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (timedelta, Duration)):
        return duration_isoformat(value)
    if isinstance(value, etree.QName):
        namespace = value.namespace or ""
        if "://" in namespace:
            parsed = urlsplit(namespace)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                return {"redacted": True}
        return str(value)
    if hasattr(value, "__values__"):
        value = value.__values__
    if isinstance(value, Mapping):
        return {
            str(key): {"redacted": True}
            if sensitive_field(str(key))
            else safe_result(item, depth + 1)
            for key, item in list(value.items())[:256]
        }
    if isinstance(value, (list, tuple)):
        return [safe_result(item, depth + 1) for item in value[:256]]
    return {"redacted": True, "kind": "opaque"}
