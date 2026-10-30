import axios from "axios";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import type {
  DiagnosticsExplanation,
  DiagnosticsReport,
} from "@/types/diagnostics";

export default function DiagnosticsMetrics({
  isActive,
  setLastUpdated,
}: {
  isActive: boolean;
  setLastUpdated: (value: number) => void;
}) {
  const { t, i18n } = useTranslation("views/system");
  const { data, error, isValidating, mutate } = useSWR<DiagnosticsReport>(
    isActive ? "diagnostics" : null,
    { refreshInterval: isActive ? 10000 : 0, refreshWhenHidden: false },
  );
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [aiError, setAiError] = useState(false);
  const [explanation, setExplanation] = useState<DiagnosticsExplanation>();
  const request = useRef<AbortController | undefined>(undefined);
  const format = useMemo(
    () => new Intl.NumberFormat(i18n.language, { maximumFractionDigits: 1 }),
    [i18n.language],
  );
  const number = (value: number | null) =>
    value === null ? t("diagnostics.unknown") : format.format(value);
  useEffect(() => {
    if (data) setLastUpdated(data.sampled_at);
  }, [data, setLastUpdated]);
  useEffect(() => {
    if (!isActive) {
      request.current?.abort();
      setBusy(false);
    }
    return () => request.current?.abort();
  }, [isActive]);
  const explain = async () => {
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setAiError(false);
    setExplanation(undefined);
    try {
      const response = await axios.post<DiagnosticsExplanation>(
        "diagnostics/explain",
        { confirm_send_metrics: true },
        { signal: controller.signal, timeout: 100000 },
      );
      if (!controller.signal.aborted) setExplanation(response.data);
    } catch (error) {
      if (!axios.isCancel(error) && !controller.signal.aborted)
        setAiError(true);
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  };
  return (
    <div className="mt-4 flex-1 space-y-4 overflow-y-auto pb-8">
      <section className="space-y-2 rounded-lg border p-4">
        <h2 className="text-lg font-medium">{t("diagnostics.title")}</h2>
        <p className="text-sm text-muted-foreground">
          {t("diagnostics.description")}
        </p>
        <p className="text-sm">{t("diagnostics.recordingCaveat")}</p>
        <Button
          variant="outline"
          disabled={isValidating}
          onClick={() => void mutate()}
        >
          {t("diagnostics.refresh")}
        </Button>
      </section>
      {error ? (
        <p role="alert">{t("diagnostics.readFailed")}</p>
      ) : !data ? (
        <p role="status">{t("diagnostics.loading")}</p>
      ) : (
        <>
          {data.stale && <p role="alert">{t("diagnostics.stale")}</p>}
          {data.truncated && (
            <p role="status">
              {t("diagnostics.truncated", { total: data.total_cameras })}
            </p>
          )}
          <section
            className="space-y-3"
            aria-label={t("diagnostics.findingsTitle")}
          >
            {data.findings.map((finding, index) => (
              <article
                key={`${finding.code}-${finding.camera}-${index}`}
                className="space-y-2 rounded-lg border p-4"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span
                    className={
                      finding.severity === "error"
                        ? "font-medium text-danger"
                        : finding.severity === "warning"
                          ? "font-medium text-warning"
                          : "font-medium"
                    }
                  >
                    {t(`diagnostics.severity.${finding.severity}`)}
                  </span>
                  <span className="text-sm text-muted-foreground">
                    {t(`diagnostics.confidence.${finding.confidence}`)}
                  </span>
                  {finding.camera && (
                    <span className="font-mono text-sm">{finding.camera}</span>
                  )}
                </div>
                <p>{t(`diagnostics.findings.${finding.code}`)}</p>
                <dl className="flex flex-wrap gap-x-5 gap-y-1 text-sm text-muted-foreground">
                  {Object.entries(finding.evidence).map(([key, value]) => (
                    <div key={key}>
                      <dt className="inline">
                        {t(`diagnostics.evidence.${key}`)}:{" "}
                      </dt>
                      <dd className="inline tabular-nums">{number(value)}</dd>
                    </div>
                  ))}
                </dl>
              </article>
            ))}
          </section>
          <section className="overflow-auto rounded-lg border p-4">
            <h3 className="mb-3 font-medium">{t("diagnostics.cameraTitle")}</h3>
            <table className="w-full text-left text-sm">
              <thead>
                <tr>
                  {[
                    "camera",
                    "capture",
                    "tracker",
                    "published",
                    "captureSkipped",
                    "outputDropped",
                    "analysis",
                    "source",
                  ].map((key) => (
                    <th key={key} className="whitespace-nowrap p-2">
                      {t(`diagnostics.columns.${key}`)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.cameras.map((camera) => (
                  <tr key={camera.camera} className="border-t">
                    <td className="p-2">{camera.camera}</td>
                    {[
                      camera.capture_fps,
                      camera.tracker_fps,
                      camera.process_fps,
                      camera.skipped_fps,
                      camera.output_dropped_fps,
                      camera.analysis_height,
                      camera.source_height,
                    ].map((value, index) => (
                      <td key={index} className="p-2 tabular-nums">
                        {number(value)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-3 text-xs text-muted-foreground">
              {t("diagnostics.ratesHelp")}
            </p>
          </section>
          <section className="space-y-2 rounded-lg border p-4">
            <h3 className="font-medium">{t("diagnostics.hardwareTitle")}</h3>
            {data.gpus.map((gpu) => (
              <p key={gpu.gpu} className="text-sm">
                {t("diagnostics.gpu", {
                  gpu: gpu.gpu,
                  decoder: number(gpu.decoder_percent),
                  compute: number(gpu.compute_percent),
                  temperature: number(gpu.temperature),
                })}
              </p>
            ))}
            <p className="text-sm">
              {t("diagnostics.detectors", {
                values:
                  data.detector_inference_ms.map(number).join(", ") ||
                  t("diagnostics.unknown"),
              })}
            </p>
          </section>
          <section className="space-y-3 rounded-lg border p-4">
            <h3 className="font-medium">{t("diagnostics.aiTitle")}</h3>
            <p className="text-sm text-muted-foreground">
              {t("diagnostics.aiHelp")}
            </p>
            {!data.ai_available && (
              <p role="status">{t("diagnostics.aiUnavailable")}</p>
            )}
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                checked={consent}
                disabled={busy || !data.ai_available}
                onChange={(event) => setConsent(event.target.checked)}
              />
              {t("diagnostics.consent")}
            </label>
            <Button
              disabled={!consent || busy || !data.ai_available || data.stale}
              onClick={() => void explain()}
            >
              {t(busy ? "diagnostics.explaining" : "diagnostics.explain")}
            </Button>
            {aiError && <p role="alert">{t("diagnostics.aiFailed")}</p>}
            {explanation && (
              <div className="space-y-2">
                <p className="text-xs text-muted-foreground">
                  {t("diagnostics.aiSnapshot", {
                    time: new Date(
                      explanation.based_on_stats_time * 1000,
                    ).toLocaleString(i18n.language),
                  })}
                </p>
                <p className="whitespace-pre-wrap text-sm">
                  {explanation.text}
                </p>
              </div>
            )}
          </section>
          <p className="text-xs text-muted-foreground">
            {t("diagnostics.coverage")}
          </p>
        </>
      )}
    </div>
  );
}
