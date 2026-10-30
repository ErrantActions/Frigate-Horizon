"""Admin-only plugin installation, lifecycle and camera provisioning."""

import json
import logging
import re
import time
from typing import Any

import aiohttp
import zmq
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from peewee import fn
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from frigate.api.auth import require_role
from frigate.api.defs.request.app_body import AppConfigSetBody
from frigate.api.defs.tags import Tags
from frigate.comms.event_metadata_updater import EventMetadataTypeEnum
from frigate.models import Recordings
from frigate.plugins.inference import EventInferenceConfig
from frigate.plugins.manager import PluginManager
from frigate.plugins.packages import PluginError

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/plugins",
    tags=[Tags.plugins],
    dependencies=[Depends(require_role(["admin"]))],
)


class DeveloperModeBody(BaseModel):
    enabled: bool
    acknowledge_code_execution: bool = False


class InstallBody(BaseModel):
    bundled_ring: bool = False
    url: str | None = Field(default=None, max_length=2048)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    acknowledge_code_execution: bool = False


class EnableBody(BaseModel):
    enabled: bool
    acknowledge_camera_interruption: bool = False


class ActionBody(BaseModel):
    method: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict, repr=False)
    acknowledge_camera_interruption: bool = False


class AddCameraBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str = Field(min_length=1, max_length=128)
    camera: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    continuous_days: int = Field(default=3, ge=1, le=365)
    acknowledge_cloud_gaps: bool


def manager(request: Request) -> PluginManager:
    """Read the application-scoped owner instead of creating per-request workers."""
    if (
        getattr(request.app.state, "plugins_error", None)
        or request.app.state.plugins.service_error
    ):
        raise HTTPException(status_code=503, detail="Plugin state requires repair")
    return request.app.state.plugins


def failure(error: Exception) -> JSONResponse:
    """Never expose archive paths, provider responses or credential-bearing URLs."""
    code = str(error) if isinstance(error, PluginError) else "plugin_operation_failed"
    logger.warning("Plugin operation rejected (%s)", code)
    return JSONResponse({"success": False, "error": code}, status_code=400)


@router.get("")
async def list_plugins(request: Request):
    """List installed plugins and the bundled catalog without any account secrets."""
    result = manager(request).catalog()
    cameras = {
        camera for entry in result["installed"] for camera in entry["bindings"].values()
    }
    if cameras:

        def latest_recordings():
            return {
                row["camera"]: row["latest_end_time"]
                for row in Recordings.select(
                    Recordings.camera,
                    fn.MAX(Recordings.end_time).alias("latest_end_time"),
                )
                .where(Recordings.camera.in_(cameras))
                .group_by(Recordings.camera)
                .dicts()
            }

        latest = await run_in_threadpool(latest_recordings)
        now = time.time()
        for entry in result["installed"]:
            entry["recording_health"] = {
                camera: {
                    "latest_end_time": latest.get(camera),
                    "fresh": latest.get(camera) is not None
                    and now - latest[camera] < 60,
                }
                for camera in entry["bindings"].values()
            }
    return result


@router.put("/developer-mode")
async def developer_mode(request: Request, body: DeveloperModeBody):
    """Opt into explicitly tagged third-party code installation."""
    try:
        await manager(request).developer_mode(
            body.enabled, body.acknowledge_code_execution
        )
    except (PluginError, OSError) as error:
        return failure(error)
    return {"success": True}


@router.post("/install")
async def install_plugin(request: Request, body: InstallBody):
    """Install a bundled plugin or a checksum-pinned developer HTTPS ZIP."""
    if body.bundled_ring and (body.url is not None or body.sha256 is not None):
        return failure(PluginError("ambiguous_package_source"))
    try:
        await manager(request).install(
            body.bundled_ring, body.url, body.sha256, body.acknowledge_code_execution
        )
    except (
        PluginError,
        OSError,
        ValueError,
        ValidationError,
        aiohttp.ClientError,
        TimeoutError,
    ) as error:
        return failure(error)
    return {"success": True}


@router.put("/{plugin_id}/enabled")
async def enable_plugin(request: Request, plugin_id: str, body: EnableBody):
    """Enable a worker or explicitly acknowledge loss of its camera sources."""
    owner = manager(request)
    entry = owner.settings.plugins.get(plugin_id)
    if (
        not body.enabled
        and entry is not None
        and entry.bindings
        and not body.acknowledge_camera_interruption
    ):
        return failure(PluginError("camera_interruption_acknowledgement_required"))
    try:
        await owner.enable(plugin_id, body.enabled)
    except (PluginError, OSError, ValidationError, aiohttp.ClientError) as error:
        return failure(error)
    return {"success": True}


@router.delete("/{plugin_id}")
async def uninstall_plugin(request: Request, plugin_id: str):
    """Remove a stopped package and its credentials, never camera recordings."""
    try:
        await manager(request).uninstall(plugin_id)
    except (PluginError, OSError, aiohttp.ClientError) as error:
        return failure(error)
    return {"success": True}


