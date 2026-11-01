"""Versioned, owner-scoped WebRTC session metadata."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from frigate.comms.live_webrtc import IceCandidate


class LiveWebRTCQuality(BaseModel):
    height: int
    bitrate: int


class LiveWebRTCDiscovery(BaseModel):
    version: Literal[1] = 1
    enabled: bool
    available: bool
    status_url_template: str = "{camera_name}/live/webrtc/status"
    signaling: Literal["frigate-rest-offer-answer-v1"] = "frigate-rest-offer-answer-v1"
    on_demand: Literal[True] = True
    recordings: Literal["original_source"] = "original_source"


class LiveWebRTCStatus(BaseModel):
    version: Literal[1] = 1
    protocol: Literal["webrtc"] = "webrtc"
    enabled: bool
    available: bool
    session_url: str
    grid_height: int
    grid_rendition: LiveWebRTCQuality
    relay_available: bool
    renditions: list[LiveWebRTCQuality]
    reason: str | None


class LiveICEServer(BaseModel):
    urls: list[str]
    username: str
    credential: str


class LiveWebRTCLease(BaseModel):
    version: Literal[1] = 1
    protocol: Literal["webrtc"] = "webrtc"
    session_id: UUID
    expires_at: float
    heartbeat_seconds: int
    answer_url: str
    candidates_url: str
    heartbeat_url: str
    close_url: str
    quality_url: str
    ice_servers: list[LiveICEServer]


class LiveSDPOffer(BaseModel):
    type: Literal["offer"]
    sdp: str


class LiveWebRTCSession(LiveWebRTCLease):
    offer: LiveSDPOffer
    ice_candidates: list[IceCandidate]
    ice_transport_policy: Literal["all", "relay"]
    renditions: list[LiveWebRTCQuality]
    initial_height: int | None
