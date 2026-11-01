"""Versioned recorded WebRTC contract shared by web and native clients.

All URLs are relative to the API root. Windows are finite and at most 30 seconds;
request the next window explicitly. Gaps are compressed, with each public timeline
span mapping media time to original catalog time. Cursor values are native input
read positions, not proof of the displayed/decoded frame. Unknown cursors are null.
Every pause/seek/resume invalidates old generation controls. Seek/resume responses
with renegotiate=true require replacing the old peer and answering the new offer.
Quality uses the same peer. There is deliberately no speed or HLS capability.
On cleanup failure, the state is cleanup_pending and no new peer is started;
heartbeat reports paused (or ended after failed activation) after native removal.
After a control error, heartbeat recovers the current server generation.
Create, seek and resume return a warming peer: only the first at-most-200 ms
item repeats while signaling connects. After decoding warmup video, clients POST
control {action:"activate", generation:N, decoded_ready:true}. Answer alone does
not activate playback. Activation confirms a unique finite native program,
preserving the same peer and generation, without returning a new offer.
warmup_expires_at is a nonrenewable deadline, unaffected by heartbeat. A client
assertion of decoded readiness is not server-side decoded-frame proof. Warming
input cursors repeat within the first item; playing cursors require the confirmed
playback program and may lead displayed video. Neither phase promises exact
first-displayed-frame timestamps. Clients should mute repeating warmup audio.
Audio track availability is fixed across activation; heterogeneous/unknown source
audio remains explicitly disabled for the whole window.
Disabled or offline configured cameras remain eligible for authorized historical
playback. Current camera connectivity and go2rtc sources are not prerequisites.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from frigate.api.defs.request.recorded_webrtc_body import RecordedWindow
from frigate.api.defs.response.live_webrtc_response import (
    LiveICEServer,
    LiveSDPOffer,
    LiveWebRTCQuality,
)
from frigate.comms.live_webrtc import IceCandidate


class RecordedWebRTCDiscovery(BaseModel):
    version: Literal[1] = 1
    enabled: bool
    available: bool
    status_url_template: str = "{camera_name}/recordings/webrtc/status"
    signaling: Literal["frigate-recorded-offer-answer-v1"] = (
        "frigate-recorded-offer-answer-v1"
    )
    on_demand: Literal[True] = True
    originals: Literal["unchanged"] = "unchanged"
    max_window_seconds: Literal[30] = 30
    pause: Literal["release_encoder"] = "release_encoder"
    seek: Literal["new_peer_generation"] = "new_peer_generation"
    resume: Literal["new_peer_generation"] = "new_peer_generation"
    position_kind: Literal["input_read_cursor"] = "input_read_cursor"
    gap_policy: Literal["skip"] = "skip"
    activation: Literal["decoded_ready_control"] = "decoded_ready_control"
    warmup_max_seconds: Literal[20] = 20


class RecordedWebRTCStatus(RecordedWebRTCDiscovery):
    protocol: Literal["webrtc"] = "webrtc"
    session_url: str
    relay_available: bool
    renditions: list[LiveWebRTCQuality]
    reason: str | None


class RecordedTimelineSpan(BaseModel):
    source_start: float
    source_end: float
    media_start: float
    media_end: float


class RecordedWebRTCSession(BaseModel):
    version: Literal[1] = 1
    protocol: Literal["webrtc"] = "webrtc"
    session_id: UUID
    generation: int
    state: Literal["warming", "playing", "paused", "ended", "cleanup_pending"]
    warmup_expires_at: float | None
    expires_at: float
    heartbeat_seconds: int
    answer_url: str
    candidates_url: str
    quality_url: str
    control_url: str
    heartbeat_url: str
    close_url: str
    ice_servers: list[LiveICEServer]
    ice_transport_policy: Literal["all", "relay"]
    renditions: list[LiveWebRTCQuality]
    initial_height: int | None
    window: RecordedWindow
    timeline: list[RecordedTimelineSpan]
    position_timestamp: float | None
    position_kind: Literal["input_read_cursor"]
    gap_policy: Literal["skip"]
    audio: bool
    audio_reason: str | None
    renegotiate: bool
    offer: LiveSDPOffer | None
    ice_candidates: list[IceCandidate]