@router.post("/{plugin_id}/actions")
async def plugin_action(request: Request, plugin_id: str, body: ActionBody):
    """Authenticate/configure through an advertised worker action."""
    entry = manager(request).settings.plugins.get(plugin_id)
    if (
        body.method == "disconnect"
        and entry is not None
        and entry.bindings
        and not body.acknowledge_camera_interruption
    ):
        return failure(PluginError("camera_interruption_acknowledgement_required"))
    if len(json.dumps(body.params).encode()) > 65536:
        return failure(PluginError("action_too_large"))
    try:
        return await manager(request).action(plugin_id, body.method, body.params)
    except (
        PluginError,
        OSError,
        ValidationError,
        aiohttp.ClientError,
        TimeoutError,
    ) as error:
        return failure(error)


@router.post("/{plugin_id}/cameras")
async def add_plugin_camera(request: Request, plugin_id: str, body: AddCameraBody):
    """Create only a new camera using the standard atomic config update path."""
    from frigate.api.app import config_set

    if not body.acknowledge_cloud_gaps:
        return failure(PluginError("cloud_recording_acknowledgement_required"))
    owner = manager(request)
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", plugin_id):
        return failure(PluginError("plugin_not_installed"))
    async with owner.lock:
        entry = owner.settings.plugins.get(plugin_id)
        if entry is None or not entry.enabled:
            return failure(PluginError("plugin_disabled"))
        try:
            stream = await owner.bind(plugin_id, body.device_id, body.camera)
        except (PluginError, OSError, aiohttp.ClientError) as error:
            return failure(error)
        camera_config = {
            "enabled": True,
            "friendly_name": owner.devices[plugin_id][body.device_id].name,
            "ffmpeg": {
                "inputs": [
                    {
                        "path": f"rtsp://127.0.0.1:8554/{stream}",
                        "input_args": "preset-rtsp-restream",
                        "roles": ["record"],
                    }
                ],
                "output_args": {"record": "preset-record-generic-audio-copy"},
            },
            "live": {"streams": {"Main": stream}},
            "detect": {"enabled": False, "width": 1280, "height": 720, "fps": 5},
            "motion": {"enabled": False},
            "audio": {"enabled": False},
            "snapshots": {"enabled": False},
            "record": {
                "enabled": True,
                "continuous": {"days": body.continuous_days},
            },
            "review": {
                "alerts": {"labels": ["doorbell"]},
                "detections": {"labels": ["motion"]},
            },
        }
        response = await run_in_threadpool(
            config_set,
            request,
            AppConfigSetBody(
                requires_restart=0,
                expected_absent_cameras=[body.camera],
                update_topic=f"config/cameras/{body.camera}/add",
                config_data={"cameras": {body.camera: camera_config}},
            ),
        )
        if response.status_code != 200:
            await owner.unbind(plugin_id, body.device_id)
            return response
    return {"success": True, "camera": body.camera, "stream": stream}


@router.put("/{plugin_id}/cameras/{camera}/event-inference")
async def configure_event_inference(
    request: Request, plugin_id: str, camera: str, body: EventInferenceConfig
):
    """Wake a separate AI decoder only for new motion events, not recordings."""
    from frigate.api.app import config_set
    from frigate.motion.record_only import plugin_recording_only

    owner = manager(request)
    async with owner.lock:
        entry = owner.settings.plugins.get(plugin_id)
        config = request.app.frigate_config
        if (
            entry is None
            or camera not in entry.bindings.values()
            or camera not in config.cameras
            or not plugin_recording_only(camera)
        ):
            return failure(PluginError("camera_not_bound"))
        if body.enabled and (not entry.enabled or not config.cameras[camera].enabled):
            return failure(PluginError("plugin_or_camera_disabled"))
        if body.enabled and "motion" not in entry.manifest.capabilities:
            return failure(PluginError("motion_not_supported"))
        if body.enabled:
            review = config.cameras[camera].review
            alert_labels = sorted(
                set(review.alerts.labels) | set(config.review.alerts.labels)
            )
            review_updates = {"alerts": {"labels": alert_labels}}
            if review.detections.labels is not None:
                inherited_labels = config.review.detections.labels
                review_updates["detections"] = {
                    "labels": sorted(
                        set(review.detections.labels)
                        | set(
                            config.cameras[camera].objects.track
                            if inherited_labels is None
                            else inherited_labels
                        )
                    )
                }
            response = await run_in_threadpool(
                config_set,
                request,
                AppConfigSetBody(
                    requires_restart=0,
                    update_topic=f"config/cameras/{camera}/review",
                    config_data={"cameras": {camera: {"review": review_updates}}},
                ),
            )
            if response.status_code != 200:
                return response
        try:
            await owner.configure_inference(plugin_id, camera, body)
        except (PluginError, OSError, ValueError) as error:
            return failure(error)
    return {"success": True, "camera": camera, **owner.inference.status(camera)}


def publish_plugin_event(app, payload: tuple) -> None:
    """Use the existing manual-event pipeline with explicit plugin provenance."""
    try:
        app.event_metadata_updater.publish(
            payload, EventMetadataTypeEnum.manual_event_create.value
        )
    except zmq.ZMQError:
        logger.error("Plugin event delivery failed")
        raise PluginError("event_delivery_failed") from None
