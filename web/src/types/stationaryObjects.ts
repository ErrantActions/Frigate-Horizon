export type StationaryObject = {
  id: string;
  label: string;
  box: [number, number, number, number];
  score: number;
  stationary_frames: number;
  position_changes: number;
  tracked_seconds: number;
  last_tracking_update: number;
  updated_in_current_frame: boolean;
};

export type StationaryObjectsSnapshot = {
  camera: string;
  status: "ready" | "stale" | "warming_up" | "disabled" | "detection_disabled";
  sampled_at: number;
  frame_time: number | null;
  frame_age_seconds: number | null;
  frame_width: number;
  frame_height: number;
  stationary_threshold: number;
  total: number;
  limit: number;
  objects: StationaryObject[];
};
