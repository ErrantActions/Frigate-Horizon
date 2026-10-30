from typing import Any, Self

from pydantic import BaseModel, Field, model_validator

from frigate.config import GenAIProviderEnum


class AppConfigSetBody(BaseModel):
    requires_restart: int = 1
    expected_absent_cameras: list[str] = Field(
        default_factory=list,
        description="Reject camera creation if any named camera already exists in saved configuration.",
    )
    update_topic: str | None = None
    update_topics: list[str] = Field(
        default_factory=list,
        description=(
            "Camera update topics published after one atomic persisted config change. "
            "Cannot be combined with update_topic or skip_save."
        ),
    )
    config_data: dict[str, Any] | None = None
    skip_save: bool = False
    # paths rewritten whole, so a map saves in the order sent
    replace_paths: list[str] = Field(default_factory=list)
    expected_storage: dict[str, Any] | None = Field(
        default=None,
        description="Expected effective values for exactly the storage fields being edited; conflicts return HTTP 409.",
    )

    @model_validator(mode="after")
    def validate_batched_updates(self) -> Self:
        """Keep persisted batch updates separate from legacy in-memory updates."""
        if self.expected_absent_cameras and (
            self.skip_save
            or not self.config_data
            or set(self.config_data) != {"cameras"}
            or not isinstance(self.config_data["cameras"], dict)
            or set(self.expected_absent_cameras) != set(self.config_data["cameras"])
        ):
            raise ValueError("Camera creation preconditions must match new cameras")
        if self.update_topics and (self.update_topic or self.skip_save):
            raise ValueError(
                "update_topics cannot be combined with update_topic or skip_save"
            )
        if self.expected_storage is not None and (
            self.skip_save
            or not self.expected_storage
            or not self.config_data
            or set(self.config_data) != {"storage"}
            or not isinstance(self.config_data["storage"], dict)
            or set(self.expected_storage) != set(self.config_data["storage"])
            or self.replace_paths
        ):
            raise ValueError("Storage preconditions must match persisted storage edits")
        return self


class GenAIProbeBody(BaseModel):
    provider: GenAIProviderEnum
    name: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)


class AppPutPasswordBody(BaseModel):
    password: str
    old_password: str | None = None


class AppPostUsersBody(BaseModel):
    username: str
    password: str
    role: str | None = "viewer"


class AppPostLoginBody(BaseModel):
    user: str
    password: str


class AppPutRoleBody(BaseModel):
    role: str


class CameraSetBody(BaseModel):
    value: str = Field(..., description="The value to set for the feature")


class MediaSyncBody(BaseModel):
    dry_run: bool = Field(
        default=True, description="If True, only report orphans without deleting them"
    )
    media_types: list[str] = Field(
        default=["all"],
        description="Types of media to sync: 'all', 'event_snapshots', 'event_thumbnails', 'review_thumbnails', 'previews', 'exports', 'recordings'",
    )
    force: bool = Field(
        default=False, description="If True, bypass safety threshold checks"
    )
    verbose: bool = Field(
        default=False,
        description="If True, write full orphan file list to disk",
    )
