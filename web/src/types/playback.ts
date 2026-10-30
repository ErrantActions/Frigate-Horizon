import { Preview } from "./preview";
import { Recording } from "./record";
import { TimeRange } from "./timeline";

export type DynamicPlayback = {
  recordings: Recording[];
  timeRange: TimeRange;
};

export type PreviewPlayback = {
  preview: Preview | undefined;
  timeRange: TimeRange;
};

export type VodManifest = {
  sequences: { clips: { clipFrom?: number }[] }[];
};

export type CompatibilityPlaybackSpan = {
  start_time: number;
  end_time: number;
  /** Measured encoded duration in seconds, unlike coverage's millisecond spans. */
  duration: number;
  measured: true;
  lead_in_ms?: number | null;
};

export type CompatibilityPlaybackStatus = {
  id: string;
  camera: string;
  client_id: string;
  generation: number;
  state: "preparing" | "ready" | "complete" | "failed";
  manifest_path: string;
  timeline: CompatibilityPlaybackSpan[];
  encoder: string;
  error: string | null;
  after: number;
  before: number;
  start_timestamp: number;
  ready_through: number | null;
};
