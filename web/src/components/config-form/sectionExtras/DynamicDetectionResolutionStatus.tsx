import { useFrigateStats } from "@/api/ws";
import type { CameraConfig } from "@/types/frigateConfig";
import type { FrigateStats } from "@/types/stats";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import useSWR from "swr";

export default function DynamicDetectionResolutionStatus({
  camera,
  config,
}: {
  camera: string;
  config?: CameraConfig;
}) {
  const { t } = useTranslation("views/settings");
  const liveStats = useFrigateStats();
  const { data: polledStats } = useSWR<FrigateStats>("stats", {
    refreshInterval: 5000,
    refreshWhenHidden: false,
  });
  const stats =
    polledStats &&
    (!liveStats ||
      polledStats.service.last_updated > liveStats.service.last_updated)
      ? polledStats
      : liveStats;
  const [now, setNow] = useState(Date.now());
  const [receivedAt, setReceivedAt] = useState(Date.now());
  useEffect(() => {
    setReceivedAt(Date.now());
  }, [stats]);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 2000);
    return () => clearInterval(timer);
  }, []);
  const applied = stats?.cameras?.[camera]?.dynamic_resolution;
  const saved = config?.detect.dynamic_resolution;
  const pending =
    applied &&
    ((saved?.enabled ?? false) !== applied.enabled ||
      (saved?.min_height ?? 360) !== applied.min_height ||
      (saved?.max_height ?? null) !== applied.max_height);

  return (
    <div
      className="mt-3 space-y-2 border-t pt-3"
      aria-label={t("dynamicDetection.title")}
    >
      <p className="font-medium">{t("dynamicDetection.title")}</p>
      <p className="text-sm text-muted-foreground">
        {t("dynamicDetection.help")}
      </p>
      <p role="status" className="text-sm">
        {!config?.enabled
          ? t("dynamicDetection.cameraDisabled")
          : !applied ||
              Math.max(0, stats.service.last_updated - applied.sampled_at) +
                Math.max(0, now - receivedAt) / 1000 >
                30
            ? t("dynamicDetection.unavailable")
            : applied.error
              ? t("dynamicDetection.captureResizeRequired")
              : pending
                ? t("dynamicDetection.pending")
                : t(`dynamicDetection.status.${applied.reason}`, {
                    width: applied.width,
                    height: applied.height,
                  })}
      </p>
    </div>
  );
}
