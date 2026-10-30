"""Admin-only advertised-service catalog and fail-closed WSDL operation gateway."""

import hmac
import json
import secrets
from types import SimpleNamespace

from lxml import etree
from lxml.etree import Error as XmlError
from onvif import ONVIFError
from onvif.definition import SERVICES
from zeep.exceptions import Error as ZeepError
from zeep.exceptions import Fault, ValidationError

from frigate.ptz.connection import camera_session, create_camera_service
from frigate.ptz.focus import unsupported_operation
from frigate.util.onvif_backup import backup_download, restore_files
from frigate.util.onvif_device import OnvifSettingsConflict, device_value
from frigate.util.onvif_mtom import (
    backup_response_files,
    encode_mtom,
    parse_soap_response,
)
from frigate.util.onvif_schema import (
    describe_operations,
    invoke_bound_operation,
    pinned_service_url,
)

READ_OPERATIONS = frozenset(
    """
GetServices GetServiceCapabilities GetCapabilities GetDeviceInformation GetHostname
GetNetworkInterfaces GetNetworkProtocols GetNetworkDefaultGateway GetDNS GetNTP
GetDynamicDNS GetSystemDateAndTime GetUsers GetCertificates GetCertificatesStatus
GetCACertificates GetClientCertificateMode GetCertificateInformation GetScopes
GetDiscoveryMode GetRemoteDiscoveryMode GetDPAddresses GetEndpointReference
GetProfiles GetVideoSources GetAudioSources GetVideoSourceConfigurations
GetAudioSourceConfigurations GetVideoEncoderConfigurations GetAudioEncoderConfigurations
GetVideoEncoderConfiguration GetAudioEncoderConfiguration GetVideoEncoderConfigurationOptions
GetAudioEncoderConfigurationOptions GetVideoSourceConfiguration GetAudioSourceConfiguration
GetVideoSourceConfigurationOptions GetAudioSourceConfigurationOptions GetGuaranteedNumberOfVideoEncoderInstances
GetOptions GetImagingSettings GetMoveOptions GetStatus GetPresets GetNodes GetNode
GetConfigurations GetConfiguration GetConfigurationOptions GetCompatibleConfigurations
GetReceivers GetReceiver GetReceiverState GetRelayOutputs GetRelayOutputOptions
GetDigitalInputs GetSerialPorts GetSerialPortConfiguration GetSerialPortConfigurationOptions
GetAnalyticsEngines GetAnalyticsEngine GetAnalyticsEngineControls GetAnalyticsEngineControl
GetAnalyticsEngineInputs GetAnalyticsEngineInput GetSupportedAnalyticsModules GetAnalyticsModules
GetSupportedRules GetRules GetEventProperties GetRecordingSummary GetRecordings
GetRecordingConfiguration GetRecordingOptions GetRecordingJobs GetRecordingJobConfiguration
GetRecordingJobState GetTrackConfiguration GetRecordingInformation GetMediaAttributes
GetReplayConfiguration GetOSDs GetOSD GetOSDOptions
GetAudioClips GetPlayingAudioClips
GetAnalyticsConfigurations GetMetadataConfigurations GetAudioOutputConfigurations
GetAudioDecoderConfigurations GetMetadataConfigurationOptions GetAudioOutputConfigurationOptions
GetAudioDecoderConfigurationOptions GetVideoEncoderInstances GetStreamUri GetSnapshotUri
GetVideoSourceModes GetMasks GetMaskOptions GetWebRTCConfigurations
GetMulticastAudioDecoderConfigurations GetMulticastAudioDecoderConfigurationOptions
""".split()
)
PROTECTED_OUTPUT = frozenset(("GetSystemBackup", "GetSystemLog", "GetAccessPolicy"))
MANAGED_SERVICES = frozenset(("imaging", "events", "recording", "search", "replay"))
MANAGED_OPERATIONS = frozenset(
    (
        "SetVideoEncoderConfiguration",
        "SetAudioEncoderConfiguration",
        "SetVideoSourceConfiguration",
        "SetVideoSourceMode",
        "ContinuousMove",
        "StartMulticastStreaming",
        "UpgradeSystemFirmware",
        "StartFirmwareUpgrade",
        "RestoreSystem",
    )
)
MEDIA2 = "http://www.onvif.org/ver20/media/wsdl"
_CATALOG_KEY = secrets.token_bytes(32)


def _endpoint(camera, namespace, connection):
    address = camera.xaddrs.get(namespace)
    if not address:
        return None
    return pinned_service_url(address, connection.host)


