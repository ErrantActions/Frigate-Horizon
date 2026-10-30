import {
  REPORT_LABELS,
  type DetectionHealth,
  type NativeDetectionReport,
  type NativeReportState,
  type ReportLabel,
} from "@/types/detectionReport";
import type { CameraDetectionRead } from "@/types/detectionReport";

function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function finite(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function timestamp(value: unknown): value is number {
  return finite(value) && value > 0 && value <= 8640000000000;
}

function label(value: unknown): value is ReportLabel {
  return REPORT_LABELS.some((entry) => entry === value);
}

function state(value: unknown): value is NativeReportState {
  return [
    "running",
    "stale",
    "starting",
    "connecting",
    "error",
    "disabled",
  ].some((entry) => entry === value);
}

export function parseNativeReport(
  value: unknown,
  camera: string,
): NativeDetectionReport | null {
  if (!record(value) || value.camera !== camera) {
    throw new Error("Detection report camera mismatch");
  }
  if (value.native_detection === undefined) return null;
  const native = value.native_detection;
  if (
    !record(native) ||
    native.camera !== camera ||
    native.source !== "reolink_http_api" ||
    !state(native.state) ||
    native.video_decoded !== false ||
    native.confidence_available !== false ||
    !Number.isSafeInteger(native.events_started) ||
    !finite(native.events_started) ||
    native.events_started < 0 ||
    !Number.isSafeInteger(native.events_ended) ||
    !finite(native.events_ended) ||
    native.events_ended < 0 ||
    (native.updated_at !== undefined && !timestamp(native.updated_at)) ||
    !Array.isArray(native.history) ||
    native.history.length > 300
  ) {
    throw new Error("Invalid native detection report");
  }
  const states: NativeDetectionReport["states"] = {};
  if (native.states !== undefined) {
    if (!record(native.states)) throw new Error("Invalid native alarm states");
    for (const key of REPORT_LABELS) {
      const value = native.states[key];
      if (value === undefined) continue;
      if (typeof value !== "boolean")
        throw new Error("Invalid native alarm state");
      states[key] = value;
    }
  }
  if (
    native.state === "running" &&
    (states.person === undefined ||
      states.vehicle === undefined ||
      native.updated_at === undefined)
  ) {
    throw new Error("Missing native detection evidence");
  }
  const history = native.history.map((item: unknown) => {
    if (
      !record(item) ||
      !label(item.label) ||
      typeof item.active !== "boolean" ||
      !timestamp(item.received_at)
    ) {
      throw new Error("Invalid native report history");
    }
    return {
      label: item.label,
      active: item.active,
      received_at: item.received_at,
    };
  });
  return {
    camera,
    state: native.state,
    updated_at: native.updated_at,
    states,
    history,
    events_started: native.events_started,
    events_ended: native.events_ended,
  };
}

export function parseRecordingEnd(value: unknown): number | null {
  if (!Array.isArray(value) || value.length > 2000)
    throw new Error("Invalid recording report");
  let latest: number | null = null;
  for (const item of value) {
    if (
      !record(item) ||
      !timestamp(item.start_time) ||
      !timestamp(item.end_time) ||
      item.end_time <= item.start_time
    ) {
      throw new Error("Invalid recording segment times");
    }
    latest = Math.max(latest ?? 0, item.end_time);
  }
  return latest;
}

export function reportAge(
  timestamp: number | undefined | null,
  serverTime: number | null,
  elapsed: number,
): number | null {
  if (
    timestamp == null ||
    serverTime === null ||
    !Number.isFinite(elapsed) ||
    !Number.isFinite(timestamp) ||
    !Number.isFinite(serverTime) ||
    elapsed < 0 ||
    timestamp > serverTime + 2
  )
    return null;
  return Math.max(0, serverTime - timestamp + elapsed);
}

export function nativeDetectionHealth(
  native: NativeDetectionReport | null,
  failed: boolean,
  age: number | null,
): DetectionHealth {
  if (failed) return "error";
  if (!native) return "unknown";
  if (native.state !== "running") return native.state;
  if (age === null) return "unknown";
  return age > 5 ? "stale" : "reporting";
}

export function recordingReportHealth(
  data: CameraDetectionRead | undefined,
  enabled: boolean,
  recordingEnabled: boolean,
  now: number,
  failed: boolean,
): DetectionHealth {
  if (!enabled || !recordingEnabled) return "disabled";
  if (data?.recordingFailed || failed) return "error";
  if (!data) return "starting";
  if (data.recordingEnd === null) return "stale";
  const age = reportAge(
    data.recordingEnd,
    data.recordingServerTime,
    (now - data.recordingReceivedAt) / 1000,
  );
  return age === null ? "unknown" : age > 60 ? "stale" : "reporting";
}
