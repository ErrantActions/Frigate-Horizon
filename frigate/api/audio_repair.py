"""Admin-only, explicitly user-started recording audio maintenance."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from frigate.api.auth import require_role
from frigate.api.defs.request.audio_repair_body import (
    AudioRepairScanBody,
    AudioRepairStartBody,
)
from frigate.api.defs.response.audio_repair_response import (
    AudioRepairItemsPage,
    AudioRepairItemState,
    AudioRepairJob,
    AudioRepairJobsPage,
)
from frigate.api.defs.tags import Tags
from frigate.record.audio_repair_manager import (
    AudioRepairManager,
    RepairJobConflict,
    RepairJobNotFound,
)

router = APIRouter(
    prefix="/recordings/audio-repair/jobs",
    tags=[Tags.recordings],
    dependencies=[Depends(require_role(["admin"]))],
)


def _manager(request: Request) -> AudioRepairManager:
    manager = getattr(request.app.state, "audio_repair_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="Audio maintenance is unavailable")
    return manager


def _call(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except RepairJobNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except RepairJobConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("", response_model=AudioRepairJob, status_code=202)
def create_audio_repair_scan(request: Request, body: AudioRepairScanBody):
    """Scan indexed recordings without modifying media."""
    return _call(_manager(request).create_scan, body)


@router.get("", response_model=AudioRepairJobsPage)
def list_audio_repair_jobs(
    request: Request,
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Discover recent and active persistent jobs after page navigation."""
    return _call(_manager(request).list_jobs, limit, offset)


@router.get("/{job_id}", response_model=AudioRepairJob)
def get_audio_repair_job(request: Request, job_id: str):
    """Return a job's safe counters and current item."""
    return _call(_manager(request).get_job, job_id)


@router.get("/{job_id}/items", response_model=AudioRepairItemsPage)
def list_audio_repair_items(
    request: Request,
    job_id: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    state: AudioRepairItemState | None = None,
):
    """Page candidates and explicit skip/failure reasons."""
    return _call(_manager(request).list_items, job_id, limit, offset, state)


@router.post("/{job_id}/start", response_model=AudioRepairJob, status_code=202)
def start_audio_repairs(request: Request, job_id: str, body: AudioRepairStartBody):
    """Start only user-selected candidates or explicitly confirmed all-eligible."""
    return _call(_manager(request).start_job, job_id, body)


@router.post("/{job_id}/pause", response_model=AudioRepairJob)
def pause_audio_repairs(request: Request, job_id: str):
    """Pause scanning or repairing after the current safe file boundary."""
    return _call(_manager(request).pause, job_id)


@router.post("/{job_id}/resume", response_model=AudioRepairJob, status_code=202)
def resume_audio_repairs(request: Request, job_id: str):
    """Explicitly resume a paused scan or repair phase."""
    return _call(_manager(request).resume, job_id)


@router.post("/{job_id}/stop", response_model=AudioRepairJob)
def stop_audio_repairs(request: Request, job_id: str):
    """Cancel the current tool before publication and retain completed repairs."""
    return _call(_manager(request).stop, job_id)
