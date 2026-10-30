"""Explicitly selected portable investigation context, excluding runtime secrets."""

import datetime
import json
import math
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any


class ExportMetadataCategory(str, Enum):
    camera = "camera"
    events = "events"
    identities = "identities"
    geometry = "geometry"
    environment = "environment"
    notes = "notes"
    provenance = "provenance"


MAX_METADATA_EVENTS = 10000
MAX_METADATA_BYTES = 2 * 2**20


def investigation_metadata(
    categories: Sequence[ExportMetadataCategory],
    *,
    camera: str,
    camera_name: str,
    after: float,
    before: float,
    timezone: str,
    version: str,
    events: Sequence[Mapping[str, Any]],
    timeline: Sequence[Mapping[str, Any]],
    timestamp_mode: str,
    media_timeline: Sequence[Mapping[str, Any]] = (),
    notes: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Project selected data; unavailable context is never guessed or fabricated."""
    if not all(math.isfinite(value) for value in (after, before)) or before <= after:
        raise ValueError("Invalid investigation metadata range")
    if len(events) > MAX_METADATA_EVENTS or len(timeline) > MAX_METADATA_EVENTS:
        raise ValueError(
            "Investigation metadata exceeds the event limit; shorten the export"
        )
    selected = set(categories)
    result: dict[str, Any] = {
        "schema": "frigate-investigation-v1",
        "selected_categories": sorted(value.value for value in selected),
        "timing": {
            "source": "frigate_recording_timeline",
            "confidence": "inferred",
            "camera_clock_verified": False,
            "timezone": timezone,
            "capture_range": {"after": after, "before": before},
        },
        "unavailable": {},
        "media_timeline": list(media_timeline),
    }
    if ExportMetadataCategory.camera in selected:
        result["camera"] = {"id": camera, "display_name": camera_name}
        result["unavailable"]["physical_location"] = "No verified capture-time location"
    if ExportMetadataCategory.provenance in selected:
        result["provenance"] = {
            "frigate_version": version,
            "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "timestamp_mode": timestamp_mode,
            "original_recordings_modified": False,
            "authenticity_verified": False,
        }
    included = selected.intersection(
        {
            ExportMetadataCategory.events,
            ExportMetadataCategory.identities,
            ExportMetadataCategory.geometry,
        }
    )
    if included:
        projected = []
        for event in events:
            entry = {
                "id": event["id"],
                "start_time": event["start_time"],
                "end_time": event["end_time"],
            }
            data = event.get("data") or {}
            if not isinstance(data, Mapping):
                raise ValueError("Invalid stored event metadata")
            if ExportMetadataCategory.events in included:
                entry.update(
                    {
                        "label": event.get("label"),
                        "zones": event.get("zones"),
                        "confidence": data.get("top_score", data.get("score")),
                        "false_positive": event.get("false_positive"),
                    }
                )
            if ExportMetadataCategory.identities in included:
                entry["identity"] = {
                    key: value
                    for key, value in {
                        "sub_label": event.get("sub_label"),
                        "recognized_license_plate": data.get(
                            "recognized_license_plate"
                        ),
                        "recognized_license_plate_score": data.get(
                            "recognized_license_plate_score"
                        ),
                    }.items()
                    if value is not None
                }
            if ExportMetadataCategory.geometry in included:
                entry["geometry"] = {
                    key: data[key]
                    for key in (
                        "box",
                        "region",
                        "path_data",
                        "velocity",
                        "position_changes",
                    )
                    if key in data
                }
            projected.append(entry)
        result["events"] = projected
        if ExportMetadataCategory.events in included:
            if any(
                row.get("data") and not isinstance(row["data"], Mapping)
                for row in timeline
            ):
                raise ValueError("Invalid stored timeline metadata")
            result["timeline"] = [
                {
                    "timestamp": row["timestamp"],
                    "source": row["source"],
                    "source_id": row["source_id"],
                    "class_type": row["class_type"],
                    "data": {
                        key: value
                        for key, value in (row.get("data") or {}).items()
                        if key
                        in ("label", "zones", "entered_zones", "stationary", "score")
                    },
                }
                for row in timeline
            ]
        if ExportMetadataCategory.geometry in included:
            result["unavailable"]["per_frame_geometry"] = (
                "Stored object summaries are not frame-accurate tracks"
            )
    if ExportMetadataCategory.environment in selected:
        result["unavailable"]["environment"] = (
            "No verified capture-time environmental samples attached to these recordings"
        )
    if ExportMetadataCategory.notes in selected:
        if notes is None:
            result["unavailable"]["notes"] = (
                "Incident notes were not available for this export"
            )
        else:
            if len(notes) > MAX_METADATA_EVENTS:
                raise ValueError(
                    "Investigation metadata exceeds the note limit; shorten the export"
                )
            result["notes"] = [
                {
                    key: note.get(key)
                    for key in (
                        "id",
                        "timestamp",
                        "end_time",
                        "title",
                        "notes",
                        "owner",
                        "created_at",
                        "updated_at",
                        "revision",
                    )
                }
                for note in notes
            ]
    # A hard bound prevents giant MP4 metadata atoms or silently truncated evidence.
    metadata_json(result)
    return result


def metadata_json(value: Mapping[str, Any]) -> str:
    """Serialize bounded interoperable metadata without NaN or infinity."""
    serialized = json.dumps(
        value, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    )
    if len(serialized.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError(
            "Investigation metadata exceeds the size limit; shorten the export"
        )
    return serialized


def metadata_text(value: Mapping[str, Any]) -> str:
    """Escape a generated JSON comment for FFmpeg's metadata demuxer."""
    encoded = metadata_json(value)
    for character in ("\\", "=", ";", "#", "\n"):
        encoded = encoded.replace(character, "\\" + character)
    return ";FFMETADATA1\ncomment=" + encoded + "\n"


def metadata_mux_command(
    ffmpeg: str, source: str, metadata: str, output: str
) -> list[str]:
    """Keep existing streams and chapters while attaching the selected JSON comment."""
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-i",
        source,
        "-f",
        "ffmetadata",
        "-i",
        metadata,
        "-map",
        "0",
        "-map_metadata",
        "0",
        "-map_metadata:g",
        "1:g",
        "-map_chapters",
        "0",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        output,
    ]
