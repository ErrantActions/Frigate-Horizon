"""Administrator-only bounded older-footage GPU compression."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from filelock import Timeout

from frigate.api.auth import require_role
from frigate.api.defs.request.compression_body import (
    CompressionControlBody,
    CompressionStartBody,
)
from frigate.api.defs.response.compression_response import (
    CompressionItems,
    CompressionJob,
    CompressionJobs,
)
from frigate.api.defs.tags import Tags
from frigate.record.compression_manager import (
    CompressionConflict,
    CompressionManager,
    CompressionNotFound,
)

router = APIRouter(
    prefix="/recordings/compression/jobs",
    tags=[Tags.recordings],
    dependencies=[Depends(require_role(["admin"]))],
)


def _manager(request: Request) -> CompressionManager:
    manager = getattr(request.app.state, "compression_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="Compression is unavailable")
    return manager


def _call(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except CompressionNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except CompressionConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Timeout as error:
        raise HTTPException(
            status_code=409, detail="Compression controls are busy"
        ) from error


@router.get("", response_model=CompressionJobs)
def list_compression_jobs(request: Request):
    """List recent jobs and truthful activation gates."""
    return _call(_manager(request).list_jobs)


@router.post("", response_model=CompressionJob, status_code=202)
def start_compression_job(request: Request, body: CompressionStartBody):
    """Explicitly authorize one bounded batch using the current policy."""
    return _call(_manager(request).create_job)


@router.get("/{job_id}", response_model=CompressionJob)
def get_compression_job(request: Request, job_id: str):
    """Read persistent job progress."""
    return _call(_manager(request).get_job, job_id)


@router.get("/{job_id}/items", response_model=CompressionItems)
def list_compression_items(
    request: Request,
    job_id: str,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Read bounded audit results without exposing paths."""
    return _call(_manager(request).list_items, job_id, limit, offset)


@router.post("/{job_id}/control", response_model=CompressionJob)
def control_compression_job(
    request: Request, job_id: str, body: CompressionControlBody
):
    """Pause/resume at file boundaries or stop before publication."""
    return _call(_manager(request).control, job_id, body.action)
