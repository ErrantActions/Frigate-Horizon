"""Scoped ONVIF image controls with source selection, bounded updates and readback."""

import inspect
import math
from collections.abc import Sequence

import httpx
from lxml.etree import Error as XmlError
from onvif import ONVIFError
from zeep.exceptions import Error as ZeepError

from frigate.api.defs.request.onvif_body import DeviceValue, OnvifConnectionBody
from frigate.api.defs.response.onvif_response import (
    OnvifImagingResponse,
    OnvifImagingState,
    OnvifMediaProfile,
)
from frigate.ptz.connection import camera_session, create_camera_service
from frigate.util.onvif_device import (
    apply_imaging_changes,
    build_imaging_patch,
    device_value,
    imaging_state,
)


class ImagingReadbackMismatch(RuntimeError):
    """The device did not confirm the requested values after a write."""

    def __init__(self, fields: tuple[str, ...], readback: OnvifImagingResponse):
        super().__init__("The camera did not confirm the requested imaging values")
        self.fields = fields
        self.readback = readback


class ImagingWriteUnverified(RuntimeError):
    """A write was sent, but its subsequent readback failed."""


def media_profiles(profiles: Sequence[object]) -> list[OnvifMediaProfile]:
    """Describe media profiles without exporting stream URLs or credentials."""
    result: list[OnvifMediaProfile] = []
    for profile in profiles:
        token = device_value(profile, "token")
        if not isinstance(token, str) or not token:
            raise ValueError("The camera reported an invalid media profile token")
        source = device_value(profile, "VideoSourceConfiguration", "SourceToken")
        if source is not None and not isinstance(source, str):
            raise ValueError("The camera reported an invalid video source token")
        result.append(
            OnvifMediaProfile(
                token=token,
                name=device_value(profile, "Name") or token,
                encoder_token=device_value(
                    profile, "VideoEncoderConfiguration", "token"
                ),
                video_source_token=source,
            )
        )
    return result


def select_imaging_profile(
    profiles: list[OnvifMediaProfile], token: str | None
) -> OnvifMediaProfile | None:
    """Auto-select only when all eligible profiles refer to one physical source."""
    eligible = [profile for profile in profiles if profile.video_source_token]
    if token is not None:
        for profile in eligible:
            if profile.token == token:
                return profile
        raise ValueError("The requested media profile has no reported imaging source")
    if not eligible:
        raise ValueError("The camera did not report an imaging source")
    sources = {profile.video_source_token for profile in eligible}
    return eligible[0] if len(sources) == 1 else None


def _response(
    profiles: list[OnvifMediaProfile],
    profile: OnvifMediaProfile,
    state: OnvifImagingState,
) -> OnvifImagingResponse:
    return OnvifImagingResponse(
        profiles=profiles,
        profile_token=profile.token,
        state="ready"
        if any(control.available for control in state.controls)
        else "read_only",
        imaging=state,
    )


async def read_imaging(connection: OnvifConnectionBody) -> OnvifImagingResponse:
    """Read camera-provided controls, requiring selection for multi-source devices."""
    async with camera_session(connection) as camera:
        media = await create_camera_service(camera, "media", connection)
        profiles = media_profiles(await media.GetProfiles() or [])
        profile = select_imaging_profile(profiles, connection.profile_token)
        if profile is None:
            return OnvifImagingResponse(profiles=profiles, state="choose_profile")
        source = profile.video_source_token
        assert source is not None
        service = await create_camera_service(camera, "imaging", connection)
        options = await service.GetOptions({"VideoSourceToken": source})
        settings = await service.GetImagingSettings({"VideoSourceToken": source})
        return _response(profiles, profile, imaging_state(settings, options, source))


async def write_imaging(
    connection: OnvifConnectionBody,
    changes: dict[str, DeviceValue],
    expected_values: dict[str, DeviceValue | None],
    *,
    confirm_device_change: bool,
    force_persistence: bool = False,
    before_write=None,
) -> OnvifImagingResponse:
    """Validate a confirmed, temporary image update and verify actual device values.

    The caller must serialize writes with its profile-application owner.
    Camera persistence is deliberately off; named Horizon profiles own persistence.
    """
    if confirm_device_change is not True:
        raise ValueError("Explicit device-change confirmation is required")
    if not changes:
        raise ValueError("Choose at least one imaging setting to change")
    async with camera_session(connection) as camera:
        media = await create_camera_service(camera, "media", connection)
        profiles = media_profiles(await media.GetProfiles() or [])
        profile = select_imaging_profile(profiles, connection.profile_token)
        if profile is None:
            raise ValueError("Choose a media profile for the intended physical source")
        source = profile.video_source_token
        assert source is not None
        service = await create_camera_service(camera, "imaging", connection)
        options = await service.GetOptions({"VideoSourceToken": source})
        settings = await service.GetImagingSettings({"VideoSourceToken": source})
        state = imaging_state(settings, options, source)
        updated = apply_imaging_changes(settings, state, changes, expected_values)
        patch = build_imaging_patch(updated, changes)
        if before_write is not None:
            pending = before_write()
            if inspect.isawaitable(pending):
                await pending
        try:
            await service.SetImagingSettings(
                {
                    "VideoSourceToken": source,
                    "ImagingSettings": patch,
                    "ForcePersistence": force_persistence,
                }
            )
            readback = await service.GetImagingSettings({"VideoSourceToken": source})
            observed = imaging_state(readback, options, source)
            response = _response(profiles, profile, observed)
        except (
            ONVIFError,
            ZeepError,
            XmlError,
            httpx.HTTPError,
            TimeoutError,
            OSError,
            ValueError,
            TypeError,
        ) as error:
            raise ImagingWriteUnverified(
                "Camera changes could not be verified"
            ) from error
        values = {control.key: control.value for control in observed.controls}
        mismatches = []
        for key, expected in changes.items():
            actual = values.get(key)
            matches = actual == expected
            if (
                isinstance(expected, (int, float))
                and not isinstance(expected, bool)
                and isinstance(actual, (int, float))
                and not isinstance(actual, bool)
            ):
                matches = math.isclose(actual, expected, rel_tol=1e-6, abs_tol=1e-6)
            if not matches:
                mismatches.append(key)
        if mismatches:
            raise ImagingReadbackMismatch(tuple(mismatches), response)
        return response
