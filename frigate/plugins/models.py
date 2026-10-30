"""Versioned, validated plugin manifests and worker messages."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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
