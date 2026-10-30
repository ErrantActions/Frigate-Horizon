"""Fail-closed recording attribution for logical cameras sharing an ONVIF device."""

import hashlib
import hmac
import ipaddress
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from frigate.util.onvif_device import device_value


class RecordingSourceDenied(PermissionError):
    """Recording attribution is missing, ambiguous, or belongs to another source."""


@dataclass(frozen=True)
class RecordingBinding:
    profile_token: str
    video_source_token: str
    source_ref: str
    source_key: bytes = field(repr=False)


def source_reference(information, source_key):
    """Key opaque references so private source URIs cannot become password oracles."""
    identifier = device_value(information, "Source", "SourceId")
    if (
        not isinstance(identifier, str)
        or not identifier.strip()
        or len(identifier) > 8192
        or any(ord(character) < 32 for character in identifier)
    ):
        return None
    if not isinstance(source_key, bytes) or len(source_key) < 32:
        raise RecordingSourceDenied("A private recording source key is required")
    return hmac.new(
        source_key, b"onvif-recording-source-v1\0" + identifier.encode(), hashlib.sha256
    ).hexdigest()


def recording_binding(config, camera_name, source_key):
    """Use saved server-side source identity, never a viewer-supplied selector."""
    camera = config.cameras.get(camera_name)
    if camera is None:
        raise RecordingSourceDenied("Camera recording binding is unavailable")
    onvif = camera.onvif
    profile = getattr(onvif, "recording_profile", None)
    video_source = getattr(onvif, "recording_video_source_token", None)
    reference = getattr(onvif, "recording_source_ref", None)
    if (
        not isinstance(profile, str)
        or not profile.strip()
        or not isinstance(video_source, str)
        or not video_source.strip()
        or not isinstance(reference, str)
        or re.fullmatch("[a-f0-9]{64}", reference) is None
    ):
        raise RecordingSourceDenied("An explicit camera recording binding is required")

    def host_name(value):
        if "://" not in value:
            try:
                if ipaddress.ip_address(value).version == 6:
                    value = f"[{value}]"
            except ValueError:
                pass
            value = f"http://{value}"
        host = urlsplit(value).hostname
        return host.lower().rstrip(".") if host else None

    host = host_name(onvif.host)
    for name, other in config.cameras.items():
        if name == camera_name:
            continue
        target = other.onvif
        other_host = host_name(target.host)
        if (
            host == other_host
            and onvif.port == target.port
            and reference == getattr(target, "recording_source_ref", None)
            and video_source != getattr(target, "recording_video_source_token", None)
        ):
            raise RecordingSourceDenied(
                "Recording source is ambiguously bound to multiple lenses"
            )
    if not isinstance(source_key, bytes) or len(source_key) < 32:
        raise RecordingSourceDenied("Recording source key is unavailable")
    return RecordingBinding(profile, video_source, reference, source_key)


def verify_media_binding(binding, profiles):
    """Reject stale, reused, ambiguous, or caller-selected physical source profiles."""
    if not isinstance(binding, RecordingBinding):
        raise RecordingSourceDenied("Camera recording binding is required")
    selected = [
        profile
        for profile in profiles
        if device_value(profile, "token") == binding.profile_token
    ]
    if (
        len(selected) != 1
        or device_value(selected[0], "VideoSourceConfiguration", "SourceToken")
        != binding.video_source_token
    ):
        raise RecordingSourceDenied(
            "The saved recording media/video source is no longer valid"
        )


def verify_recording_source(binding, information):
    """A guessed recording token must still match this logical camera's source."""
    actual = (
        source_reference(information, binding.source_key)
        if isinstance(binding, RecordingBinding)
        else None
    )
    if actual is None or not hmac.compare_digest(actual, binding.source_ref):
        raise RecordingSourceDenied(
            "The recording does not belong to the authorized camera source"
        )


def attributed_recordings(binding, information):
    """Filter device-wide search results before returning any viewer-visible metadata."""
    if not isinstance(binding, RecordingBinding):
        raise RecordingSourceDenied("An explicit recording binding is required")
    if information and not any(
        source_reference(item, binding.source_key) for item in information
    ):
        raise RecordingSourceDenied(
            "Camera recording source attribution is unavailable"
        )
    return [
        item
        for item in information
        if source_reference(item, binding.source_key) == binding.source_ref
    ]


def verify_source_catalog(profiles, information, source_key):
    """Reject multi-lens devices whose index cannot distinguish physical sources."""
    physical = physical_sources(profiles)
    references = {source_reference(item, source_key) for item in information} - {None}
    if len(physical) > 1 and len(references) < len(physical):
        raise RecordingSourceDenied(
            "Multi-source recording attribution is incomplete or ambiguous"
        )


def physical_sources(profiles):
    """Do not treat malformed video-source configurations as audio-only profiles."""
    sources = set()
    for profile in profiles:
        configuration = device_value(profile, "VideoSourceConfiguration")
        if configuration is None:
            continue
        token = device_value(configuration, "SourceToken")
        if not isinstance(token, str) or not token.strip():
            raise RecordingSourceDenied(
                "A media profile has no attributable physical video source"
            )
        sources.add(token)
    return sources
