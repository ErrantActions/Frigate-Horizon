"""Authorized modeled-weather and per-camera environmental automation status."""

import time
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from frigate.api.auth import (
    allow_any_authenticated,
    require_camera_access,
    require_role,
)
from frigate.api.defs.tags import Tags
from frigate.environment.runtime import CameraEnvironmentStatus, WeatherStatus

router = APIRouter(tags=[Tags.environment])


@dataclass(frozen=True)
class EnvironmentStatus:
    service_running: bool
    weather: WeatherStatus
    cameras: dict[str, CameraEnvironmentStatus]


def _unavailable() -> JSONResponse:
    return JSONResponse(
        content={
            "success": False,
            "message": "Environmental automation is unavailable",
        },
        status_code=503,
    )


@router.get(
    "/environment",
    response_model=EnvironmentStatus,
    dependencies=[Depends(require_role(["admin"]))],
)
def environment_status(request: Request):
    """Return complete automation status to administrators without location coordinates."""
    manager = getattr(request.app, "environment_manager", None)
    if manager is None:
        return _unavailable()
    return EnvironmentStatus(
        manager.is_alive(),
        manager.weather_status(datetime.now(UTC), time.monotonic()),
        manager.all_camera_statuses(),
    )


@router.get(
    "/environment/weather",
    response_model=WeatherStatus,
    dependencies=[Depends(allow_any_authenticated())],
)
def weather_status(request: Request):
    """Expose modeled values and freshness, never configured coordinates."""
    manager = getattr(request.app, "environment_manager", None)
    if manager is None:
        return _unavailable()
    return manager.weather_status(datetime.now(UTC), time.monotonic())


@router.get(
    "/environment/cameras/{camera_name}",
    response_model=CameraEnvironmentStatus,
    dependencies=[Depends(require_camera_access)],
)
def camera_environment_status(camera_name: str, request: Request):
    """Return only an authorized camera's desired and applied environmental state."""
    manager = getattr(request.app, "environment_manager", None)
    if manager is None:
        return _unavailable()
    if camera_name not in manager.camera_names():
        return JSONResponse(
            content={"success": False, "message": "Camera is not configured"},
            status_code=404,
        )
    try:
        return manager.camera_status(camera_name)
    except KeyError:
        return JSONResponse(
            content={"success": False, "message": "Camera is not configured"},
            status_code=404,
        )
