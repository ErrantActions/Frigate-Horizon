"""Capability-bounded Media1 and Media2 encoder changes with optimistic readback."""

import copy
import hashlib
import json

import httpx
from lxml.etree import Error as XmlError
from onvif import ONVIFError
from zeep.exceptions import Error as ZeepError

from frigate.api.defs.request.onvif_body import OnvifConnectionBody, OnvifEncoderChanges
from frigate.ptz.connection import camera_session, create_camera_service
from frigate.ptz.imaging import ImagingWriteUnverified, media_profiles
from frigate.util.onvif_device import (
    OnvifSettingsConflict,
    apply_encoder_changes,
    device_value,
    encoder_state,
)


def _media2_codec(option):
    limits = {
        "QualityRange": device_value(option, "QualityRange"),
        "H264": {
            key: device_value(option, key)
            for key in ("ResolutionsAvailable", "BitrateRange", "GovLengthRange")
        },
    }
    gop = device_value(option, "GovLengthRange")
    if isinstance(gop, str):
        gop = gop.split()
    if isinstance(gop, (list, tuple)) and len(gop) == 2:
        try:
            minimum, maximum = (int(value) for value in gop)
        except (TypeError, ValueError):
            pass
        else:
            limits["H264"]["GovLengthRange"] = {"Min": minimum, "Max": maximum}
    options = encoder_state({"token": "", "Encoding": "H264"}, limits, []).options
    rates = device_value(option, "FrameRatesSupported") or []
    if isinstance(rates, str):
        rates = rates.split()
    try:
        rates = [float(rate) for rate in rates]
    except (TypeError, ValueError):
        rates = []
    profiles = device_value(option, "ProfilesSupported") or []
    if isinstance(profiles, str):
        profiles = profiles.split()
    return options, [rate for rate in rates if 0 < rate < float("inf")], profiles


