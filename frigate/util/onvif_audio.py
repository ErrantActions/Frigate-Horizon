"""Normalize and validate only camera-reported audio encoder combinations."""

import copy
import hashlib
import json

from frigate.api.defs.request.onvif_body import OnvifAudioChanges
from frigate.util.onvif_device import OnvifSettingsConflict, _set, device_value


def _positive_integers(value):
    values = device_value(value, "Items")
    if values is None:
        values = value if isinstance(value, (list, tuple)) else []
    return sorted(
        {
            item
            for item in values
            if isinstance(item, int) and not isinstance(item, bool) and item > 0
        }
    )


def audio_state(configuration, options, shared_profiles, source_token):
    """Keep per-codec option rows separate rather than inventing combinations."""
    reported = []
    rows = (
        options
        if isinstance(options, (list, tuple))
        else device_value(options, "Options") or []
    )
    for option in rows:
        encoding = device_value(option, "Encoding")
        if not isinstance(encoding, str) or not encoding:
            continue
        reported.append(
            {
                "encoding": encoding,
                "bitrates_kbps": _positive_integers(
                    device_value(option, "BitrateList")
                ),
                "sample_rates_khz": _positive_integers(
                    device_value(option, "SampleRateList")
                ),
            }
        )
    state = {
        "token": device_value(configuration, "token"),
        "audio_source_token": source_token,
        "encoding": device_value(configuration, "Encoding"),
        "bitrate_kbps": device_value(configuration, "Bitrate"),
        "sample_rate_khz": device_value(configuration, "SampleRate"),
        "options": reported,
        "shared_profiles": shared_profiles,
        "editable": any(
            row["bitrates_kbps"] and row["sample_rates_khz"] for row in reported
        ),
    }
    state["revision"] = hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return state


def apply_audio_changes(configuration, state, changes: OnvifAudioChanges, revision):
    """Validate an entire target combination before preserving unrelated fields."""
    if revision != state["revision"]:
        raise OnvifSettingsConflict(
            "The audio encoder changed; refresh before applying"
        )
    selected = {
        key: getattr(changes, key) if getattr(changes, key) is not None else state[key]
        for key in ("encoding", "bitrate_kbps", "sample_rate_khz")
    }
    if not any(
        row["encoding"] == selected["encoding"]
        and selected["bitrate_kbps"] in row["bitrates_kbps"]
        and selected["sample_rate_khz"] in row["sample_rates_khz"]
        for row in state["options"]
    ):
        raise ValueError(
            "Choose a camera-reported audio codec, bitrate and sample rate"
        )
    updated = copy.deepcopy(configuration)
    for key, field in (
        ("encoding", "Encoding"),
        ("bitrate_kbps", "Bitrate"),
        ("sample_rate_khz", "SampleRate"),
    ):
        _set(updated, (field,), selected[key])
    return updated
