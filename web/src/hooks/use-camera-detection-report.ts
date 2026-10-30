import axios from "axios";
import { useEffect, useRef, useState } from "react";
import type { CameraConfig } from "@/types/frigateConfig";
import type {
  CameraDetectionRead,
  NativeDetectionReport,
} from "@/types/detectionReport";
import {
  parseNativeReport,
  parseRecordingEnd,
} from "@/utils/detectionReportUtil";

function serverTime(value: unknown): number | null {
  if (typeof value !== "string") return null;
  const timestamp = Date.parse(value) / 1000;
  return Number.isFinite(timestamp) && timestamp > 0 ? timestamp : null;
}

async function readCamera(
  camera: string,
  recordingEnabled: boolean,
  signal: AbortSignal,
): Promise<CameraDetectionRead> {
  let native: NativeDetectionReport | null = null;
  let nativeFailed = false;
  let nativeServerTime: number | null = null;
  try {
    const response = await axios.get<unknown>(
      `${encodeURIComponent(camera)}/compressed-motion-preview`,
      { signal, timeout: 8000 },
    );
    native = parseNativeReport(response.data, camera);
    nativeServerTime = serverTime(response.headers.date);
  } catch (error) {
    if (signal.aborted || !(error instanceof Error)) throw error;
    nativeFailed = true;
  }
  const nativeReceivedAt = performance.now();
  let recordingEnd: number | null = null;
  let recordingFailed = false;
  let recordingServerTime: number | null = null;
  if (recordingEnabled) {
    try {
      const response = await axios.get<unknown>(
        `${encodeURIComponent(camera)}/recordings`,
        {
          signal,
          timeout: 8000,
          params:
            nativeServerTime === null
              ? undefined
              : { after: nativeServerTime - 180, before: nativeServerTime + 1 },
        },
      );
      recordingEnd = parseRecordingEnd(response.data);
      recordingServerTime = serverTime(response.headers.date);
    } catch (error) {
      if (signal.aborted || !(error instanceof Error)) throw error;
      recordingFailed = true;
    }
  }
  return {
    native,
    nativeFailed,
    nativeServerTime,
    nativeReceivedAt,
    recordingEnd,
    recordingFailed,
    recordingServerTime,
    recordingReceivedAt: performance.now(),
    receivedAt: performance.now(),
  };
}

export function useCameraDetectionReport(
  camera: CameraConfig,
  isActive: boolean,
  refresh = 0,
  onUpdated?: (value: number) => void,
) {
  const [data, setData] = useState<CameraDetectionRead>();
  const [readFailed, setReadFailed] = useState(false);
  const [now, setNow] = useState(performance.now());
  const lastNative = useRef<
    | {
        value: NativeDetectionReport;
        serverTime: number | null;
        receivedAt: number;
      }
    | undefined
  >(undefined);

  useEffect(() => {
    if (!isActive || !camera.enabled) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      if (document.hidden) {
        timer = setTimeout(() => void poll(), 1000);
        return;
      }
      try {
        const result = await readCamera(
          camera.name,
          camera.record.enabled,
          controller.signal,
        );
        if (controller.signal.aborted) return;
        if (!result.nativeFailed) {
          if (result.native === null) lastNative.current = undefined;
          else if (result.native.updated_at !== undefined) {
            lastNative.current = {
              value: result.native,
              serverTime: result.nativeServerTime,
              receivedAt: result.nativeReceivedAt,
            };
          }
        }
        setData(result);
        setReadFailed(false);
        setNow(performance.now());
        if (
          !result.nativeFailed ||
          (camera.record.enabled && !result.recordingFailed)
        ) {
          onUpdated?.(Math.floor(Date.now() / 1000));
        }
      } catch (error) {
        if (!controller.signal.aborted) {
          if (!(error instanceof Error)) throw error;
          setReadFailed(true);
        }
      }
      if (!controller.signal.aborted)
        timer = setTimeout(() => void poll(), 3000);
    };
    void poll();
    const tick = setInterval(() => setNow(performance.now()), 1000);
    return () => {
      controller.abort();
      clearTimeout(timer);
      clearInterval(tick);
    };
  }, [
    camera.name,
    camera.enabled,
    camera.record.enabled,
    isActive,
    refresh,
    onUpdated,
  ]);

  return { data, readFailed, now, lastNative: lastNative.current };
}
