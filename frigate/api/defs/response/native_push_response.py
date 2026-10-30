"""Native push responses intentionally exclude device tokens and provider secrets."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from .generic_response import GenericResponse


class NativePushStatus(BaseModel):
    enabled: bool
    server_id: str | None
    topic: str | None
    image_notifications: bool = False
    device_preferences: bool = False
    action_notifications: bool = False
    health_notifications: bool = False
    supports_device_features: bool = False


class NativePushDeviceMetadata(BaseModel):
    installation_id: UUID
    environment: Literal["production", "sandbox"]
    camera_ids: list[str] | None
    registered_at: float


class NativePushRegistrationResponse(GenericResponse):
    server_id: str


class NativeNotificationResolution(BaseModel):
    """A current-authorized opaque destination, never a URL or image attachment."""

    server_id: str
    kind: Literal["event", "review", "camera"]
    identifier: str
    camera: str
    timestamp: float | None
