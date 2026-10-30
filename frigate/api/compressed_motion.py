"""Administrator-only live vector inspection with explicit bounded source start and stop."""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from frigate.api.auth import require_camera_access, require_role
from frigate.api.defs.tags import Tags
from frigate.comms.reolink_api import native_api_selected
from frigate.config import CameraConfig
from frigate.const import REPLAY_CAMERA_PREFIX
from frigate.motion.compressed_preview import VectorPreviewOptions
from frigate.motion.record_only import timing_calibration_options

logger = logging.getLogger(__name__)
router = APIRouter(
    tags=[Tags.camera],
    dependencies=[Depends(require_role(["admin"])), Depends(require_camera_access)],
)


class TimingCalibrationRequest(BaseModel):
    """A short original-detector reference, never a permanent detection setting."""

    model_config = ConfigDict(extra="forbid")
    duration_seconds: int = Field(default=900, ge=30, le=900, strict=True)


def camera_config(request: Request, camera_name: str) -> CameraConfig:
    """Require an enabled real camera; never accept arbitrary source URLs."""
    camera = request.app.frigate_config.cameras.get(camera_name)
    if camera is None:
        raise HTTPException(404, "Camera is not configured")
    if not camera.enabled or camera_name.startswith(REPLAY_CAMERA_PREFIX):
        raise HTTPException(422, "Preview requires an enabled real camera")
    return camera


def timing_tuner(request: Request, camera_name: str):
    """Keep calibration status and control scoped to the operator-selected camera."""
    camera_config(request, camera_name)
    options = timing_calibration_options()
    if options is None or options.camera != camera_name:
        raise HTTPException(404, "Timing calibration is not enabled for this camera")
    return request.app.state.timing_calibration


@router.get("/{camera_name}/timing-calibration")
async def timing_calibration_status(camera_name: str, request: Request):
    """Read bounded temporary-tuner progress without starting a decoder."""
    tuner = timing_tuner(request, camera_name)
    return JSONResponse(
        {**tuner.snapshot(), "camera": camera_name},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/{camera_name}/timing-calibration")
async def start_timing_calibration(
    camera_name: str, request: Request, options: TimingCalibrationRequest
):
    """Temporarily run the original detector for one authorized timing reference."""
    tuner = timing_tuner(request, camera_name)
    try:
        result = await asyncio.to_thread(
            tuner.start, camera_name, options.duration_seconds
        )
    except ValueError:
        raise HTTPException(422, "Invalid timing calibration selection") from None
    except RuntimeError:
        raise HTTPException(409, "A timing calibration is already running") from None
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.delete("/{camera_name}/timing-calibration")
async def stop_timing_calibration(camera_name: str, request: Request):
    """Cancel the temporary reference and restore decoder-disabled capture."""
    tuner = timing_tuner(request, camera_name)
    await asyncio.to_thread(tuner.close)
    return JSONResponse(tuner.snapshot(), headers={"Cache-Control": "no-store"})


@router.get("/{camera_name}/compressed-motion-preview")
async def vector_preview_status(
    camera_name: str,
    request: Request,
    session: str | None = Query(None, min_length=32, max_length=32),
):
    """Read availability or an existing preview without starting processing."""
    camera_config(request, camera_name)
    try:
        result = await asyncio.to_thread(
            request.app.state.vector_previews.status, camera_name, session
        )
    except KeyError:
        raise HTTPException(404, "Vector preview session is unavailable") from None
    if native_api_selected(camera_name):
        result["native_detection"] = request.app.state.reolink_events.snapshot(
            camera_name
        )
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/{camera_name}/compressed-motion-preview")
async def start_vector_preview(
    camera_name: str, request: Request, options: VectorPreviewOptions
):
    """Explicitly start a temporary compressed-only source, not the detection pipeline."""
    camera = camera_config(request, camera_name)
    try:
        result = await asyncio.to_thread(
            request.app.state.vector_previews.start, camera, options
        )
    except FileNotFoundError:
        raise HTTPException(
            503, "Native vector preview is not installed in this build"
        ) from None
    except RuntimeError:
        raise HTTPException(409, "Another vector preview is running") from None
    except (OSError, ValueError):
        logger.exception(
            "Unable to start compressed vector preview for %s", camera_name
        )
        raise HTTPException(503, "Unable to start vector preview") from None
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.delete("/{camera_name}/compressed-motion-preview")
async def stop_vector_preview(
    camera_name: str,
    request: Request,
    session: str = Query(..., min_length=32, max_length=32),
):
    """Stop only the explicitly identified preview."""
    camera_config(request, camera_name)
    try:
        await asyncio.to_thread(
            request.app.state.vector_previews.stop, camera_name, session
        )
    except KeyError:
        raise HTTPException(404, "Vector preview session is unavailable") from None
    return JSONResponse({"success": True}, headers={"Cache-Control": "no-store"})