def _operations(service, name):
    operations = describe_operations(
        service,
        READ_OPERATIONS,
        MANAGED_SERVICES,
        MANAGED_OPERATIONS,
        PROTECTED_OUTPUT,
        name,
    )
    if operations is None:
        return None
    for item in operations:
        item["binding_id"] = service.binding_name
    for binding_id in sorted(service.zeep_client.wsdl.bindings):
        if binding_id == service.binding_name:
            continue
        alternate = SimpleNamespace(
            zeep_client=service.zeep_client, binding_name=binding_id
        )
        for item in (
            describe_operations(
                alternate,
                READ_OPERATIONS,
                MANAGED_SERVICES,
                MANAGED_OPERATIONS,
                PROTECTED_OUTPUT,
                name,
            )
            or []
        ):
            item["binding_id"] = binding_id
            item["readiness"] = "service_reference_required"
            operations.append(item)
    return operations


async def _catalog(camera, connection):
    device = await create_camera_service(camera, "devicemgmt", connection)
    discovery, versions = "get_services", {}
    try:
        advertised = await device.GetServices({"IncludeCapability": False})
    except (ONVIFError, Fault) as error:
        if not unsupported_operation(error):
            raise
        advertised = []
        discovery = "get_capabilities_fallback"
    for item in advertised or []:
        namespace, address = (
            device_value(item, "Namespace"),
            device_value(item, "XAddr"),
        )
        if (
            not isinstance(namespace, str)
            or not isinstance(address, str)
            or not namespace
            or not address
        ):
            raise ValueError("The camera reported an invalid service description")
        camera.xaddrs[namespace] = camera.rewrite_url(address)
        major, minor = (
            device_value(item, "Version", "Major"),
            device_value(item, "Version", "Minor"),
        )
        if type(major) is int and type(minor) is int:
            versions[namespace] = {"major": major, "minor": minor}
    definitions = {}
    for name, definition in SERVICES.items():
        definitions.setdefault(definition.get("ns"), []).append(name)
    rows = []
    namespaces = set(camera.xaddrs)
    namespaces.add(SERVICES["devicemgmt"]["ns"])
    for namespace in sorted(namespaces):
        names = definitions.get(namespace, [])
        # Subscription references are not advertised device-service bindings.
        names = [name for name in names if name not in ("pullpoint", "subscription")]
        name = names[0] if names else "media2" if namespace == MEDIA2 else None
        rejected = False
        try:
            address = _endpoint(camera, namespace, connection)
        except ValueError:
            address = None
            rejected = True
        if name != "devicemgmt" and address is None and not rejected:
            continue
        row = {
            "namespace": namespace,
            "service": name,
            "advertised": True,
            "binding": "unavailable",
            "operations": [],
            "version": versions.get(namespace),
        }
        if rejected:
            row["binding"] = "endpoint_rejected"
            rows.append(row)
            continue
        if name:
            try:
                service = await create_camera_service(camera, name, connection)
                operations = _operations(service, name)
            except (ONVIFError, ZeepError, XmlError, OSError, ValueError):
                row["binding_error"] = "client_binding_setup_failed"
                rows.append(row)
                continue
            if operations is not None:
                row.update(binding="available", operations=operations)
        rows.append(row)
    # Include pinned endpoints in the private revision, never in editable browser fields.
    revision = hmac.new(
        _CATALOG_KEY,
        json.dumps(
            {"services": rows, "endpoints": camera.xaddrs},
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
        digestmod="sha256",
    ).hexdigest()
    return {
        "revision": revision,
        "services": rows,
        "discovery": discovery,
        "support": "advertised_service_not_operation_guarantee",
        "coverage": {
            "service_bindings_available": sum(
                row["binding"] == "available" for row in rows
            ),
            "service_bindings_unavailable": sum(
                row["binding"] == "unavailable" for row in rows
            ),
            "service_endpoints_rejected": sum(
                row["binding"] == "endpoint_rejected" for row in rows
            ),
            "operation_states": {
                state: sum(
                    item["readiness"] == state
                    for row in rows
                    for item in row["operations"]
                )
                for state in (
                    "ready",
                    "managed_workflow",
                    "file_transfer_required",
                    "protected_output",
                    "schema_unavailable",
                    "service_reference_required",
                )
            },
        },
        "complete": all(
            row["binding"] == "available"
            and all(item["readiness"] == "ready" for item in row["operations"])
            for row in rows
        ),
    }


async def administration_catalog(connection):
    """Load real installed WSDL bindings for reported services, without probing operations."""
    async with camera_session(connection) as camera:
        return await _catalog(camera, connection)


async def download_system_backup(connection, revision):
    """Read an explicitly requested backup through the pinned device connection."""
    async with camera_session(connection) as camera:
        catalog = await _catalog(camera, connection)
        if catalog["revision"] != revision:
            raise OnvifSettingsConflict("The service catalog changed; read it again")
        row = next(
            (item for item in catalog["services"] if item["service"] == "devicemgmt"),
            None,
        )
        if row is None or row["binding"] != "available":
            raise ValueError("The device-management binding is unavailable")
        operation = next(
            (item for item in row["operations"] if item["name"] == "GetSystemBackup"),
            None,
        )
        if operation is None:
            raise ValueError("The device does not advertise a backup operation")
        service = await create_camera_service(camera, "devicemgmt", connection)
        response, attachments = await _protected_soap(service, "GetSystemBackup", {})
        files = backup_response_files(response, attachments)
        return backup_download(files)


async def _protected_soap(
    service, operation, values, attachments=None, before_write=None
):
    binding = service.zeep_client.wsdl.bindings[service.binding_name]
    envelope, headers = binding._create(
        operation,
        (),
        values,
        client=service.zeep_client,
        options={"address": service.xaddr},
    )
    content = etree.tostring(envelope, xml_declaration=True, encoding="utf-8")
    if attachments:
        content, content_type = encode_mtom(content, attachments)
        headers = {**headers, "Content-Type": content_type}
    if before_write is not None:
        before_write()
    response = await service.transport.post(service.xaddr, content, headers)
    try:
        result = parse_soap_response(
            response.content, response.headers.get("Content-Type", ""), operation
        )
    except ValueError as error:
        response.raise_for_status()
        raise ONVIFError("Invalid protected transfer response") from error
    response.raise_for_status()
    return result


async def restore_system_backup(connection, revision, payload, *, before_write):
    """Restore checked backup files once, never retrying an uncertain device write."""
    files = restore_files(payload)
    async with camera_session(connection) as camera:
        catalog = await _catalog(camera, connection)
        if catalog["revision"] != revision:
            raise OnvifSettingsConflict("The service catalog changed; read it again")
        row = next(
            (item for item in catalog["services"] if item["service"] == "devicemgmt"),
            None,
        )
        if (
            row is None
            or row["binding"] != "available"
            or not any(item["name"] == "RestoreSystem" for item in row["operations"])
        ):
            raise ValueError("The device-management restore binding is unavailable")
        service = await create_camera_service(camera, "devicemgmt", connection)
        binding = service.zeep_client.wsdl.bindings[service.binding_name]
        body = binding.get("RestoreSystem").input.body
        attachments = {}
        entries = []
        for item in files:
            identifier = "backup-" + secrets.token_hex(16)
            attachments[identifier] = item["Data"]["Data"]
            entries.append(
                {
                    "Name": item["Name"],
                    "Data": {
                        "Include": {"href": f"cid:{identifier}"},
                        "contentType": "application/octet-stream",
                    },
                }
            )
        values = {"BackupFiles": entries}
        try:
            request = body.type(**values)
            body.type.validate(request, required=True)
        except (TypeError, ValueError, ValidationError):
            raise ValueError(
                "The backup does not match the camera restore schema"
            ) from None
        await _protected_soap(
            service, "RestoreSystem", values, attachments, before_write
        )
        return {"state": "acknowledged_unverified", "result": None}


async def invoke_operation(
    connection,
    service_name,
    operation_name,
    parameters,
    revision,
    confirmation,
    *,
    before_write,
):
    """Invoke one validated pinned operation; unknown effects require write consent."""
    async with camera_session(connection) as camera:
        catalog = await _catalog(camera, connection)
        if catalog["revision"] != revision:
            raise OnvifSettingsConflict("The service catalog changed; read it again")
        row = next(
            (item for item in catalog["services"] if item["service"] == service_name),
            None,
        )
        if row is None or row["binding"] != "available":
            raise ValueError(
                "No advertised schema binding is available for this service"
            )
        operation = next(
            (item for item in row["operations"] if item["name"] == operation_name), None
        )
        if operation is None or operation["readiness"] != "ready":
            raise ValueError("Use the managed workflow or a supported schema operation")
        service = await create_camera_service(camera, service_name, connection)
        return await invoke_bound_operation(
            service, operation, parameters, confirmation, before_write
        )
