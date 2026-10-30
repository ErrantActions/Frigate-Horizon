"""Read-only discovery/status, bounded camera recordings, and confirmed PTZ presets."""

import asyncio
import socket
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

from lxml import etree
from onvif import ONVIFError
from onvif.exceptions import ONVIFAuthError, ONVIFTimeoutError
from zeep.exceptions import Fault

from frigate.ptz.connection import camera_session, create_camera_service
from frigate.ptz.imaging import ImagingWriteUnverified
from frigate.ptz.recording_access import (
    RecordingBinding,
    RecordingSourceDenied,
    attributed_recordings,
    physical_sources,
    source_reference,
    verify_media_binding,
    verify_recording_source,
    verify_source_catalog,
)
from frigate.ptz.replay_transport import ReplayUnsupported, pin_replay_source
from frigate.util.onvif_device import device_value

SERVICE_NAMES = {
    "http://www.onvif.org/ver10/media/wsdl": "media",
    "http://www.onvif.org/ver20/media/wsdl": "media2",
    "http://www.onvif.org/ver20/imaging/wsdl": "imaging",
    "http://www.onvif.org/ver20/ptz/wsdl": "ptz",
    "http://www.onvif.org/ver10/events/wsdl": "events",
    "http://www.onvif.org/ver10/recording/wsdl": "recording",
    "http://www.onvif.org/ver10/search/wsdl": "search",
    "http://www.onvif.org/ver10/replay/wsdl": "replay",
}


def safe_uri(value, schemes=("http", "https")):
    """Remove userinfo and credential-bearing query strings from camera URLs."""
    parsed = urlsplit(str(value))
    if parsed.scheme not in schemes or not parsed.hostname:
        raise ValueError("The camera returned an unsupported URI")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    if parsed.port:
        host += f":{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


async def discover_devices():
    """Explicit, one-shot WS-Discovery multicast with bounded replies and lifetime."""
    message_id = f"urn:uuid:{uuid.uuid4()}"
    payload = (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        f"<s:Header><a:MessageID>{message_id}</a:MessageID>"
        "<a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>"
        "<a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>"
        "</s:Header><s:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types>"
        "</d:Probe></s:Body></s:Envelope>"
    ).encode()
    loop = asyncio.get_running_loop()
    found = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setblocking(False)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        await loop.sock_sendto(sock, payload, ("239.255.255.250", 3702))
        deadline = loop.time() + 3
        for _ in range(128):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                data, _address = await asyncio.wait_for(
                    loop.sock_recvfrom(sock, 65536), remaining
                )
            except TimeoutError:
                break
            try:
                root = etree.fromstring(
                    data, etree.XMLParser(resolve_entities=False, no_network=True)
                )
                related = root.find(
                    ".//{http://schemas.xmlsoap.org/ws/2004/08/addressing}RelatesTo"
                )
                if related is None or related.text != message_id:
                    continue
                for match in root.findall(
                    ".//{http://schemas.xmlsoap.org/ws/2005/04/discovery}ProbeMatch"
                ):
                    xaddrs = match.find(
                        "{http://schemas.xmlsoap.org/ws/2005/04/discovery}XAddrs"
                    )
                    for uri in (
                        (xaddrs.text or "").split() if xaddrs is not None else []
                    ):
                        address = safe_uri(uri)
                        parsed = urlsplit(address)
                        found[address] = {
                            "address": address,
                            "host": parsed.hostname,
                            "port": parsed.port
                            or (443 if parsed.scheme == "https" else 80),
                        }
            except (ValueError, etree.XMLSyntaxError):
                continue
    return {"devices": list(found.values()), "complete": True}


