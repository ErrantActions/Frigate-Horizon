import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import axios, { AxiosError } from "axios";
import useSWR from "swr";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import type { CompatibilityPlaybackStatus } from "@/types/playback";
import type { PlaybackQuality } from "@/types/record";

type Parameters = {
  enabled: boolean;
  camera: string;
  after: number;
  before: number;
  startTimestamp: number;
  seekTimestamp?: number;
  stream: PlaybackQuality;
};
type Entry = { key: string; value: CompatibilityPlaybackStatus };
type Failure = { key: string; code: string };
type ApiError = { detail?: unknown };

function requestCode(error: AxiosError<ApiError>): string {
  const detail = error.response?.data?.detail;
  return typeof detail === "string" && /^playback_[a-z_]+$/.test(detail)
    ? detail
    : "playback_request_failed";
}

function matches(
  value: CompatibilityPlaybackStatus,
  expected: Pick<
    CompatibilityPlaybackStatus,
    "camera" | "client_id" | "generation"
  >,
) {
  const base = `${encodeURIComponent(expected.camera)}/recordings/playback-compatibility`;
  return (
    value.camera === expected.camera &&
    value.client_id === expected.client_id &&
    value.generation === expected.generation &&
    /^[a-zA-Z0-9_-]+$/.test(value.id) &&
    value.manifest_path === `${base}/${value.id}/media/index.m3u8`
  );
}

export function useRecordingCompatibility(parameters: Parameters) {
  const { t } = useTranslation("components/player");
  const translationRef = useRef(t);
  const parametersRef = useRef(parameters);
  const generation = useRef(0);
  const [clientId] = useState(() =>
    Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) =>
      byte.toString(16).padStart(2, "0"),
    ).join(""),
  );
  const [attempt, setAttempt] = useState(0);
  const [entry, setEntry] = useState<Entry>();
  const [failure, setFailure] = useState<Failure>();
  const { enabled, camera, after, before, stream, seekTimestamp } = parameters;
  const [activation, setActivation] = useState({ enabled, serial: 0 });
  if (activation.enabled !== enabled) {
    setActivation({ enabled, serial: activation.serial + 1 });
  }
  const key = useMemo(
    () =>
      enabled
        ? JSON.stringify([
            camera,
            after,
            before,
            stream,
            seekTimestamp,
            attempt,
            activation.serial,
          ])
        : null,
    [
      enabled,
      camera,
      after,
      before,
      stream,
      seekTimestamp,
      attempt,
      activation.serial,
    ],
  );
  const activeEntry = key && entry?.key === key ? entry : undefined;
  const activeFailure = key && failure?.key === key ? failure : undefined;
  const base = `${encodeURIComponent(camera)}/recordings/playback-compatibility`;

  useEffect(() => {
    translationRef.current = t;
    parametersRef.current = parameters;
  }, [t, parameters]);

  const release = useCallback(async (path: string) => {
    try {
      await axios.delete(path, { timeout: 10000 });
    } catch (error) {
      if (!axios.isAxiosError(error)) throw error;
      if ([404, 410].includes(error.response?.status ?? 0)) return;
      toast.warning(translationRef.current("compatibility.cleanupFailed"));
    }
  }, []);

  useEffect(() => {
    if (!key) return;
    const current = parametersRef.current;
    const path = `${encodeURIComponent(current.camera)}/recordings/playback-compatibility`;
    const requestGeneration = ++generation.current;
    const abort = new AbortController();
    let disposed = false;
    let createdId: string | undefined;
    setFailure(undefined);

    const create = async () => {
      try {
        const response = await axios.post<CompatibilityPlaybackStatus>(
          path,
          {
            after: current.after,
            before: current.before,
            stream: current.stream,
            start_timestamp: current.startTimestamp,
            client_id: clientId,
            generation: requestGeneration,
          },
          { signal: abort.signal, timeout: 20000 },
        );
        if (
          !matches(response.data, {
            camera: current.camera,
            client_id: clientId,
            generation: requestGeneration,
          })
        ) {
          if (!disposed) {
            setFailure({ key, code: "playback_invalid_response" });
          }
          return;
        }
        createdId = response.data.id;
        if (disposed) {
          await release(`${path}/${createdId}`);
          return;
        }
        setEntry({ key, value: response.data });
      } catch (error) {
        if (disposed || axios.isCancel(error)) return;
        if (!axios.isAxiosError<ApiError>(error)) throw error;
        setFailure({ key, code: requestCode(error) });
      }
    };
    void create();
    return () => {
      disposed = true;
      abort.abort();
      if (createdId) void release(`${path}/${createdId}`);
    };
  }, [key, clientId, release]);

  const { data, error: statusError } = useSWR<
    CompatibilityPlaybackStatus,
    Error | AxiosError<ApiError>
  >(
    activeEntry && !activeFailure ? `${base}/${activeEntry.value.id}` : null,
    async (path: string) => {
      const response = await axios.get<CompatibilityPlaybackStatus>(path, {
        timeout: 10000,
      });
      if (!activeEntry || !matches(response.data, activeEntry.value)) {
        throw new Error("playback_invalid_response");
      }
      return response.data;
    },
    {
      fallbackData: activeEntry?.value,
      refreshInterval: (status) =>
        status?.state === "complete"
          ? 20000
          : status?.state === "failed"
            ? 0
            : 1000,
      refreshWhenHidden: true,
      revalidateOnFocus: true,
      shouldRetryOnError: false,
      keepPreviousData: false,
    },
  );
  const status = activeEntry
    ? data && matches(data, activeEntry.value)
      ? data
      : activeEntry.value
    : undefined;
  const ready = status?.state === "ready" || status?.state === "complete";
  const statusFatal =
    !!statusError &&
    (!ready ||
      !axios.isAxiosError(statusError) ||
      [401, 403, 404, 410].includes(statusError.response?.status ?? 0));
  const error = key
    ? (activeFailure?.code ??
      (status?.state === "failed"
        ? status.error || "playback_encoding_failed"
        : undefined) ??
      (statusFatal
        ? axios.isAxiosError<ApiError>(statusError)
          ? requestCode(statusError)
          : "playback_status_failed"
        : undefined))
    : undefined;

  useEffect(() => {
    if (!key || ready || error) return;
    const timeout = setTimeout(
      () => setFailure({ key, code: "playback_startup_timeout" }),
      30000,
    );
    return () => clearTimeout(timeout);
  }, [key, ready, error]);

  useEffect(() => {
    if (error && activeEntry) {
      void release(`${base}/${activeEntry.value.id}`);
    }
  }, [error, activeEntry, base, release]);

  return {
    status: error ? undefined : status,
    loading: !!key && !ready && !error,
    error,
    warning: !!key && !!statusError && !statusFatal,
    retry: () => {
      setFailure(undefined);
      setAttempt((previous) => previous + 1);
    },
    fail: (code: string) => {
      if (key) setFailure({ key, code });
    },
  };
}
