"""Recording storage aggregates without filesystem or archive network access."""

from typing import Any, TypedDict

from peewee import Case, fn

from frigate.models import Recordings


def storage_capacity(
    usage_bytes: int | None,
    budget_bytes: int | None,
    total_bytes: int | None,
    available_bytes: int | None,
    *,
    reserved_bytes: int = 0,
    written_reservation_bytes: int = 0,
    physical_reserved_bytes: int | None = None,
    reserve_bytes: int = 0,
    sampled_at: float | None = None,
    filesystem_id: str | None = None,
    state: str = "online",
) -> dict[str, Any]:
    """Separate content quota from mounted space, charging unwritten bytes once."""
    remaining = (
        max(0, budget_bytes - usage_bytes - reserved_bytes)
        if budget_bytes is not None and usage_bytes is not None
        else None
    )
    physical_reserved = (
        reserved_bytes if physical_reserved_bytes is None else physical_reserved_bytes
    )
    physical = (
        max(
            0,
            available_bytes
            - reserve_bytes
            - max(0, physical_reserved - written_reservation_bytes),
        )
        if available_bytes is not None and state == "online"
        else None
    )
    headroom = (
        min(remaining, physical)
        if remaining is not None and physical is not None
        else physical
        if budget_bytes is None
        else None
    )
    return {
        "state": state if total_bytes is not None else "unknown",
        "sampled_at": sampled_at,
        "filesystem_id": filesystem_id,
        "filesystem_total_bytes": total_bytes,
        "filesystem_available_bytes": available_bytes,
        "usage_bytes": usage_bytes,
        "budget_bytes": budget_bytes,
        "budget_remaining_bytes": remaining,
        "over_budget_bytes": (
            max(0, usage_bytes + reserved_bytes - budget_bytes)
            if usage_bytes is not None and budget_bytes is not None
            else None
        ),
        "reserved_bytes": reserved_bytes,
        "physical_reserved_bytes": physical_reserved,
        "written_reservation_bytes": written_reservation_bytes,
        "reserve_bytes": reserve_bytes,
        "effective_headroom_bytes": headroom,
        "protected_bytes": None,
    }


def invalidate_storage_capacity(capacity: dict[str, Any], state: str) -> dict[str, Any]:
    """Keep last observations visible without advertising unavailable headroom."""
    return {
        **capacity,
        "state": state,
        "effective_headroom_bytes": None,
    }


class RecordingStorageUsage(TypedDict):
    usage_mib: float
    recordings: int
    unknown_size_recordings: int


class CameraStorageUsage(TypedDict):
    friendly_name: str
    tiers: dict[str, RecordingStorageUsage]


def empty_recording_usage() -> RecordingStorageUsage:
    """Return an independent empty usage entry."""
    return {"usage_mib": 0.0, "recordings": 0, "unknown_size_recordings": 0}


def recording_storage_usage() -> tuple[
    dict[str, RecordingStorageUsage],
    dict[str, dict[str, RecordingStorageUsage]],
]:
    """Aggregate each camera/tier once and derive consistent global totals.

    Zero-size legacy rows are reported separately rather than implying their
    recording files consume no space. Paths are never dereferenced, so an
    unavailable NAS does not block storage information.
    """
    totals: dict[str, RecordingStorageUsage] = {}
    cameras: dict[str, dict[str, RecordingStorageUsage]] = {}
    size = Case(
        None,
        [
            (
                Recordings.storage_tier == "archive",
                Recordings.archive_bytes.cast("REAL") / (2**20),
            )
        ],
        Recordings.segment_size,
    )
    known = Case(
        None,
        [
            (
                Recordings.storage_tier == "archive",
                Recordings.archive_bytes.is_null(False)
                & (Recordings.archive_bytes >= 0),
            )
        ],
        Recordings.segment_size > 0,
    )
    for row in (
        Recordings.select(
            Recordings.camera,
            Recordings.storage_tier,
            fn.SUM(Case(None, [(known, size)], 0)).alias("usage"),
            fn.COUNT(Recordings.id).alias("recordings"),
            fn.SUM(
                Case(
                    None,
                    [
                        (
                            ~known | size.is_null() | (size < 0),
                            1,
                        )
                    ],
                    0,
                )
            ).alias("unknown"),
        )
        .group_by(Recordings.camera, Recordings.storage_tier)
        .dicts()
    ):
        camera = row["camera"]
        tier = row["storage_tier"]
        if not isinstance(camera, str) or not isinstance(tier, str):
            raise ValueError(
                "Recording storage contains invalid camera or tier metadata"
            )
        usage: RecordingStorageUsage = {
            "usage_mib": float(row["usage"]),
            "recordings": row["recordings"],
            "unknown_size_recordings": row["unknown"],
        }
        cameras.setdefault(camera, {})[tier] = usage
        total = totals.setdefault(tier, empty_recording_usage())
        total["usage_mib"] += usage["usage_mib"]
        total["recordings"] += usage["recordings"]
        total["unknown_size_recordings"] += usage["unknown_size_recordings"]
    return totals, cameras