async def device_status(connection):
    """List advertised services, not assumed support inferred from SDK method names."""
    async with camera_session(connection) as camera:
        available = {
            SERVICE_NAMES[namespace]
            for namespace, endpoint in camera.xaddrs.items()
            if namespace in SERVICE_NAMES and endpoint
        }
        device = await create_camera_service(camera, "devicemgmt", connection)
        try:
            advertised = await device.GetServices({"IncludeCapability": False})
        except (ONVIFAuthError, ONVIFTimeoutError):
            raise
        except (ONVIFError, Fault):
            # Preserve actually reported GetCapabilities services on older
            # cameras, while GetServices adds Media2/search/replay when present.
            advertised = []
        available.update(
            SERVICE_NAMES[device_value(item, "Namespace")]
            for item in advertised or []
            if device_value(item, "Namespace") in SERVICE_NAMES
            and device_value(item, "XAddr")
        )
        return {
            "connected": True,
            "services": {name: name in available for name in SERVICE_NAMES.values()},
            "noise_reduction": "read_imaging_options",
            "administration": "explicit_admin_catalog",
        }


async def native_event_sources(connection):
    """Read physical source bindings without using the PTZ profile selector."""
    async with camera_session(connection) as camera:
        media = await create_camera_service(camera, "media", connection)
        profiles = await media.GetProfiles() or []
        physical_sources(profiles)
        return [
            {
                "token": device_value(profile, "token"),
                "name": device_value(profile, "Name"),
                "video_source_token": device_value(
                    profile, "VideoSourceConfiguration", "SourceToken"
                ),
                "configuration_token": device_value(
                    profile, "VideoSourceConfiguration", "token"
                ),
            }
            for profile in profiles
            if isinstance(device_value(profile, "token"), str)
            and bool(device_value(profile, "token").strip())
            and isinstance(
                device_value(profile, "VideoSourceConfiguration", "SourceToken"), str
            )
            and bool(
                device_value(profile, "VideoSourceConfiguration", "SourceToken").strip()
            )
        ]


async def profile_catalog(connection, kind):
    """Describe only reported media configuration bindings, including fixed cameras."""
    async with camera_session(connection) as camera:
        service = await create_camera_service(camera, kind, connection)
        profiles = []
        for item in await service.GetProfiles() or []:
            token = device_value(item, "token")
            if not isinstance(token, str) or not token:
                raise ValueError("The camera reported an invalid media profile")
            config = device_value(item, "Configurations") if kind == "media2" else item

            def value(media1, media2, key):
                return device_value(config, media2 if kind == "media2" else media1, key)

            profiles.append(
                {
                    "token": token,
                    "name": device_value(item, "Name") or token,
                    "video_source_token": value(
                        "VideoSourceConfiguration", "VideoSource", "SourceToken"
                    ),
                    "audio_source_token": value(
                        "AudioSourceConfiguration", "AudioSource", "SourceToken"
                    ),
                    "video_encoder_token": value(
                        "VideoEncoderConfiguration", "VideoEncoder", "token"
                    ),
                    "audio_encoder_token": value(
                        "AudioEncoderConfiguration", "AudioEncoder", "token"
                    ),
                    "ptz_configuration_token": value(
                        "PTZConfiguration", "PTZ", "token"
                    ),
                }
            )
        return {
            "state": "ready" if profiles else "unsupported",
            "service": kind,
            "profiles": profiles,
        }


