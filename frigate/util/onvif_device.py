"""Validate camera-advertised ONVIF encoder and imaging settings."""

import copy
import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

from frigate.api.defs.request.onvif_body import (
    DeviceValue,
    OnvifEncoderChanges,
    VideoResolution,
)
from frigate.api.defs.response.onvif_response import (
    OnvifEncoderOptions,
    OnvifEncoderState,
    OnvifImagingControl,
    OnvifImagingState,
    OnvifNumberRange,
)

IMAGING_PATHS = {
    "brightness": ("Brightness",),
    "contrast": ("Contrast",),
    "saturation": ("ColorSaturation",),
    "sharpness": ("Sharpness",),
    "backlight_mode": ("BacklightCompensation", "Mode"),
    "backlight_level": ("BacklightCompensation", "Level"),
    "exposure_mode": ("Exposure", "Mode"),
    "exposure_priority": ("Exposure", "Priority"),
    "exposure_time": ("Exposure", "ExposureTime"),
    "exposure_gain": ("Exposure", "Gain"),
    "exposure_iris": ("Exposure", "Iris"),
    "exposure_min_time": ("Exposure", "MinExposureTime"),
    "exposure_max_time": ("Exposure", "MaxExposureTime"),
    "exposure_min_gain": ("Exposure", "MinGain"),
    "exposure_max_gain": ("Exposure", "MaxGain"),
    "exposure_min_iris": ("Exposure", "MinIris"),
    "exposure_max_iris": ("Exposure", "MaxIris"),
    "ir_cut_filter": ("IrCutFilter",),
    "focus_mode": ("Focus", "AutoFocusMode"),
    "focus_speed": ("Focus", "DefaultSpeed"),
    "focus_near_limit": ("Focus", "NearLimit"),
    "focus_far_limit": ("Focus", "FarLimit"),
    "white_balance_mode": ("WhiteBalance", "Mode"),
    "white_balance_cr_gain": ("WhiteBalance", "CrGain"),
    "white_balance_cb_gain": ("WhiteBalance", "CbGain"),
    "wide_dynamic_range_mode": ("WideDynamicRange", "Mode"),
    "wide_dynamic_range_level": ("WideDynamicRange", "Level"),
    "noise_reduction_level": ("Extension", "Extension", "Extension", "NoiseReduction", "Level"),
}
MANUAL_IMAGING_MODES = {
    "exposure_time": "exposure_mode",
    "exposure_gain": "exposure_mode",
    "exposure_iris": "exposure_mode",
    "white_balance_cr_gain": "white_balance_mode",
    "white_balance_cb_gain": "white_balance_mode",
}


class OnvifSettingsConflict(ValueError):
    """The device no longer has the settings the user reviewed."""


def device_value(value: Any, *path: str) -> Any:
    """Read a known field from a Zeep value or a serialized mapping."""
    for key in path:
        value = (
            value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)
        )
        if value is None:
            break
    return value


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def _range(value: Any) -> OnvifNumberRange | None:
    minimum = _number(device_value(value, "Min"))
    maximum = _number(device_value(value, "Max"))
    if minimum is None or maximum is None or minimum > maximum:
        return None
    return OnvifNumberRange(minimum=minimum, maximum=maximum)


def _set(value: Any, path: tuple[str, ...], updated: Any) -> None:
    for key in path[:-1]:
        child = device_value(value, key)
        if child is None:
            raise ValueError("The camera did not report the requested settings group")
        value = child
    if isinstance(value, dict):
        value[path[-1]] = updated
    else:
        setattr(value, path[-1], updated)


def _in_range(value: float, limits: OnvifNumberRange | None) -> bool:
    return limits is not None and limits.minimum <= value <= limits.maximum


