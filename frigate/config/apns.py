"""Opt-in Apple Push Notification service provider settings."""

import re
from pathlib import Path
from typing import Self

from pydantic import Field, field_validator, model_validator

from frigate.util.env import EnvString

from .base import FrigateBaseModel


class APNsConfig(FrigateBaseModel):
    """Keep provider credentials on the server, outside the mobile application."""

    enabled: bool = Field(
        default=False,
        title="Enable native Apple push",
        description="Enable opt-in iOS push delivery alongside existing browser notifications. Native authentication is required.",
    )
    server_id: str | None = Field(
        default=None,
        max_length=64,
        title="Server identifier",
        description="A stable unique identifier for this NVR, used to reject notifications from another saved server.",
    )
    topic: str | None = Field(
        default=None,
        max_length=255,
        title="App bundle identifier",
        description="The registered iOS bundle ID used as the APNs topic.",
    )
    team_id: EnvString | None = Field(
        default=None,
        title="Apple team identifier",
        description="Apple Developer team ID. Supports FRIGATE environment placeholders.",
    )
    key_id: EnvString | None = Field(
        default=None,
        title="APNs key identifier",
        description="Identifier of a server-side Apple push authentication key, not an App Store Connect upload key.",
    )
    private_key_file: EnvString | None = Field(
        default=None,
        title="APNs private key file",
        description="Absolute path to the mounted APNs .p8 file. Never put its contents into the configuration or app.",
    )
    include_details: bool = Field(
        default=False,
        title="Include notification details",
        description="Include event titles and descriptions in Apple-delivered lock-screen alerts. Disabled by default; routing identifiers are still sent.",
    )
    image_notifications: bool = Field(
        default=False,
        title="Enable private notification images",
        description="Advertise the installation-scoped authenticated thumbnail service and let compatible native apps request opt-in image previews. No media or URLs are included in push payloads.",
    )
    minimum_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        strict=True,
        allow_inf_nan=False,
        title="Minimum native notification confidence",
        description="Minimum stored detection confidence for native activity notifications, from 0 to 1. Zero preserves existing eligibility. Does not change detection thresholds, recording, browser notifications, tests, or health alerts.",
    )
    require_coral_confirmation: bool = Field(
        default=False,
        strict=True,
        title="Require Coral-confirmed native notifications",
        description="Require a linked tracked object from an Edge TPU before sending native activity notifications. Camera alarms without a matching Coral detection are not sufficient. Review notifications wait for completion; this does not enable additional analysis.",
    )

    @field_validator("team_id", "key_id")
    @classmethod
    def validate_key_identifiers(cls, value: str | None) -> str | None:
        """These fields accept public identifiers, never private key contents."""
        if value is not None and not re.fullmatch(r"[A-Z0-9]{10}", value):
            raise ValueError(
                "Apple team and key IDs must contain 10 uppercase alphanumeric characters"
            )
        return value

    @field_validator("private_key_file")
    @classmethod
    def validate_key_path(cls, value: str | None) -> str | None:
        """Refuse inline keys, including while the provider is disabled."""
        if value is not None and (
            "\n" in value or "\r" in value or not Path(value).is_absolute()
        ):
            raise ValueError(
                "Provide an absolute APNs key file path, not private key contents"
            )
        return value

    @model_validator(mode="after")
    def validate_enabled_provider(self) -> Self:
        """Require complete provider metadata only when native delivery is enabled."""
        if not self.enabled:
            return self
        if not self.server_id or not re.fullmatch(
            r"[A-Za-z0-9._-]{1,64}", self.server_id
        ):
            raise ValueError("Enabled APNs requires a stable alphanumeric server_id")
        if not self.topic or not re.fullmatch(
            r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", self.topic
        ):
            raise ValueError("Enabled APNs requires the registered app bundle ID")
        for value in (self.team_id, self.key_id):
            if not value or not re.fullmatch(r"[A-Z0-9]{10}", value):
                raise ValueError("Enabled APNs requires 10-character team and key IDs")
        if not self.private_key_file or not Path(self.private_key_file).is_absolute():
            raise ValueError("Enabled APNs requires an absolute private_key_file path")
        return self
