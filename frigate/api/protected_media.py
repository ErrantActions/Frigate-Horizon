"""Camera-authorized permanent evidence library using existing saved events."""

from math import isfinite
from time import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from peewee import Case, ColumnBase, Expression, ModelSelect, Value

from frigate.api.auth import allow_any_authenticated, get_current_user
from frigate.api.defs.response.protected_media_response import (
    ProtectedMediaItem,
    ProtectedMediaResponse,
    ProtectedStorageUsage,
)
from frigate.api.defs.tags import Tags
from frigate.api.media_auth import check_camera_access
from frigate.models import Event, Recordings
from frigate.record.policy import CapturePadding

router = APIRouter(tags=[Tags.recordings])


async def protected_camera_scope(request: Request) -> list[str]:
    """Include historical cameras only when canonical media ACLs permit access."""
    user = await get_current_user(request)
    if isinstance(user, JSONResponse):
        raise HTTPException(
            status_code=user.status_code, detail="Authentication required"
        )
    config = request.app.frigate_config
    role = user["role"]
    if role != "admin" and role not in config.auth.roles:
        raise HTTPException(status_code=403, detail="Access denied")
    candidates = (
        Event.select(Event.camera)
        .where(Event.retain_indefinitely == True)
        .distinct()
        .tuples()
    )
    return sorted(
        camera for (camera,) in candidates if check_camera_access(role, camera, config)
    )


def _capture_padding(request: Request) -> tuple[ColumnBase, ColumnBase]:
    config = request.app.frigate_config
    default = CapturePadding.from_record(config.record)
    padding = {
        name: CapturePadding.from_record(camera.record)
        for name, camera in config.cameras.items()
    }
    if not padding:
        return Value(default.pre), Value(default.post)
    return (
        Case(
            Event.camera,
            [(name, value.pre) for name, value in padding.items()],
            default.pre,
        ),
        Case(
            Event.camera,
            [(name, value.post) for name, value in padding.items()],
            default.post,
        ),
    )


def _recordings_for_events(
    clause: Expression, pre: ColumnBase, post: ColumnBase
) -> ModelSelect:
    # DISTINCT belongs to physical recording rows, not event counts or sizes.
    return (
        Recordings.select(
            Recordings.id,
            Recordings.storage_tier,
            Recordings.archive_bytes,
            Recordings.segment_size,
            Recordings.delete_requested,
        )
        .join(
            Event,
            on=(
                (Recordings.camera == Event.camera)
                & (Recordings.end_time >= Event.start_time - pre)
                & (
                    Event.end_time.is_null()
                    | (Recordings.start_time <= Event.end_time + post)
                )
            ),
        )
        .where(clause)
        .distinct()
    )


def _storage_usage(recordings: ModelSelect) -> dict[str, ProtectedStorageUsage]:
    usage = {tier: ProtectedStorageUsage() for tier in ("local", "archive")}
    for recording in recordings.iterator():
        tier = usage.setdefault(recording.storage_tier, ProtectedStorageUsage())
        tier.recordings += 1
        size = recording.archive_bytes if recording.storage_tier == "archive" else None
        if (
            size is None
            and recording.segment_size
            and isfinite(recording.segment_size)
            and recording.segment_size > 0
        ):
            size = int(recording.segment_size * 2**20)
        if size is None or size <= 0:
            tier.unknown_size_recordings += 1
        else:
            tier.known_bytes += size
        if recording.storage_tier == "local" and recording.archive_bytes is not None:
            archived = usage["archive"]
            archived.recordings += 1
            if recording.archive_bytes < 0:
                archived.unknown_size_recordings += 1
            else:
                archived.known_bytes += recording.archive_bytes
    for tier in usage.values():
        tier.total_bytes = None if tier.unknown_size_recordings else tier.known_bytes
    return usage


