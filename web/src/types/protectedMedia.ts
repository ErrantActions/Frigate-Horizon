export type ProtectedStorageUsage = {
  recordings: number;
  known_bytes: number;
  unknown_size_recordings: number;
  total_bytes: number | null;
};

export type ProtectedMediaItem = {
  id: string;
  camera: string;
  start_time: number;
  end_time: number | null;
  label: string;
  reason: "saved_event";
  protection: "permanent";
  thumbnail_url: string;
  snapshot_url: string | null;
  original_url: string | null;
  availability:
    | "indexed_local"
    | "archive_online"
    | "archive_unavailable"
    | "missing"
    | "deleting";
  tiers: string[];
  recordings: number;
  has_snapshot: boolean;
};

export type ProtectedMediaResponse = {
  items: ProtectedMediaItem[];
  total: number;
  limit: number;
  offset: number;
  cameras: string[];
  storage: Record<string, ProtectedStorageUsage>;
  archive_state: string;
  archive_updated_at: number | null;
};
