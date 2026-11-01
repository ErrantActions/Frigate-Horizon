import type { LiveWebRTCLease } from "./liveWebrtc";
import type { LiveHlsRendition } from "./liveHls";
import type { PlaybackQuality } from "./record";

export type RecordedWebRTCWindow = {
  after: number;
  before: number;
  start_timestamp: number;
  stream: PlaybackQuality;
};

export type RecordedWebRTCSession = LiveWebRTCLease & {
  generation: number;
  state: "warming" | "playing" | "paused" | "ended" | "cleanup_pending";
  warmup_expires_at: number | null;
  control_url: string;
  ice_transport_policy: RTCIceTransportPolicy;
  renditions: LiveHlsRendition[];
  initial_height: number | null;
  window: RecordedWebRTCWindow;
  timeline: {
    source_start: number;
    source_end: number;
    media_start: number;
    media_end: number;
  }[];
  position_timestamp: number | null;
  position_kind: "input_read_cursor";
  gap_policy: "skip";
  audio: boolean;
  audio_reason: string | null;
  renegotiate: boolean;
  offer: RTCSessionDescriptionInit | null;
  ice_candidates: RTCIceCandidateInit[];
};