def media2_state(configuration, options, shared_profiles):
    """Normalize only reported Media2 limits, including H.265 and discrete rates."""
    encoding = device_value(configuration, "Encoding")
    option = next(
        (item for item in options if device_value(item, "Encoding") == encoding), None
    )
    normalized = {
        "token": device_value(configuration, "token"),
        "Encoding": "H264",
        "Resolution": device_value(configuration, "Resolution"),
        "Quality": device_value(configuration, "Quality"),
        "RateControl": device_value(configuration, "RateControl"),
        "H264": {"GovLength": device_value(configuration, "GovLength")},
    }
    limits = (
        {
            "QualityRange": device_value(option, "QualityRange"),
            "H264": {
                key: device_value(option, key)
                for key in ("ResolutionsAvailable", "BitrateRange", "GovLengthRange")
            },
        }
        if option is not None
        else {}
    )
    gop_range = device_value(option, "GovLengthRange")
    if isinstance(gop_range, str):
        gop_range = gop_range.split()
    if isinstance(gop_range, (list, tuple)) and len(gop_range) == 2:
        try:
            minimum, maximum = (int(value) for value in gop_range)
        except (TypeError, ValueError):
            pass
        else:
            limits["H264"]["GovLengthRange"] = {"Min": minimum, "Max": maximum}
    state = encoder_state(normalized, limits, shared_profiles)
    rates = device_value(option, "FrameRatesSupported") or []
    if isinstance(rates, str):
        rates = rates.split()
    try:
        rates = [float(rate) for rate in rates]
    except (TypeError, ValueError):
        rates = []
    state.encoding = encoding or ""
    state.service = "media2"
    state.frame_rates = [rate for rate in rates if 0 < rate < float("inf")]
    state.persistence_required = True
    reported = {
        device_value(item, "Encoding"): _media2_codec(item)
        for item in options
        if isinstance(device_value(item, "Encoding"), str) and bool(device_value(item, "Encoding"))
    }
    state.codec_options = {name: value[0] for name, value in reported.items()}
    state.codec_frame_rates = {name: value[1] for name, value in reported.items()}
    state.codec_profiles = {name: value[2] for name, value in reported.items()}
    state.codec_profile = device_value(configuration, "Profile")
    state.editable = option is not None
    state.reason = None if state.editable else "options_not_reported"
    state.revision = hashlib.sha256(
        json.dumps(
            state.model_dump(exclude={"revision"}),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return state


async def _read(service, kind, profile_token):
    profiles_raw = await service.GetProfiles() or []
    profiles = (
        media_profiles(profiles_raw)
        if kind == "media"
        else [
            {
                "token": device_value(profile, "token"),
                "name": device_value(profile, "Name"),
                "encoder_token": device_value(
                    profile, "Configurations", "VideoEncoder", "token"
                ),
                "video_source_token": device_value(
                    profile, "Configurations", "VideoSource", "SourceToken"
                ),
            }
            for profile in profiles_raw
        ]
    )
    if not profile_token:
        return {"profiles": profiles, "encoder": None, "profile_token": None}
    raw = next(
        (item for item in profiles_raw if device_value(item, "token") == profile_token),
        None,
    )
    if raw is None:
        raise ValueError("Choose a reported media profile")
    token = device_value(
        raw,
        *(
            ("VideoEncoderConfiguration", "token")
            if kind == "media"
            else ("Configurations", "VideoEncoder", "token")
        ),
    )
    if not token:
        raise ValueError("This profile has no video encoder configuration")
    if kind == "media":
        configuration = await service.GetVideoEncoderConfiguration(
            {"ConfigurationToken": token}
        )
        options = await service.GetVideoEncoderConfigurationOptions(
            {
                "ConfigurationToken": token,
                "ProfileToken": profile_token,
            }
        )
    else:
        configurations = await service.GetVideoEncoderConfigurations(
            {"ConfigurationToken": token}
        )
        configuration = next(
            (item for item in configurations if device_value(item, "token") == token),
            None,
        )
        if configuration is None:
            raise ValueError("The camera did not return the selected encoder")
        options = await service.GetVideoEncoderConfigurationOptions(
            {
                "ConfigurationToken": token,
                "ProfileToken": profile_token,
            }
        )
    shared = [
        device_value(item, "token")
        for item in profiles_raw
        if device_value(
            item,
            *(
                ("VideoEncoderConfiguration", "token")
                if kind == "media"
                else ("Configurations", "VideoEncoder", "token")
            ),
        )
        == token
    ]
    state = (encoder_state if kind == "media" else media2_state)(
        configuration, options, shared
    )
    return {
        "profiles": profiles,
        "profile_token": profile_token,
        "encoder": state,
        "_configuration": configuration,
    }


async def read_encoder(connection: OnvifConnectionBody, kind: str):
    """Read a deliberately selected Media1 or Media2 encoder, without fallback."""
    async with camera_session(connection) as camera:
        service = await create_camera_service(camera, kind, connection)
        result = await _read(service, kind, connection.profile_token)
        result.pop("_configuration", None)
        return result


async def write_encoder(
    connection,
    kind,
    changes: OnvifEncoderChanges,
    expected_revision,
    *,
    force_persistence=False,
    before_write=None,
):
    """Never retry a configuration mutation with an uncertain acknowledgement."""
    async with camera_session(connection) as camera:
        service = await create_camera_service(camera, kind, connection)
        result = await _read(service, kind, connection.profile_token)
        state = result["encoder"]
        if state is None:
            raise ValueError("Choose an encoder media profile")
        configuration = result["_configuration"]
        if kind == "media":
            updated = apply_encoder_changes(
                configuration, state, changes, expected_revision
            )
        else:
            if not force_persistence:
                raise ValueError(
                    "Media2 encoder writes require explicit device persistence"
                )
            if state.revision != expected_revision:
                raise OnvifSettingsConflict(
                    "The encoder changed; refresh before applying"
                )
            if not state.editable:
                raise ValueError("The camera did not report editable encoder options")
            updated = copy.deepcopy(configuration)
            from frigate.util.onvif_device import _set

            target = changes.encoding or state.encoding
            if target not in state.codec_options:
                raise ValueError("Choose a reported Media2 codec")
            limits = state.codec_options[target]
            rates = state.codec_frame_rates[target]
            if target != state.encoding:
                if (changes.resolution or state.resolution) not in limits.resolutions:
                    raise ValueError(
                        "Choose a resolution reported for the target codec"
                    )
                rate = (
                    changes.frame_rate
                    if changes.frame_rate is not None
                    else state.frame_rate
                )
                if rate not in rates:
                    raise ValueError(
                        "Choose a frame rate reported for the target codec"
                    )
                for key in ("bitrate_kbps", "quality"):
                    value = getattr(changes, key)
                    value = getattr(state, key) if value is None else value
                    bound = getattr(limits, key)
                    if (
                        value is None
                        or bound is None
                        or not bound.minimum <= value <= bound.maximum
                    ):
                        raise ValueError(
                            "Choose values with reported target codec limits"
                        )
                if state.codec_profiles[target] and changes.codec_profile not in state.codec_profiles[target]:
                    raise ValueError("Choose a reported target codec profile")
                if limits.gop_length is not None and changes.gop_length is None:
                    raise ValueError("Choose a target codec GOP length within reported limits")
                _set(updated, ("Encoding",), target)
                _set(updated, ("GovLength",), None)
                _set(updated, ("Profile",), None)
            if changes.codec_profile is not None:
                if changes.codec_profile not in state.codec_profiles[target]:
                    raise ValueError("Choose a reported Media2 codec profile")
                _set(updated, ("Profile",), changes.codec_profile)
            state = state.model_copy(update={"options": limits, "frame_rates": rates})
            for key, path in (
                ("quality", ("Quality",)),
                ("bitrate_kbps", ("RateControl", "BitrateLimit")),
                ("gop_length", ("GovLength",)),
            ):
                value = getattr(changes, key)
                if value is not None:
                    limits = getattr(state.options, key)
                    if limits is None or not limits.minimum <= value <= limits.maximum:
                        raise ValueError("Value is outside reported Media2 limits")
                    _set(updated, path, value)
            if changes.encoding_interval is not None:
                raise ValueError("Media2 does not advertise an encoding interval")
            if changes.frame_rate is not None:
                if changes.frame_rate not in state.frame_rates:
                    raise ValueError("Choose a reported Media2 frame rate")
                _set(updated, ("RateControl", "FrameRateLimit"), changes.frame_rate)
            if changes.resolution is not None:
                if changes.resolution not in state.options.resolutions:
                    raise ValueError("Choose a reported Media2 resolution")
                _set(updated, ("Resolution", "Width"), changes.resolution.width)
                _set(updated, ("Resolution", "Height"), changes.resolution.height)
        if before_write is not None:
            before_write()
        request = {"Configuration": updated}
        if kind == "media":
            request["ForcePersistence"] = force_persistence
        try:
            await service.SetVideoEncoderConfiguration(request)
            observed = await _read(service, kind, connection.profile_token)
        except (
            ONVIFError,
            ZeepError,
            XmlError,
            httpx.HTTPError,
            TimeoutError,
            OSError,
            ValueError,
        ) as error:
            raise ImagingWriteUnverified(
                "Encoder change could not be verified"
            ) from error
        for key, expected in changes.model_dump(exclude_none=True).items():
            actual = getattr(observed["encoder"], key)
            if hasattr(actual, "model_dump"):
                actual = actual.model_dump()
            if actual != expected:
                raise ImagingWriteUnverified(
                    "The encoder did not confirm every requested value"
                )
        observed.pop("_configuration", None)
        return observed