async def presets(connection, action=None, token=None, name=None, guard=None):
    """Require an explicit media profile with PTZ configuration, never an imaging selector."""
    async with camera_session(connection) as camera:
        media = await create_camera_service(camera, "media", connection)
        profiles = await media.GetProfiles() or []
        selected = next(
            (
                item
                for item in profiles
                if device_value(item, "token") == connection.profile_token
                and device_value(item, "PTZConfiguration") is not None
            ),
            None,
        )
        if selected is None:
            if action:
                raise ValueError("Choose an explicitly reported PTZ media profile")
            ptz_profiles = [
                {
                    "token": device_value(item, "token"),
                    "name": device_value(item, "Name"),
                }
                for item in profiles
                if device_value(item, "PTZConfiguration") is not None
            ]
            return {
                "state": "choose_profile" if ptz_profiles else "unsupported",
                "profiles": ptz_profiles,
                "presets": [],
            }
        service = await create_camera_service(camera, "ptz", connection)
        current = (
            await service.GetPresets({"ProfileToken": connection.profile_token}) or []
        )
        if action:
            if action != "set" and token not in {
                device_value(item, "token") for item in current
            }:
                raise ValueError("Choose a reported preset")
            guard()
            request = {"ProfileToken": connection.profile_token}
            if token:
                request["PresetToken"] = token
            if action == "set":
                request["PresetName"] = name
                applied_token = await service.SetPreset(request)
            elif action == "goto":
                await service.GotoPreset(request)
            elif action == "remove":
                await service.RemovePreset(request)
            current = (
                await service.GetPresets({"ProfileToken": connection.profile_token})
                or []
            )
            if action == "set" and not any(
                device_value(item, "token") == (applied_token or token)
                and device_value(item, "Name") == name
                for item in current
            ):
                raise ImagingWriteUnverified(
                    "The camera did not confirm the saved preset"
                )
            if action == "remove" and any(
                device_value(item, "token") == token for item in current
            ):
                raise ImagingWriteUnverified(
                    "The camera did not confirm preset removal"
                )
        return {
            "state": "movement_acknowledged"
            if action == "goto"
            else "verified"
            if action
            else "read",
            "profiles": [
                {
                    "token": device_value(item, "token"),
                    "name": device_value(item, "Name"),
                }
                for item in profiles
                if device_value(item, "PTZConfiguration") is not None
            ],
            "presets": [
                {
                    "token": device_value(item, "token"),
                    "name": device_value(item, "Name") or device_value(item, "token"),
                }
                for item in current
            ],
        }


async def search_recordings(connection, start, end, binding=None):
    """Bounded ONVIF recording search with guaranteed search-session release."""
    if not isinstance(binding, RecordingBinding):
        raise RecordingSourceDenied("Explicit recording source binding is required")
    async with camera_session(connection) as camera:
        media = await create_camera_service(camera, "media", connection)
        profiles = await media.GetProfiles() or []
        verify_media_binding(binding, profiles)
        service = await create_camera_service(camera, "search", connection)
        token = await service.FindRecordings(
            {
                "Scope": {},
                "KeepAliveTime": timedelta(seconds=30),
                "MaxMatches": 100,
            }
        )
        try:
            result = await service.GetRecordingSearchResults(
                {
                    "SearchToken": token,
                    "MinResults": 1,
                    "MaxResults": 100,
                    "WaitTime": timedelta(seconds=5),
                }
            )
            records = []
            raw_information = device_value(result, "RecordingInformation") or []
            verify_source_catalog(profiles, raw_information, binding.source_key)
            information = attributed_recordings(binding, raw_information)
            for item in information:
                earliest = device_value(item, "EarliestRecording")
                latest = device_value(item, "LatestRecording")
                if (
                    not isinstance(earliest, datetime)
                    or not isinstance(latest, datetime)
                    or earliest.tzinfo is None
                    or latest.tzinfo is None
                ):
                    continue
                if earliest <= end and latest >= start:
                    records.append(
                        {
                            "token": device_value(item, "RecordingToken"),
                            "start": max(start, earliest),
                            "end": min(end, latest),
                        }
                    )
            return {
                "recordings": records,
                "search_state": str(device_value(result, "SearchState") or "Unknown"),
                "limit": 100,
            }
        finally:
            await service.EndSearch({"SearchToken": token})