@router.get(
    "/protected-media",
    response_model=ProtectedMediaResponse,
    dependencies=[Depends(allow_any_authenticated())],
    summary="Browse permanently protected evidence",
    description=(
        "Lists saved events across the complete retained history, including events "
        "whose clip flag was cleared. Totals count distinct physical recording rows "
        "for the entire filtered result, not only this page. Sizes and availability "
        "come from database metadata and cached worker health, never NAS probes. "
        "Temporary recording leases are not permanent evidence."
    ),
)
def protected_media(
    request: Request,
    cameras: list[str] = Depends(protected_camera_scope),
    camera: str | None = None,
    after: float | None = Query(None, allow_inf_nan=False),
    before: float | None = Query(None, allow_inf_nan=False),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Return scoped saved events, physical tier totals and cached availability."""
    if after is not None and before is not None and after > before:
        raise HTTPException(status_code=422, detail="Invalid time range")
    clause = (Event.retain_indefinitely == True) & Event.camera.in_(cameras)
    if camera is not None:
        if camera not in cameras:
            raise HTTPException(status_code=403, detail="Access denied to camera")
        clause &= Event.camera == camera
    if after is not None:
        clause &= Event.end_time.is_null() | (Event.end_time >= after)
    if before is not None:
        clause &= Event.start_time <= before
    query = Event.select(
        Event.id,
        Event.camera,
        Event.start_time,
        Event.end_time,
        Event.label,
        Event.has_snapshot,
    ).where(clause)
    pre, post = _capture_padding(request)
    health = request.app.storage_maintainer.get_archive_status()
    state = str(health.get("state", "unknown"))
    updated = health.get("updated_at")
    updated = (
        updated if isinstance(updated, (float, int)) and isfinite(updated) else None
    )
    archive_online = (
        state in ("ready", "moving")
        and updated is not None
        and 0 <= time() - updated <= 60
        and not health.get("settings_pending", False)
    )
    items = []
    for event in (
        query.order_by(Event.start_time.desc(), Event.id.desc())
        .limit(limit)
        .offset(offset)
    ):
        recording_count = 0
        tier_names: set[str] = set()
        deleting = False
        archive_required = False
        for recording in _recordings_for_events(
            (Event.id == event.id) & (Event.retain_indefinitely == True), pre, post
        ).iterator():
            recording_count += 1
            tier_names.add(recording.storage_tier)
            if recording.archive_bytes is not None:
                tier_names.add("archive")
            deleting |= recording.delete_requested
            archive_required |= recording.storage_tier == "archive"
        tiers = sorted(tier_names)
        if recording_count == 0:
            availability = "missing"
        elif deleting:
            availability = "deleting"
        elif archive_required and not archive_online:
            availability = "archive_unavailable"
        elif archive_required:
            availability = "archive_online"
        else:
            availability = "indexed_local"
        camera_path = quote(event.camera, safe="")
        event_path = quote(event.id, safe="")
        original_url = None
        if event.end_time is not None and availability in (
            "indexed_local",
            "archive_online",
        ):
            original_url = (
                f"{camera_path}/start/{event.start_time}/end/{event.end_time}/clip.mp4"
            )
        items.append(
            ProtectedMediaItem(
                id=event.id,
                camera=event.camera,
                start_time=event.start_time,
                end_time=event.end_time,
                label=event.label,
                thumbnail_url=f"events/{event_path}/thumbnail.jpg",
                snapshot_url=f"events/{event_path}/snapshot.jpg"
                if event.has_snapshot
                else None,
                original_url=original_url,
                availability=availability,
                tiers=tiers,
                recordings=recording_count,
                has_snapshot=event.has_snapshot,
            )
        )
    return ProtectedMediaResponse(
        items=items,
        total=query.count(),
        limit=limit,
        offset=offset,
        cameras=cameras,
        storage=_storage_usage(_recordings_for_events(clause, pre, post)),
        archive_state=state,
        archive_updated_at=updated,
    )
