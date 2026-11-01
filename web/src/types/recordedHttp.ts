export type RecordedHTTPQuality =
  "auto" | "original" | "360" | "720" | "1080" | "2160";

export type RecordedHTTPSpan = {
  source_start: number;
  source_end: number;
  media_start: number;
  media_end: number;
};

export type RecordedHTTPDiscovery = {
  version: 1;
  enabled: boolean;
  available: boolean;
  transport: "https_mp4";
  seek: "http_byte_range";
  max_window_seconds: number;
  session_url: string;
  qualities: RecordedHTTPQuality[];
};

export type RecordedHTTPSession = {
  version: 1;
  session_id: string;
  camera: string;
  state: "preparing" | "ready" | "failed" | "cleanup_pending";
  quality: RecordedHTTPQuality;
  audio: boolean;
  status_url: string;
  close_url: string;
  media_url: string;
  hard_expires_at: number;
  timeline: RecordedHTTPSpan[];
  error: string | null;
  width: number | null;
  height: number | null;
};
