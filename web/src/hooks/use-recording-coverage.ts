import { useCallback, useEffect, useRef, useState } from "react";
import axios from "axios";
import type { RecordingCoverage } from "@/types/record";

export function useRecordingCoverage(
  camera: string,
  after: number,
  before: number,
  enabled: boolean,
) {
  const key = JSON.stringify([camera, after, before]);
  const request = useRef<AbortController | undefined>(undefined);
  const [result, setResult] = useState<{
    key: string;
    data?: RecordingCoverage;
    error?: Error;
  }>();
  const resultRef = useRef(result);
  useEffect(() => {
    resultRef.current = result;
  }, [result]);

  const refresh = useCallback(async () => {
    request.current?.abort();
    if (!enabled) return;
    const controller = new AbortController();
    request.current = controller;
    setResult((current) =>
      current?.key === key ? { key, data: current.data } : { key },
    );
    try {
      const response = await axios.get<RecordingCoverage>(
        `${camera}/recordings/coverage`,
        {
          params: { after, before, timelines: true },
          signal: controller.signal,
          timeout: 20000,
        },
      );
      if (!controller.signal.aborted && request.current === controller) {
        setResult({ key, data: response.data });
      }
    } catch (error) {
      if (controller.signal.aborted || axios.isCancel(error)) return;
      if (request.current === controller) {
        setResult({
          key,
          error:
            error instanceof Error
              ? error
              : new Error("recording_coverage_failed"),
        });
      }
    }
  }, [camera, after, before, key, enabled]);

  useEffect(() => {
    if (!enabled) {
      request.current?.abort();
      return;
    }
    if (
      resultRef.current?.key === key &&
      resultRef.current.data &&
      !resultRef.current.error
    )
      return;
    void refresh();
    const current = request.current;
    return () => current?.abort();
  }, [refresh, enabled, key]);

  useEffect(() => () => request.current?.abort(), []);

  return {
    data: result?.key === key ? result.data : undefined,
    error: result?.key === key ? result.error : undefined,
    refresh,
  };
}
