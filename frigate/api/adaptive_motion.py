"""Camera-authorized, local-cache-only adaptive motion diagnostics."""

import asyncio
import json
import logging
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from frigate.api.auth import require_camera_access
from frigate.api.defs.tags import Tags
from frigate.const import REPLAY_CAMERA_PREFIX
from frigate.motion.adaptive_runtime import atomic_json, cache_path

logger = logging.getLogger(__name__)
router = APIRouter(tags=[Tags.camera])


@router.get(
    "/{camera_name}/adaptive-motion",
    dependencies=[Depends(require_camera_access)],
)
async def adaptive_motion_status(
    camera_name: str,
    request: Request,
    field: Literal["adjustment", "activity", "effective", "p", "i", "d"] = "adjustment",
    resolution: int = Query(64, ge=4, le=100),
    column: int | None = Query(None, ge=0, le=8191),
    row: int | None = Query(None, ge=0, le=8191),
):
    """Request bounded diagnostic snapshots without exposing persisted controllers."""
    config = request.app.frigate_config
    camera = config.cameras.get(camera_name)
    if camera is None:
        raise HTTPException(404, "Camera is not configured")
    if camera_name.startswith(REPLAY_CAMERA_PREFIX):
        raise HTTPException(422, "Adaptive motion is unavailable for replay cameras")
    settings = camera.motion.adaptive
    if (column is None) != (row is None):
        raise HTTPException(422, "Both row and column are required")
    if (
        column is not None
        and row is not None
        and (column >= settings.columns or row >= settings.rows)
    ):
        raise HTTPException(422, "Cell is outside the configured grid")
    output = {
        "camera": camera_name,
        "mode": settings.mode if settings.enabled else "disabled",
        "status": "warming_up" if settings.enabled else "disabled",
        "updated_at": None,
        "baseline_threshold": camera.motion.threshold,
        "max_adjustment": settings.max_adjustment,
        "applied": False,
        "positive_holds": 0,
        "warning": None,
        "error": None,
        "profile": None,
        "grid": {
            "columns": settings.columns,
            "rows": settings.rows,
            "analysis_width": 0,
            "analysis_height": 0,
            "display_columns": 0,
            "display_rows": 0,
            "values": [],
            "field": field,
        },
    }
    if settings.enabled:

        def snapshot():
            atomic_json(
                cache_path(camera_name, "request"),
                {
                    "timestamp": time.time(),
                    "field": field,
                    "resolution": resolution,
                    "column": column,
                    "row": row,
                },
            )
            try:
                path = cache_path(camera_name, "snapshot")
                if path.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError("Adaptive diagnostic snapshot exceeds its limit")
                return json.loads(path.read_text())
            except FileNotFoundError:
                return None

        try:
            cached = await asyncio.to_thread(snapshot)
        except (OSError, ValueError):
            logger.exception(
                "Unable to read adaptive motion diagnostics for %s", camera_name
            )
            raise HTTPException(
                503, "Adaptive motion diagnostics are unavailable"
            ) from None
        if cached is not None:
            if cached.get("camera") != camera_name:
                raise HTTPException(503, "Adaptive motion diagnostic identity mismatch")
            # The worker acknowledges the requested view asynchronously; never
            # label an old field/cell as the newly selected one.
            same_cell = column is None or (
                cached.get("cell", {}).get("column"),
                cached.get("cell", {}).get("row"),
            ) == (column, row)
            if (
                cached.get("grid", {}).get("field") == field
                and same_cell
                and cached.get("grid", {}).get("columns") == settings.columns
                and cached.get("grid", {}).get("rows") == settings.rows
                and cached.get("mode") == settings.mode
            ):
                output = cached
                if not 0 <= time.time() - cached.get("updated_at", 0) < 10:
                    output["status"] = "stale"
                    output["applied"] = False
    return JSONResponse(output, headers={"Cache-Control": "private, no-store"})
