"""Versioned, validated plugin manifests and worker messages."""

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger(__name__)


class PluginManifest(BaseModel):
    """Describe a Python worker without executable installation hooks."""

    model_config = ConfigDict(extra="forbid")
    api_version: Literal[1]
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9.-]+)?$")
    description: str = Field(max_length=1000)
    entrypoint: Literal["plugin.py"] = "plugin.py"
    capabilities: list[Literal["camera", "motion", "doorbell", "two_way_audio"]]
    actions: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="before")
    @classmethod
    def migrate_inactive_host_metadata(cls, value: Any) -> Any:
        """Normalize newer hosts' default metadata for unchanged version-1 workers."""
        if not isinstance(value, dict) or value.get("api_version") != 1:
            return value
        defaults = {
            "runtime": "python",
            "platform": None,
            "architecture": None,
            "configuration": [],
        }
        present = defaults.keys() & value.keys()
        if not present or any(value[key] != defaults[key] for key in present):
            return value
        logger.warning("Normalized inactive version-1 plugin manifest host metadata")
        return {key: item for key, item in value.items() if key not in present}


class PluginDevice(BaseModel):
    """A provider device with a source consumed only by the media bridge."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=100)
    source: str = Field(min_length=1, max_length=16384, repr=False)
    two_way_audio: bool = False


class PluginEvent(BaseModel):
    """Camera-reported activity, never an invented object detection."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    kind: Literal["motion", "doorbell"]
    timestamp: float


class PluginSnapshot(BaseModel):
    """The complete device inventory and new events returned by a worker."""

    model_config = ConfigDict(extra="forbid")
    state: Literal["running", "auth_required", "two_factor_required", "degraded"]
    devices: list[PluginDevice] = Field(default_factory=list, max_length=100)
    events: list[PluginEvent] = Field(default_factory=list, max_length=100)


class PluginEntry(BaseModel):
    """Persist only package identity, lifecycle intent and camera bindings."""

    model_config = ConfigDict(extra="forbid")
    manifest: PluginManifest
    enabled: bool = False
    developer: bool = False
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    bindings: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def migrate_inactive_host_metadata(cls, value: Any) -> Any:
        """Normalize only unused notification metadata without losing subscriptions."""
        if not isinstance(value, dict):
            return value
        manifest = value.get("manifest")
        if not isinstance(manifest, dict) or manifest.get("api_version") != 1:
            return value
        defaults = {"notification_cameras": [], "notification_config_id": None}
        present = defaults.keys() & value.keys()
        if not present or any(value[key] != defaults[key] for key in present):
            return value
        logger.warning("Normalized inactive version-1 plugin notification metadata")
        return {key: item for key, item in value.items() if key not in present}


class PluginSettings(BaseModel):
    """Developer mode permits installation, not automatic trust or updates."""

    model_config = ConfigDict(extra="forbid")
    developer_mode: bool = False
    plugins: dict[str, PluginEntry] = Field(default_factory=dict)


class WorkerReply(BaseModel):
    """A single JSON-lines response from a plugin process."""

    model_config = ConfigDict(extra="forbid")
    id: int
    result: dict[str, Any] | None = None
    error: str | None = Field(default=None, max_length=100)
