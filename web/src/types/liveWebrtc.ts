import type { LiveHlsRendition } from "./liveHls";

export type LiveWebRTCStatus = {
  version: 1;
  protocol: "webrtc";
  enabled: boolean;
  available: boolean;
  session_url: string;
  grid_height: number;
  grid_rendition: LiveHlsRendition;
  relay_available: boolean;
  renditions: LiveHlsRendition[];
  reason: string | null;
};

export type LiveWebRTCLease = {
  version: 1;
  protocol: "webrtc";
  session_id: string;
  expires_at: number;
  heartbeat_seconds: number;
  answer_url: string;
  candidates_url: string;
  heartbeat_url: string;
  close_url: string;
  quality_url: string;
  ice_servers: RTCIceServer[];
};

export type LiveWebRTCSession = LiveWebRTCLease & {
  offer: RTCSessionDescriptionInit;
  ice_candidates: RTCIceCandidateInit[];
  ice_transport_policy: RTCIceTransportPolicy;
  renditions: LiveHlsRendition[];
  initial_height: number | null;
};
