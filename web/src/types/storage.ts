import type { CompressionPolicy } from "./compression";

export type RecordingStorageSettings = {
  compression?: CompressionPolicy;
  enabled: boolean;
  archive_path: string;
  mount_id: string;
  local_budget_gib: number;
  start_percent?: number;
  drain_percent: number;
  retain_days: number;
  max_age_days?: number | null;
  max_archive_gib?: number | null;
  archive_full_behavior?: "pause" | "expire_oldest";
  archive_reserve_gib?: number;
  transfer_mib_per_second: number;
  parallel_transfers?: number;
  adaptive_transfers?: boolean;
  emergency_delete: boolean;
};

export type RecordingStorageUsage = {
  usage_mib: number;
  recordings: number;
  unknown_size_recordings?: number;
};

export type CameraRecordingStorage = {
  friendly_name: string;
  tiers: Record<string, RecordingStorageUsage>;
};

export type StorageCapacity = {
  state: "online" | "unknown" | "stale" | "offline";
  sampled_at: number | null;
  filesystem_id: string | null;
  filesystem_total_bytes: number | null;
  filesystem_available_bytes: number | null;
  usage_bytes: number | null;
  budget_bytes: number | null;
  budget_remaining_bytes: number | null;
  over_budget_bytes: number | null;
  reserved_bytes: number;
  physical_reserved_bytes: number;
  written_reservation_bytes: number;
  reserve_bytes: number;
  effective_headroom_bytes: number | null;
  protected_bytes: number | null;
  reservation_accounting_known?: boolean;
};

export type ArchiveTransferFileProgress = {
  id?: string;
  name: string;
  camera: string;
  start_time: number;
  end_time: number;
  size_bytes: number;
  copied_bytes: number;
  readback_bytes: number;
  phase?: "copying" | "verification" | "commit" | "waiting";
};

export type ArchiveTransferProgress = {
  sampled_at?: number;
  stale?: boolean;
  trigger?: "high_water" | "local_pressure";
  active: boolean;
  started_at: number | null;
  phase: "copying" | "verification" | "commit" | "waiting" | "buffering";
  initial_plan_bytes: number;
  initial_plan_files: number;
  plan_is_estimate: boolean;
  verified_bytes: number;
  verified_files: number;
  incoming_bytes: number;
  local_usage_bytes: number;
  drain_target_bytes: number;
  remaining_bytes: number;
  rate_bytes_per_second: number | null;
  copy_rate_bytes_per_second?: number | null;
  verification_rate_bytes_per_second?: number | null;
  ingest_bytes_per_second: number | null;
  eta_seconds: number | null;
  buffer_seconds: number | null;
  cannot_outpace_ingest: boolean;
  aggregate_rates?: boolean;
  parallel_transfers?: number;
  adaptive_transfers?: {
    enabled: boolean;
    maximum_workers: number;
    effective_workers: number;
    state: string;
  };
  active_files?: ArchiveTransferFileProgress[];
  current_file: ArchiveTransferFileProgress | null;
};

export type RecordingStorageTiers = {
  usage_status?: {
    sampled_at: number | null;
    stale: boolean;
    error: string | null;
  };
  settings: RecordingStorageSettings;
  local: RecordingStorageUsage;
  archive: RecordingStorageUsage;
  cameras?: Record<string, CameraRecordingStorage>;
  tier_labels?: Record<string, string>;
  health: {
    state:
      "starting" | "checking" | "disabled" | "ready" | "moving" | "blocked";
    error: string | null;
    updated_at: number;
    settings_pending?: boolean;
    archive_free_mib?: number;
    archive_total_mib?: number;
    backlog_mib?: number;
    archive_usage_bytes?: number | null;
    archive_budget_bytes?: number | null;
    archive_reserved_bytes?: number;
    capacity?: { local?: StorageCapacity; archive?: StorageCapacity };
    transfer?: ArchiveTransferProgress | null;
  };
};
