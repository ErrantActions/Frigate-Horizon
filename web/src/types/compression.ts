export const COMPRESSION_ENCODERS = [
  "h264_nvenc",
  "hevc_nvenc",
  "av1_nvenc",
  "libx264",
  "libx265",
  "libsvtav1",
  "libaom-av1",
] as const;

export type CompressionEncoder = (typeof COMPRESSION_ENCODERS)[number];

export function isCompressionEncoder(
  value: string,
): value is CompressionEncoder {
  return COMPRESSION_ENCODERS.some((encoder) => encoder === value);
}

export type CompressionPolicy = {
  enabled: boolean;
  encoder: CompressionEncoder;
  cpu_threads: number;
  svt_preset: number;
  frame_rate: number | null;
  min_age_days: number;
  target_mbps: number;
  max_files: number;
  max_batch_minutes: number;
  min_free_gib: number;
  window: {
    enabled: boolean;
    weekday: number;
    start_hour: number;
    duration_minutes: number;
  };
};

export type CompressionJob = {
  id: string;
  status:
    | "running"
    | "pause_requested"
    | "paused"
    | "stop_requested"
    | "stopped"
    | "completed"
    | "interrupted"
    | "failed";
  created_at: number;
  updated_at: number;
  scheduled: boolean;
  processed: number;
  compressed: number;
  skipped: number;
  failed: number;
  saved_bytes: number;
  max_files: number;
  target_mbps: number;
  encoder: CompressionEncoder;
  video_codec: "h264" | "hevc" | "av1";
  cpu_threads: number;
  svt_preset: number;
  frame_rate: number | null;
  min_age_days: number;
  current_item_id: string | null;
  reason: string | null;
};

export type CompressionJobs = {
  jobs: CompressionJob[];
  enabled: boolean;
  readers_leased: boolean;
  recovery_required: boolean;
  archive_supported: boolean;
  reservations_ready: boolean;
};

export type CompressionItem = {
  id: string;
  camera: string;
  start_time: number;
  end_time: number;
  state: string;
  reason: string | null;
  saved_bytes: number;
};

export type CompressionItems = { items: CompressionItem[]; total: number };

export const DEFAULT_COMPRESSION_POLICY: CompressionPolicy = {
  enabled: false,
  encoder: "hevc_nvenc",
  cpu_threads: 8,
  svt_preset: 12,
  frame_rate: null,
  min_age_days: 14,
  target_mbps: 4,
  max_files: 100,
  max_batch_minutes: 30,
  min_free_gib: 2,
  window: {
    enabled: false,
    weekday: 6,
    start_hour: 2,
    duration_minutes: 120,
  },
};
