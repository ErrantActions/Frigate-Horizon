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
  resolve_url: string;
  qualities: RecordedHTTPQuality[];
};

export type RecordedHTTPSession = {
  version: 1;
  session_id: string;
  camera: string;
  state: "preparing" | "ready" | "empty" | "failed" | "cleanup_pending";
  after: number;
  before: number;
  start_timestamp: number;
  effective_start_timestamp: number | null;
  next_timestamp: number;
  delivery?: "prepared" | "original_file";
  preparation_reason?: string | null;
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

export type RecordedHTTPResolution = {
  version: 1;
  effective_start_timestamp: number | null;
  exhausted: boolean;
  searched_before: number;
  reason: string | null;
};