def _codec_options(options, encoding):
    codec = device_value(options, encoding)
    extension = device_value(options, "Extension", encoding)
    resolutions = []
    for item in device_value(codec, "ResolutionsAvailable") or []:
        width, height = device_value(item, "Width"), device_value(item, "Height")
        if (
            isinstance(width, int)
            and not isinstance(width, bool)
            and width > 0
            and isinstance(height, int)
            and not isinstance(height, bool)
            and height > 0
        ):
            resolutions.append(VideoResolution(width=width, height=height))
    return OnvifEncoderOptions(
        resolutions=resolutions,
        frame_rate=_range(device_value(codec, "FrameRateRange")),
        bitrate_kbps=_range(
            device_value(extension, "BitrateRange")
            or device_value(codec, "BitrateRange")
        ),
        quality=_range(device_value(options, "QualityRange")),
        gop_length=_range(device_value(codec, "GovLengthRange")),
        encoding_interval=_range(device_value(codec, "EncodingIntervalRange")),
    )


def encoder_state(
    configuration: Any, options: Any, shared_profiles: list[str]
) -> OnvifEncoderState:
    """Describe only limits the camera actually reported for its active encoding."""
    encoding = str(device_value(configuration, "Encoding") or "")
    codec = device_value(options, encoding)
    resolution = device_value(configuration, "Resolution")
    width = device_value(resolution, "Width")
    height = device_value(resolution, "Height")
    current_resolution = (
        VideoResolution(width=width, height=height)
        if isinstance(width, int)
        and isinstance(height, int)
        and width > 0
        and height > 0
        else None
    )
    resolutions = []
    for item in device_value(codec, "ResolutionsAvailable") or []:
        item_width = device_value(item, "Width")
        item_height = device_value(item, "Height")
        if (
            isinstance(item_width, int)
            and isinstance(item_height, int)
            and item_width > 0
            and item_height > 0
        ):
            resolutions.append(VideoResolution(width=item_width, height=item_height))
    extension = device_value(options, "Extension", encoding)
    current = {
        "token": str(device_value(configuration, "token") or ""),
        "encoding": encoding,
        "resolution": current_resolution.model_dump() if current_resolution else None,
        "frame_rate": _number(
            device_value(configuration, "RateControl", "FrameRateLimit")
        ),
        "bitrate_kbps": device_value(configuration, "RateControl", "BitrateLimit"),
        "quality": _number(device_value(configuration, "Quality")),
        "gop_length": device_value(configuration, encoding, "GovLength"),
        "encoding_interval": device_value(
            configuration, "RateControl", "EncodingInterval"
        ),
    }
    limits = OnvifEncoderOptions(
        resolutions=resolutions,
        frame_rate=_range(device_value(codec, "FrameRateRange")),
        bitrate_kbps=_range(
            device_value(extension, "BitrateRange")
            or device_value(codec, "BitrateRange")
        ),
        quality=_range(device_value(options, "QualityRange")),
        gop_length=_range(device_value(codec, "GovLengthRange")),
        encoding_interval=_range(device_value(codec, "EncodingIntervalRange")),
    )
    revision = hashlib.sha256(
        json.dumps(current, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    supported = codec is not None and encoding in ("JPEG", "H264", "MPEG4")
    state = OnvifEncoderState(
        **current,
        options=limits,
        revision=revision,
        shared_profiles=shared_profiles,
        editable=supported,
        codec_profile=device_value(
            configuration,
            encoding,
            "H264Profile" if encoding == "H264" else "Mpeg4Profile",
        ),
        codec_options={
            name: _codec_options(options, name)
            for name in ("JPEG", "H264", "MPEG4")
            if device_value(options, name) is not None
        },
        codec_profiles={
            name: list(device_value(options, name, field) or [])
            for name, field in (
                ("H264", "H264ProfilesSupported"),
                ("MPEG4", "Mpeg4ProfilesSupported"),
            )
            if device_value(options, name) is not None
        },
        reason=None
        if supported
        else (
            "unsupported_encoding"
            if encoding not in ("JPEG", "H264", "MPEG4")
            else "options_not_reported"
        ),
    )
    state.revision = hashlib.sha256(
        json.dumps(
            state.model_dump(exclude={"revision"}),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return state


def apply_encoder_changes(
    configuration: Any,
    state: OnvifEncoderState,
    changes: OnvifEncoderChanges,
    expected_revision: str,
) -> Any:
    """Copy and validate an encoder update without modifying the fetched object."""
    if expected_revision != state.revision:
        raise OnvifSettingsConflict(
            "Camera encoder settings changed; refresh before applying"
        )
    target = changes.encoding or state.encoding
    options = state.codec_options.get(target)
    if target != state.encoding and options is None:
        raise ValueError("The camera did not advertise the requested codec")
    if target == state.encoding and not state.editable:
        raise ValueError("The camera did not report editable options for this encoding")
    updated = copy.deepcopy(configuration)
    if target != state.encoding:
        assert options is not None
        resolution = changes.resolution or state.resolution
        if resolution not in options.resolutions:
            raise ValueError("Choose a resolution reported for the target codec")
        for key in ("frame_rate", "bitrate_kbps", "quality", "encoding_interval"):
            value = getattr(changes, key)
            value = getattr(state, key) if value is None else value
            if value is None or not _in_range(value, getattr(options, key)):
                raise ValueError(
                    "Choose values with reported limits for the target codec"
                )
        if target in ("H264", "MPEG4") and (
            changes.codec_profile not in state.codec_profiles.get(target, [])
            or changes.gop_length is None
        ):
            raise ValueError("Choose a reported codec profile and GOP length")
        _set(updated, ("Encoding",), target)
        for name in ("H264", "MPEG4"):
            _set(updated, (name,), None)
        if target in ("H264", "MPEG4"):
            _set(
                updated,
                (target,),
                {
                    "GovLength": changes.gop_length,
                    "H264Profile"
                    if target == "H264"
                    else "Mpeg4Profile": changes.codec_profile,
                },
            )
    if changes.codec_profile is not None:
        if changes.codec_profile not in state.codec_profiles.get(target, []):
            raise ValueError("Choose a reported codec profile")
        _set(
            updated,
            (
                target,
                "H264Profile" if target == "H264" else "Mpeg4Profile",
            ),
            changes.codec_profile,
        )
    if options is not None:
        state = state.model_copy(update={"encoding": target, "options": options})
    if changes.resolution is not None:
        if changes.resolution not in state.options.resolutions:
            raise ValueError("The camera did not advertise this resolution")
        _set(updated, ("Resolution", "Width"), changes.resolution.width)
        _set(updated, ("Resolution", "Height"), changes.resolution.height)
    for key, path in (
        ("frame_rate", ("RateControl", "FrameRateLimit")),
        ("bitrate_kbps", ("RateControl", "BitrateLimit")),
        ("quality", ("Quality",)),
        ("gop_length", (state.encoding, "GovLength")),
        ("encoding_interval", ("RateControl", "EncodingInterval")),
    ):
        value = getattr(changes, key)
        if value is None:
            continue
        if not _in_range(value, getattr(state.options, key)):
            raise ValueError(
                "An encoder setting is outside the camera's reported limits"
            )
        if key == "frame_rate" and not value.is_integer():
            raise ValueError(
                "This ONVIF Media1 encoder requires a whole-number frame rate"
            )
        _set(updated, path, int(value) if key == "frame_rate" else value)
    return updated


def imaging_state(settings: Any, options: Any, token: str) -> OnvifImagingState:
    """Expose supported standard imaging controls without inventing fallback ranges."""
    controls = []
    for key, path in IMAGING_PATHS.items():
        current = device_value(settings, *path)
        number = _number(current)
        value = (
            number
            if number is not None
            else current
            if isinstance(current, str)
            else None
        )
        option = device_value(options, *path)
        choices = (
            [item for item in option if isinstance(item, str)]
            if isinstance(option, (list, tuple))
            else []
        )
        limits = _range(option)
        limits_source = "camera"
        available = bool(choices) or limits is not None
        if key == "noise_reduction_level":
            available = False
            limits = None
            supported = device_value(
                options, "Extension", "Extension", "Extension", "NoiseReductionOptions", "Level"
            )
            if supported is True:
                # ONVIF NoiseReduction defines this normalized interval, not a vendor range.
                limits = OnvifNumberRange(minimum=0, maximum=1)
                limits_source = "onvif_standard"
                available = number is not None and 0 <= number <= 1
        controls.append(
            OnvifImagingControl(
                key=key,
                value=value,
                limits=limits,
                choices=choices,
                available=available,
                limits_source=limits_source,
            )
        )
    return OnvifImagingState(video_source_token=token, controls=controls)


def apply_imaging_changes(
    settings: Any,
    state: OnvifImagingState,
    changes: dict[str, DeviceValue],
    expected_values: dict[str, DeviceValue | None],
) -> Any:
    """Validate supported imaging values and detect conflicting edits."""
    controls = {control.key: control for control in state.controls}
    if set(changes) != set(expected_values):
        raise ValueError("Each changed imaging setting needs its previously read value")
    updated = copy.deepcopy(settings)
    for key, value in changes.items():
        control = controls.get(key)
        if control is None or not control.available:
            raise ValueError("The camera did not advertise this imaging control")
        mode_key = MANUAL_IMAGING_MODES.get(key)
        switching_from_auto = (
            mode_key is not None
            and mode_key in controls
            and controls[mode_key].value == "AUTO"
            and expected_values.get(mode_key) == "AUTO"
            and changes.get(mode_key) == "MANUAL"
        )
        # Auto-exposure/white-balance readings can drift while the user selects
        # manual settings. The unchanged mode, not that live reading, is the guard.
        if not switching_from_auto and control.value != expected_values[key]:
            raise OnvifSettingsConflict(
                "Camera imaging settings changed; refresh before applying"
            )
        if isinstance(value, str):
            if value not in control.choices:
                raise ValueError("The camera did not advertise this imaging value")
        elif not _in_range(value, control.limits):
            raise ValueError(
                "An imaging setting is outside the camera's reported limits"
            )
        _set(updated, IMAGING_PATHS[key], value)
    for key in ("exposure_time", "exposure_gain", "exposure_iris"):
        if key in changes and device_value(updated, "Exposure", "Mode") != "MANUAL":
            raise ValueError("Manual exposure values require manual exposure mode")
    for key in ("white_balance_cr_gain", "white_balance_cb_gain"):
        if key in changes and device_value(updated, "WhiteBalance", "Mode") != "MANUAL":
            raise ValueError("White-balance gains require manual white-balance mode")
    for group, minimum, maximum in (
        ("Exposure", "MinExposureTime", "MaxExposureTime"),
        ("Exposure", "MinGain", "MaxGain"),
        ("Exposure", "MinIris", "MaxIris"),
        ("Focus", "NearLimit", "FarLimit"),
    ):
        lower = _number(device_value(updated, group, minimum))
        upper = _number(device_value(updated, group, maximum))
        if lower is not None and upper is not None and lower > upper:
            raise ValueError(
                "Minimum imaging settings must not exceed maximum settings"
            )
    return updated


def build_imaging_patch(
    updated: Any, changes: dict[str, DeviceValue]
) -> dict[str, Any]:
    """Send only requested imaging fields and their required mode context."""
    patch: dict[str, Any] = {}
    for key in changes:
        path = IMAGING_PATHS[key]
        target = patch
        for part in path[:-1]:
            target = target.setdefault(part, {})
        target[path[-1]] = device_value(updated, *path)
    for group, field in (
        ("Exposure", "Mode"),
        ("WhiteBalance", "Mode"),
        ("Focus", "AutoFocusMode"),
        ("BacklightCompensation", "Mode"),
        ("WideDynamicRange", "Mode"),
    ):
        if group in patch:
            value = device_value(updated, group, field)
            if value is None:
                raise ValueError("The camera did not report a required imaging mode")
            patch[group][field] = value
    return patch
