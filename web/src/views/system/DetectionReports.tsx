import { useContext, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import useSWR from "swr";
import { AuthContext } from "@/context/auth-context";
import { Button } from "@/components/ui/button";
import { useAllowedCameras } from "@/hooks/use-allowed-cameras";
import { useIsAdmin } from "@/hooks/use-is-admin";
import { useTimezone } from "@/hooks/use-date-utils";
import { useCameraDetectionReport } from "@/hooks/use-camera-detection-report";
import type { CameraConfig, FrigateConfig } from "@/types/frigateConfig";
import type { CameraStats, FrigateStats } from "@/types/stats";
import { REPORT_LABELS, type DetectionHealth } from "@/types/detectionReport";
import {
  nativeDetectionHealth,
  reportAge,
  recordingReportHealth,
} from "@/utils/detectionReportUtil";

function Health({ value }: { value: DetectionHealth }) {
  const { t } = useTranslation("views/system");
  const color =
    value === "reporting"
      ? "text-success"
      : value === "error" || value === "stale"
        ? "text-danger"
        : "text-muted-foreground";
  return (
    <span className={`font-medium ${color}`}>
      {t(`detectionReports.states.${value}`)}
    </span>
  );
}

function CameraReport({
  camera,
  stats,
  statsSampledAt,
  isActive,
  refresh,
  timezone,
  setLastUpdated,
}: {
  camera: CameraConfig;
  stats: CameraStats | undefined;
  statsSampledAt: number | undefined;
  isActive: boolean;
  refresh: number;
  timezone: string | undefined;
  setLastUpdated: (value: number) => void;
}) {
  const { t, i18n } = useTranslation("views/system");
  const { data, readFailed, now, lastNative } = useCameraDetectionReport(
    camera,
    isActive,
    refresh,
    setLastUpdated,
  );
  const date = useMemo(
    () =>
      new Intl.DateTimeFormat(i18n.language, {
        dateStyle: "short",
        timeStyle: "medium",
        timeZone: timezone,
      }),
    [i18n.language, timezone],
  );
  const number = useMemo(
    () =>
      new Intl.NumberFormat(i18n.language, {
        maximumFractionDigits: 1,
      }),
    [i18n.language],
  );

  const currentNative = data?.native ?? lastNative?.value;
  const native = currentNative ?? null;
  const observed =
    data?.native?.updated_at !== undefined
      ? {
          value: data.native,
          serverTime: data.nativeServerTime,
          receivedAt: data.nativeReceivedAt,
        }
      : lastNative;
  const nativeAge = observed
    ? reportAge(
        observed.value.updated_at,
        observed.serverTime,
        (now - observed.receivedAt) / 1000,
      )
    : null;
  const recordingElapsed = data ? (now - data.recordingReceivedAt) / 1000 : 0;
  const currentServerTime =
    data?.recordingServerTime ?? data?.nativeServerTime ?? null;
  const statsReceivedAt =
    data?.recordingServerTime !== null
      ? data?.recordingReceivedAt
      : data?.nativeReceivedAt;
  const statsAge = reportAge(
    statsSampledAt,
    currentServerTime,
    statsReceivedAt === undefined ? 0 : (now - statsReceivedAt) / 1000,
  );
  let health: DetectionHealth = !camera.enabled ? "disabled" : "starting";
  let source: "unknown" | "reolink" | "frigate" | "none" = "unknown";
  if (native) {
    source = "reolink";
    health = nativeDetectionHealth(
      native,
      Boolean(data?.nativeFailed || readFailed),
      nativeAge,
    );
  } else if (stats?.capture_mode === "decoded_detection") {
    source = "frigate";
    health =
      statsAge === null
        ? "unknown"
        : statsAge > 45
          ? "stale"
          : !stats.detection_enabled
            ? "disabled"
            : stats.camera_fps > 0 &&
                stats.process_fps > 0 &&
                stats.ffmpeg_pid > 0
              ? "reporting"
              : "error";
  } else if (data?.nativeFailed || readFailed) {
    health = "error";
  } else if (data && stats?.capture_mode === "recording_only") {
    source = "none";
    health = "disabled";
  }
  if (!camera.enabled) health = "disabled";
  const recordingAge = data
    ? reportAge(data.recordingEnd, data.recordingServerTime, recordingElapsed)
    : null;
  const recordingHealth = recordingReportHealth(
    data,
    camera.enabled,
    camera.record.enabled,
    now,
    readFailed,
  );
  const timestamp = (value: number | undefined | null) =>
    value == null
      ? t("detectionReports.unknown")
      : date.format(new Date(value * 1000));
  const age = (value: number | null) =>
    value === null
      ? t("detectionReports.unknown")
      : t("detectionReports.seconds", { value: number.format(value) });
  const reports = [...(native?.history ?? [])]
    .sort((a, b) => b.received_at - a.received_at)
    .slice(0, 4);

  return (
    <article
      className="space-y-3 rounded-lg border p-4"
      data-testid={`detection-report-${camera.name}`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-medium">
          {camera.friendly_name || camera.name.replaceAll("_", " ")}
        </h3>
        <div data-testid="detection-health">
          <Health value={health} />
        </div>
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
        <dt className="text-muted-foreground">
          {t("detectionReports.source")}
        </dt>
        <dd>{t(`detectionReports.sources.${source}`)}</dd>
        <dt className="text-muted-foreground">
          {t(
            source === "frigate"
              ? "detectionReports.lastStats"
              : "detectionReports.lastPoll",
          )}
        </dt>
        <dd>
          {timestamp(
            source === "frigate" ? statsSampledAt : observed?.value.updated_at,
          )}
        </dd>
        <dt className="text-muted-foreground">
          {t("detectionReports.reportAge")}
        </dt>
        <dd>{age(source === "frigate" ? statsAge : nativeAge)}</dd>
        <dt className="text-muted-foreground">
          {t("detectionReports.recording")}
        </dt>
        <dd data-testid="recording-health">
          <Health value={recordingHealth} />
        </dd>
        <dt className="text-muted-foreground">
          {t("detectionReports.segmentAge")}
        </dt>
        <dd>{age(recordingAge)}</dd>
        <dt className="text-muted-foreground">
          {t("detectionReports.decoder")}
        </dt>
        <dd>
          {statsAge === null || statsAge > 45 || !stats
            ? t("detectionReports.unknown")
            : t(
                stats.ffmpeg_pid > 0
                  ? "detectionReports.decoderOn"
                  : "detectionReports.decoderOff",
              )}
        </dd>
        {native && (
          <>
            <dt className="text-muted-foreground">
              {t("detectionReports.sessionCounts")}
            </dt>
            <dd>
              {t("detectionReports.counts", {
                opened: native.events_started,
                closed: native.events_ended,
              })}
            </dd>
          </>
        )}
        {source === "frigate" && (
          <>
            <dt className="text-muted-foreground">
              {t("detectionReports.inferenceRate")}
            </dt>
            <dd>
              {statsAge !== null && statsAge <= 45 && stats
                ? number.format(stats.detection_fps)
                : t("detectionReports.unknown")}
            </dd>
          </>
        )}
      </dl>
      {camera.enabled &&
        (health === "error" || health === "stale" || health === "unknown") && (
          <p role="alert" className="text-sm text-danger">
            {t("detectionReports.unavailable")}
          </p>
        )}
      {native && (
        <>
          <div className="flex flex-wrap gap-2">
            {REPORT_LABELS.filter(
              (key) => native.states[key] !== undefined,
            ).map((key) => (
              <span key={key} className="rounded border px-2 py-1 text-xs">
                {t(`detectionReports.labels.${key}`)}:{" "}
                {health === "reporting"
                  ? t(
                      native.states[key]
                        ? "detectionReports.active"
                        : "detectionReports.clear",
                    )
                  : t("detectionReports.unknown")}
              </span>
            ))}
          </div>
          <div className="space-y-1 text-sm">
            <h4 className="font-medium">
              {t("detectionReports.recentReports")}
            </h4>
            {reports.length ? (
              reports.map((item, index) => (
                <p
                  key={`${item.received_at}-${item.label}-${index}`}
                  className="text-muted-foreground"
                >
                  {timestamp(item.received_at)}:{" "}
                  {t(`detectionReports.labels.${item.label}`)}{" "}
                  {t(
                    item.active
                      ? "detectionReports.active"
                      : "detectionReports.clear",
                  )}
                </p>
              ))
            ) : (
              <p className="text-muted-foreground">
                {t("detectionReports.noTransitions")}
              </p>
            )}
          </div>
        </>
      )}
    </article>
  );
}

export default function DetectionReports({
  isActive,
  setLastUpdated,
}: {
  isActive: boolean;
  setLastUpdated: (value: number) => void;
}) {
  const { t } = useTranslation("views/system");
  const { auth } = useContext(AuthContext);
  const isAdmin = useIsAdmin();
  const authorized =
    !auth.isLoading && (!auth.isAuthenticated || auth.user?.role === "admin");
  const allowed = useAllowedCameras();
  const { data: config } = useSWR<FrigateConfig>("config", {
    revalidateOnFocus: false,
  });
  const { data: stats } = useSWR<FrigateStats>(
    isActive && authorized ? "stats" : null,
    {
      refreshInterval: isActive ? 10000 : 0,
      refreshWhenHidden: false,
    },
  );
  const timezone = useTimezone(config);
  const [refresh, setRefresh] = useState(0);
  const identity = JSON.stringify([
    auth.user?.username,
    auth.user?.role,
    auth.isAuthenticated,
  ]);
  if (auth.isLoading || (auth.isAuthenticated && !auth.user)) return null;
  if (!isAdmin)
    return <p role="alert">{t("detectionReports.adminRequired")}</p>;
  const cameras = allowed
    .flatMap((name) => (config?.cameras[name] ? [config.cameras[name]] : []))
    .sort((a, b) =>
      (a.friendly_name || a.name).localeCompare(b.friendly_name || b.name),
    );
  return (
    <div className="mt-4 flex-1 space-y-4 overflow-y-auto pb-8">
      <section className="space-y-2 rounded-lg border p-4">
        <h2 className="text-lg font-medium">{t("detectionReports.title")}</h2>
        <p className="text-sm text-muted-foreground">
          {t("detectionReports.description")}
        </p>
        <p className="text-sm text-muted-foreground">
          {t("detectionReports.caveat")}
        </p>
        <Button
          variant="outline"
          onClick={() => setRefresh((value) => value + 1)}
        >
          {t("detectionReports.refresh")}
        </Button>
      </section>
      {!config ? (
        <p role="status">{t("detectionReports.loading")}</p>
      ) : (
        <div className="grid gap-4 xl:grid-cols-2">
          {cameras.map((camera) => (
            <CameraReport
              key={`${identity}:${camera.name}`}
              camera={camera}
              stats={stats?.cameras[camera.name]}
              statsSampledAt={stats?.service.last_updated}
              isActive={isActive}
              refresh={refresh}
              timezone={timezone}
              setLastUpdated={setLastUpdated}
            />
          ))}
        </div>
      )}
    </div>
  );
}
