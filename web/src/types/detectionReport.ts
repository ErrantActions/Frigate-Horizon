export const REPORT_LABELS = ["motion", "person", "vehicle", "animal"] as const;
export type ReportLabel = (typeof REPORT_LABELS)[number];
export type NativeReportState =
  "running" | "stale" | "starting" | "connecting" | "error" | "disabled";

export type NativeDetectionReport = {
  camera: string;
  state: NativeReportState;
  updated_at?: number;
  states: Partial<Record<ReportLabel, boolean>>;
  events_started: number;
  events_ended: number;
  history: { label: ReportLabel; active: boolean; received_at: number }[];
};

export type DetectionHealth =
  | "reporting"
  | "stale"
  | "starting"
  | "connecting"
  | "error"
  | "disabled"
  | "unknown";

export type CameraDetectionRead = {
  native: NativeDetectionReport | null;
  nativeFailed: boolean;
  nativeServerTime: number | null;
  nativeReceivedAt: number;
  recordingEnd: number | null;
  recordingFailed: boolean;
  recordingServerTime: number | null;
  recordingReceivedAt: number;
  receivedAt: number;
};
