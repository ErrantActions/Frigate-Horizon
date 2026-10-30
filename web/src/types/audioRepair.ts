export type AudioRepairJobStatus =
  | "scanning"
  | "ready"
  | "running"
  | "pause_requested"
  | "paused"
  | "stop_requested"
  | "stopped"
  | "completed"
  | "interrupted"
  | "failed";

export type AudioRepairItemState =
  | "candidate"
  | "skipped"
  | "selected"
  | "repairing"
  | "repaired"
  | "failed"
  | "stopped";

export type AudioRepairItem = {
  id: string;
  camera: string;
  start_time: number;
  end_time: number;
  video_codec: string | null;
  audio_codec: string | null;
  audio_rate: number | null;
  has_audio: boolean | null;
  state: AudioRepairItemState;
  reason: string | null;
};

export type AudioRepairJob = {
  id: string;
  status: AudioRepairJobStatus;
  phase: "scan" | "repair";
  created_at: number;
  updated_at: number;
  total: number;
  scanned: number;
  eligible: number;
  selected: number;
  processed: number;
  repaired: number;
  skipped: number;
  failed: number;
  current_item_id: string | null;
  current_item?: AudioRepairItem | null;
  error: string | null;
};

export type AudioRepairJobs = {
  jobs: AudioRepairJob[];
  total: number;
  limit: number;
  offset: number;
};

export type AudioRepairItems = {
  items: AudioRepairItem[];
  total: number;
  limit: number;
  offset: number;
};