async def replay_source(connection, token, start, end, binding=None):
    """Verify a recording/range and keep its authenticated replay address private."""
    if not isinstance(binding, RecordingBinding):
        raise RecordingSourceDenied("Explicit recording source binding is required")
    async with camera_session(connection) as camera:
        media = await create_camera_service(camera, "media", connection)
        profiles = await media.GetProfiles() or []
        verify_media_binding(binding, profiles)
        search = await create_camera_service(camera, "search", connection)
        information = await search.GetRecordingInformation({"RecordingToken": token})
        if device_value(information, "RecordingToken") != token:
            raise ReplayUnsupported(
                "The camera did not confirm the requested recording"
            )
        verify_recording_source(binding, information)
        physical = physical_sources(profiles)
        if len(physical) > 1:
            scope = await search.FindRecordings(
                {
                    "Scope": {},
                    "KeepAliveTime": timedelta(seconds=30),
                    "MaxMatches": 100,
                }
            )
            try:
                catalog = await search.GetRecordingSearchResults(
                    {
                        "SearchToken": scope,
                        "MinResults": 1,
                        "MaxResults": 100,
                        "WaitTime": timedelta(seconds=5),
                    }
                )
                verify_source_catalog(
                    profiles,
                    device_value(catalog, "RecordingInformation") or [],
                    binding.source_key,
                )
            finally:
                await search.EndSearch({"SearchToken": scope})
        earliest = device_value(information, "EarliestRecording")
        latest = device_value(information, "LatestRecording")
        if (
            not isinstance(earliest, datetime)
            or not isinstance(latest, datetime)
            or earliest.tzinfo is None
            or latest.tzinfo is None
            or start < earliest
            or end > latest
        ):
            raise ReplayUnsupported(
                "The camera did not confirm the requested recording range"
            )
        service = await create_camera_service(camera, "replay", connection)
        value = await service.GetReplayUri(
            {
                "StreamSetup": {
                    "Stream": "RTP-Unicast",
                    "Transport": {"Protocol": "RTSP"},
                },
                "RecordingToken": token,
            }
        )
        uri = device_value(value, "Uri") if not isinstance(value, str) else value
        source = await pin_replay_source(str(uri), connection)
        tracks = device_value(information, "Track")
        types = [device_value(track, "TrackType") for track in tracks or []]
        recorded_audio = (
            True
            if "Audio" in types
            else False
            if types
            and "Video" in types
            and all(kind in ("Video", "Audio", "Metadata") for kind in types)
            else None
        )
        return replace(source, recorded_audio=recorded_audio)


async def discover_recording_sources(connection, source_key):
    """Administrator-only attributed source discovery; no replay URLs are returned."""
    if not isinstance(source_key, bytes) or len(source_key) < 32:
        raise RecordingSourceDenied("Recording source key is unavailable")
    profiles = await native_event_sources(connection)
    async with camera_session(connection) as camera:
        service = await create_camera_service(camera, "search", connection)
        token = await service.FindRecordings(
            {
                "Scope": {},
                "KeepAliveTime": timedelta(seconds=30),
                "MaxMatches": 100,
            }
        )
        try:
            result = await service.GetRecordingSearchResults(
                {
                    "SearchToken": token,
                    "MinResults": 1,
                    "MaxResults": 100,
                    "WaitTime": timedelta(seconds=5),
                }
            )
            sources = {}
            for information in device_value(result, "RecordingInformation") or []:
                reference = source_reference(information, source_key)
                if reference is None:
                    continue
                name = device_value(information, "Source", "Name")
                if (
                    not isinstance(name, str)
                    or len(name) > 256
                    or any(part in name for part in ("://", "?", "@", "="))
                ):
                    name = None
                sources[reference] = {"source_ref": reference, "name": name}
            return {
                "profiles": profiles,
                "sources": list(sources.values()),
                "limit": 100,
                "search_state": str(device_value(result, "SearchState") or "Unknown"),
                "attribution_ready": len(sources)
                >= len({profile["video_source_token"] for profile in profiles})
                and bool(profiles)
                and bool(sources),
            }
        finally:
            await service.EndSearch({"SearchToken": token})
