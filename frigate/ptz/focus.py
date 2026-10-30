"""Advertised absolute focus moves with explicit manual mode and readback."""

import hashlib
import json
import math

import httpx
from lxml.etree import Error as XmlError
from onvif import ONVIFError
from zeep.exceptions import Error as ZeepError
from zeep.exceptions import Fault

from frigate.ptz.connection import camera_session, create_camera_service
from frigate.ptz.imaging import (
    ImagingWriteUnverified,
    media_profiles,
    select_imaging_profile,
)
from frigate.util.onvif_device import (
    OnvifSettingsConflict,
    _number,
    _range,
    device_value,
)


def unsupported_operation(error):
    """Only an explicit standard unsupported-action fault denotes unsupported hardware."""
    if isinstance(error, ONVIFError):
        error = error.__cause__ or (error.args[0] if error.args else None)
    if not isinstance(error, Fault):
        return False
    return any(
        str(code).split("}")[-1].split(":")[-1]
        in ("ActionNotSupported", "NotSupported", "OptionalActionNotImplemented")
        for code in [error.code, *(error.subcodes or [])]
    )


async def _read(camera, connection):
    media = await create_camera_service(camera, "media", connection)
    profiles = media_profiles(await media.GetProfiles() or [])
    profile = select_imaging_profile(profiles, connection.profile_token)
    response = {
        "profiles": [item.model_dump() for item in profiles],
        "profile_token": profile.token if profile else None,
        "state": "choose_profile" if profile is None else "unsupported",
        "focus": None,
    }
    if profile is None:
        return response
    source = profile.video_source_token
    service = await create_camera_service(camera, "imaging", connection)
    try:
        options = await service.GetMoveOptions({"VideoSourceToken": source})
    except (ONVIFError, Fault) as error:
        if not unsupported_operation(error):
            raise
        return response
    position_range = _range(device_value(options, "Absolute", "Position"))
    if position_range is None:
        return response
    speed_range = _range(device_value(options, "Absolute", "Speed"))
    settings = await service.GetImagingSettings({"VideoSourceToken": source})
    status = await service.GetStatus({"VideoSourceToken": source})
    position = _number(device_value(status, "FocusStatus", "Position"))
    mode = device_value(settings, "Focus", "AutoFocusMode")
    state = {
        "video_source_token": source,
        "position": position,
        "position_range": position_range.model_dump(),
        "speed_range": speed_range.model_dump() if speed_range else None,
        "autofocus_mode": mode,
        "editable": position is not None and mode == "MANUAL",
    }
    state["revision"] = hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        **response,
        "state": "ready" if state["editable"] else "read_only",
        "focus": state,
    }


async def read_focus(connection):
    """Read only reported motor options, never assuming a fixed camera has a lens motor."""
    async with camera_session(connection) as camera:
        return await _read(camera, connection)


async def write_focus(connection, position, speed, expected_revision, *, before_write):
    """Request one absolute manual move; never start indefinite continuous motion."""
    async with camera_session(connection) as camera:
        response = await _read(camera, connection)
        focus = response["focus"]
        if focus is None or not focus["editable"]:
            raise ValueError("Manual absolute focus and readable position are required")
        if focus["revision"] != expected_revision:
            raise OnvifSettingsConflict(
                "Focus position or options changed; refresh before moving"
            )
        bounds = focus["position_range"]
        if not bounds["minimum"] <= position <= bounds["maximum"]:
            raise ValueError("Focus position is outside reported limits")
        target = {"Position": position}
        if speed is not None:
            bounds = focus["speed_range"]
            if bounds is None or not bounds["minimum"] <= speed <= bounds["maximum"]:
                raise ValueError("Focus speed is outside reported limits")
            target["Speed"] = speed
        before_write(response["profile_token"])
        service = await create_camera_service(camera, "imaging", connection)
        try:
            await service.Move(
                {
                    "VideoSourceToken": focus["video_source_token"],
                    "Focus": {"Absolute": target},
                }
            )
            observed = await _read(camera, connection)
        except (
            ONVIFError,
            ZeepError,
            XmlError,
            httpx.HTTPError,
            TimeoutError,
            OSError,
            ValueError,
        ) as error:
            raise ImagingWriteUnverified("Focus move could not be verified") from error
        actual = (observed["focus"] or {}).get("position")
        if actual is None or not math.isclose(
            actual, position, rel_tol=1e-6, abs_tol=1e-6
        ):
            raise ImagingWriteUnverified(
                "The camera has not confirmed the requested focus position"
            )
        return observed
