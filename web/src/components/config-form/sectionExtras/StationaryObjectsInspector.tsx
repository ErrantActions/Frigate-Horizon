import { useApiHost } from "@/api";
import { Button } from "@/components/ui/button";
import type { StationaryObjectsSnapshot } from "@/types/stationaryObjects";
import type { User } from "@/types/user";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import useSWR from "swr";
import type { SectionRendererProps } from "./registry";

const POLL_MS = 2000;
const TIMEOUT_MS = 5000;

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function finite(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function validSnapshot(
  value: unknown,
  camera: string,
): value is StationaryObjectsSnapshot {
  if (!record(value)) return false;
  return (
    value.camera === camera &&
    ["ready", "stale", "warming_up", "disabled", "detection_disabled"].includes(
      String(value.status),
    ) &&
    finite(value.sampled_at) &&
    (value.frame_time === null || finite(value.frame_time)) &&
    (value.frame_age_seconds === null ||
      (finite(value.frame_age_seconds) && value.frame_age_seconds >= 0)) &&
    finite(value.frame_width) &&
    Number.isInteger(value.frame_width) &&
    value.frame_width > 0 &&
    finite(value.frame_height) &&
    Number.isInteger(value.frame_height) &&
    value.frame_height > 0 &&
    finite(value.stationary_threshold) &&
    value.stationary_threshold >= 0 &&
    finite(value.total) &&
    Number.isInteger(value.total) &&
    finite(value.limit) &&
    value.limit === 100 &&
    Array.isArray(value.objects) &&
    value.objects.length <= 100 &&
    value.total >= value.objects.length &&
    value.objects.every(
      (object: unknown) =>
        record(object) &&
        typeof object.id === "string" &&
        typeof object.label === "string" &&
        Array.isArray(object.box) &&
        object.box.length === 4 &&
        object.box.every(
          (coordinate: unknown) =>
            finite(coordinate) && Number.isInteger(coordinate),
        ) &&
        finite(object.score) &&
        object.score >= 0 &&
        object.score <= 1 &&
        finite(object.stationary_frames) &&
        Number.isInteger(object.stationary_frames) &&
        object.stationary_frames >= 0 &&
        finite(object.position_changes) &&
        Number.isInteger(object.position_changes) &&
        object.position_changes >= 0 &&
        finite(object.tracked_seconds) &&
        object.tracked_seconds >= 0 &&
        finite(object.last_tracking_update) &&
        typeof object.updated_in_current_frame === "boolean",
    )
  );
}

function Inspector({ camera }: { camera: string }) {
  const { t, i18n } = useTranslation(["views/settings", "objects"]);
  const apiHost = useApiHost();
  const [open, setOpen] = useState(false);
  const [visible, setVisible] = useState(!document.hidden);
  const [snapshot, setSnapshot] = useState<StationaryObjectsSnapshot>();
  const [failure, setFailure] = useState<
    "request" | "unavailable" | "invalid"
  >();
  const [imageError, setImageError] = useState(false);
  const [liveStream, setLiveStream] = useState(true);
  const [showDetections, setShowDetections] = useState(false);
  const [retry, setRetry] = useState(0);
  const number = useMemo(
    () => new Intl.NumberFormat(i18n.language, { maximumFractionDigits: 1 }),
    [i18n.language],
  );
  const percent = useMemo(
    () =>
      new Intl.NumberFormat(i18n.language, {
        style: "percent",
        maximumFractionDigits: 0,
      }),
    [i18n.language],
  );

  useEffect(() => {
    const update = () => setVisible(!document.hidden);
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);

  useEffect(() => {
    if (!open || !visible) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let timeout: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    setSnapshot(undefined);
    setFailure(undefined);
    const poll = async () => {
      if (disposed || document.hidden) return;
      const request = new AbortController();
      controller = request;
      timeout = setTimeout(() => request.abort(), TIMEOUT_MS);
      try {
        const response = await fetch(
          `${apiHost}api/${encodeURIComponent(camera)}/stationary-objects`,
          {
            signal: request.signal,
            cache: "no-store",
            credentials: "same-origin",
          },
        );
        if (disposed || document.hidden) return;
        if (!response.ok) {
          setFailure(response.status === 404 ? "unavailable" : "request");
          setSnapshot(undefined);
          return;
        }
        const data: unknown = await response.json();
        if (disposed || document.hidden) return;
        if (!validSnapshot(data, camera)) {
          setFailure("invalid");
          setSnapshot(undefined);
          return;
        }
        setSnapshot(data);
        setFailure(undefined);
      } catch {
        if (!disposed && !document.hidden) {
          setFailure("request");
          setSnapshot(undefined);
        }
      } finally {
        clearTimeout(timeout);
        if (!disposed && !document.hidden) timer = setTimeout(poll, POLL_MS);
      }
    };
    void poll();
    return () => {
      disposed = true;
      clearTimeout(timer);
      clearTimeout(timeout);
      controller?.abort();
    };
  }, [apiHost, camera, open, visible, retry]);

  return (
    <section className="mt-5 space-y-3 rounded-lg border p-3">
      <Button
        type="button"
        variant="secondary"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        {t(open ? "stationaryObjects.close" : "stationaryObjects.open")}
      </Button>
      {open && (
        <div
          role="region"
          aria-label={t("stationaryObjects.title")}
          className="space-y-3"
        >
          <p className="text-sm text-muted-foreground">
            {t("stationaryObjects.help")}
          </p>
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={liveStream}
                onChange={(event) => {
                  setLiveStream(event.target.checked);
                  setImageError(false);
                }}
              />
              {t("stationaryObjects.liveStream")}
            </label>
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={showDetections}
                onChange={(event) => {
                  setShowDetections(event.target.checked);
                  setImageError(false);
                }}
              />
              {t("stationaryObjects.showDetections")}
            </label>
          </div>
          {showDetections && (
            <p className="text-sm text-muted-foreground">
              {t("stationaryObjects.detectionsHelp")}
            </p>
          )}
          {!visible ? (
            <p role="status">{t("stationaryObjects.paused")}</p>
          ) : failure ? (
            <div role="alert" className="space-y-2">
              <p>{t(`stationaryObjects.errors.${failure}`)}</p>
              <Button
                type="button"
                variant="outline"
                onClick={() => setRetry(retry + 1)}
              >
                {t("stationaryObjects.retry")}
              </Button>
            </div>
          ) : !snapshot ? (
            <p role="status">{t("stationaryObjects.loading")}</p>
          ) : (
            <>
              <p
                role="status"
                className={
                  snapshot.status === "stale"
                    ? "text-warning"
                    : "text-muted-foreground"
                }
              >
                {t(`stationaryObjects.status.${snapshot.status}`)}
              </p>
              {snapshot.frame_time !== null && snapshot.status === "ready" && (
                <>
                  <p className="text-sm text-muted-foreground">
                    {t("stationaryObjects.frameAge", {
                      seconds: number.format(snapshot.frame_age_seconds ?? 0),
                      frames: number.format(snapshot.stationary_threshold),
                    })}
                  </p>
                  <img
                    key={`${liveStream}-${showDetections}-${retry}`}
                    className="w-full rounded-md"
                    src={
                      liveStream
                        ? `${apiHost}api/${encodeURIComponent(camera)}?fps=5&height=640&bbox=1&stationary=${!showDetections}&timestamp=1&cache=${retry}`
                        : `${apiHost}api/${encodeURIComponent(camera)}/latest.jpg?bbox=1&stationary=${!showDetections}&timestamp=1&height=640&cache=${snapshot.sampled_at}`
                    }
                    alt={t(
                      showDetections
                        ? "stationaryObjects.detectionsImage"
                        : "stationaryObjects.image",
                      { camera },
                    )}
                    onLoad={() => setImageError(false)}
                    onError={() => setImageError(true)}
                  />
                  {imageError && (
                    <div role="alert" className="space-y-2">
                      <p>{t("stationaryObjects.imageFailed")}</p>
                      <Button
                        type="button"
                        variant="outline"
                        onClick={() => {
                          setImageError(false);
                          setRetry(retry + 1);
                        }}
                      >
                        {t("stationaryObjects.retry")}
                      </Button>
                    </div>
                  )}
                </>
              )}
              {snapshot.objects.length === 0 ? (
                snapshot.status === "ready" && (
                  <p>{t("stationaryObjects.empty")}</p>
                )
              ) : (
                <div className="max-h-96 overflow-auto">
                  <table className="w-full text-left text-sm">
                    <caption className="mb-2 text-left">
                      {t("stationaryObjects.count", { count: snapshot.total })}
                    </caption>
                    <thead>
                      <tr>
                        <th className="p-2">
                          {t("stationaryObjects.columns.object")}
                        </th>
                        <th className="p-2">
                          {t("stationaryObjects.columns.confidence")}
                        </th>
                        <th className="p-2">
                          {t("stationaryObjects.columns.frames")}
                        </th>
                        <th className="p-2">
                          {t("stationaryObjects.columns.age")}
                        </th>
                        <th className="p-2">
                          {t("stationaryObjects.columns.movement")}
                        </th>
                        <th className="p-2">
                          {t("stationaryObjects.columns.update")}
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {snapshot.objects.map((object) => (
                        <tr key={object.id} className="border-t">
                          <td className="p-2">
                            <div>
                              {t(object.label, {
                                ns: "objects",
                                defaultValue: object.label,
                              })}
                            </div>
                            <div className="font-mono text-xs">{object.id}</div>
                          </td>
                          <td className="p-2">
                            {percent.format(object.score)}
                          </td>
                          <td className="p-2">
                            {number.format(object.stationary_frames)}
                          </td>
                          <td className="p-2">
                            {t("stationaryObjects.seconds", {
                              value: number.format(object.tracked_seconds),
                            })}
                          </td>
                          <td className="p-2">
                            {number.format(object.position_changes)}
                          </td>
                          <td className="p-2">
                            {t(
                              object.updated_in_current_frame
                                ? "stationaryObjects.current"
                                : "stationaryObjects.notCurrent",
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {snapshot.total > snapshot.objects.length && (
                <p>
                  {t("stationaryObjects.truncated", {
                    shown: snapshot.objects.length,
                    total: snapshot.total,
                  })}
                </p>
              )}
            </>
          )}
        </div>
      )}
    </section>
  );
}

export default function StationaryObjectsInspector({
  formContext,
  selectedCamera,
}: SectionRendererProps) {
  const { data: profile } = useSWR<User>("profile");
  const camera = selectedCamera ?? formContext?.cameraName;
  if (
    formContext?.level !== "camera" ||
    !camera ||
    !profile?.username ||
    !profile.role
  )
    return null;
  return (
    <Inspector
      key={JSON.stringify([camera, profile.username, profile.role])}
      camera={camera}
    />
  );
}
