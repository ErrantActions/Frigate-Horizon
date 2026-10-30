import { useContext, useEffect, useId, useState } from "react";
import axios from "axios";
import { useTranslation } from "react-i18next";
import { AuthContext } from "@/context/auth-context";
import { useAllowedCameras } from "@/hooks/use-allowed-cameras";
import { Button } from "@/components/ui/button";
import type { ReviewSegment } from "@/types/review";
import type { Event } from "@/types/event";

type Props = {
  camera: string;
  review?: ReviewSegment;
  timezone: string;
  visible?: boolean;
};

const EVENT_LIMIT = 50;
const text = (value: unknown) =>
  typeof value === "string" && value.trim() ? value.slice(0, 500) : undefined;
const strings = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.flatMap((item) => {
        const selected = text(item);
        return selected ? [selected] : [];
      })
    : [];
const record = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
const finite = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);
const geometry = (value: unknown) =>
  Array.isArray(value) && value.length === 4 && value.every(finite)
    ? value.join(", ")
    : undefined;
const confidence = (value: unknown) =>
  finite(value) && value >= 0 && value <= 1
    ? `${Math.round(value * 1000) / 10}%`
    : undefined;

// Keep only display fields, never thumbnails, paths, arbitrary attributes or payload dumps.
function retainedEvent(value: unknown, camera: string, ids: string[]) {
  const event = record(value);
  if (
    event.camera !== camera ||
    typeof event.id !== "string" ||
    !ids.includes(event.id)
  )
    return undefined;
  const data = record(event.data);
  return {
    id: event.id,
    label: text(event.label),
    identity: text(event.sub_label),
    plate: text(data.recognized_license_plate),
    start: finite(event.start_time) ? event.start_time : undefined,
    end: finite(event.end_time) ? event.end_time : undefined,
    zones: strings(event.zones),
    confidence: confidence(data.top_score ?? data.score),
    identityConfidence: confidence(data.sub_label_score),
    box: geometry(data.box),
    region: geometry(data.region),
    area: finite(data.area) && data.area >= 0 ? data.area : undefined,
    type: text(data.type),
    model: text(event.model_hash),
    detector: text(event.detector_type),
    modelType: text(event.model_type),
  };
}

type RetainedEvent = NonNullable<ReturnType<typeof retainedEvent>>;

export default function ReviewMetadata(props: Props) {
  const { auth } = useContext(AuthContext);
  const allowed = useAllowedCameras();
  if (
    auth.isLoading ||
    (auth.isAuthenticated && !auth.user) ||
    !allowed.includes(props.camera)
  )
    return null;
  const review =
    props.review?.camera === props.camera ? props.review : undefined;
  const ids = [...new Set(strings(review?.data?.detections))];
  const identity = JSON.stringify([
    auth.user?.username ?? "anonymous",
    auth.user?.role,
    props.camera,
    review?.id,
    ids,
  ]);
  return (
    <MetadataPanel
      key={identity}
      {...props}
      review={review}
      ids={ids.slice(0, EVENT_LIMIT)}
      total={ids.length}
    />
  );
}

function MetadataPanel({
  camera,
  review,
  timezone,
  ids,
  total,
  visible = true,
}: Props & { ids: string[]; total: number }) {
  const { t } = useTranslation("components/player");
  const panelId = useId();
  const [open, setOpen] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<RetainedEvent[]>();
  const [error, setError] = useState(false);
  const idsKey = JSON.stringify(ids);

  useEffect(() => {
    if (!visible || !open || !ids.length) return;
    const controller = new AbortController();
    const requested: string[] = JSON.parse(idsKey);
    setResult(undefined);
    setError(false);
    void axios
      .get<Event[]>("event_ids", {
        params: { ids: requested.join(",") },
        signal: controller.signal,
      })
      .then(({ data }) => {
        if (controller.signal.aborted) return;
        if (!Array.isArray(data)) throw new Error();
        const selected = data.flatMap((event) => {
          const item = retainedEvent(event, camera, requested);
          return item ? [item] : [];
        });
        setResult([
          ...new Map(selected.map((item) => [item.id, item])).values(),
        ]);
      })
      .catch(() => {
        if (!controller.signal.aborted) setError(true);
      });
    return () => controller.abort();
  }, [visible, open, camera, idsKey, ids.length, attempt]);

  const unknown = t("metadata.unknown");
  const format = (value: unknown) => {
    if (!finite(value) || !Number.isFinite(new Date(value * 1000).getTime()))
      return unknown;
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "long",
      timeZone: timezone,
    }).format(value * 1000);
  };
  const row = (label: string, value: React.ReactNode) => (
    <div className="min-w-0">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="break-words">{value ?? unknown}</dd>
    </div>
  );
  const list = (values: unknown) => strings(values).join(", ") || unknown;
  const missing = result
    ? ids.filter((id) => !result.some((event) => event.id === id)).length
    : 0;

  return (
    <div className="mt-3 border-t pt-2">
      <Button
        variant="ghost"
        size="sm"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => {
          setResult(undefined);
          setError(false);
          setOpen((current) => !current);
        }}
      >
        {t("metadata.title")}
      </Button>
      {open && (
        <section
          id={panelId}
          aria-label={t("metadata.title")}
          className="mt-2 space-y-3 text-xs"
        >
          {!review ? (
            <p role="status">{t("metadata.noReview")}</p>
          ) : (
            <>
              <dl className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                {row(t("metadata.camera"), camera)}
                {row(t("metadata.reviewId"), review.id)}
                {row(
                  t("metadata.severity"),
                  ["alert", "detection", "significant_motion"].includes(
                    review.severity,
                  )
                    ? t(`metadata.severities.${review.severity}`)
                    : unknown,
                )}
                {row(t("metadata.timezone"), timezone)}
                {row(t("metadata.start"), format(review.start_time))}
                {row(
                  t("metadata.end"),
                  review.end_time == null
                    ? t("metadata.ongoing")
                    : format(review.end_time),
                )}
                {row(t("metadata.labels"), list(review.data?.objects))}
                {row(t("metadata.audio"), list(review.data?.audio))}
                {row(t("metadata.identities"), list(review.data?.sub_labels))}
                {row(t("metadata.zones"), list(review.data?.zones))}
                {row(
                  t("metadata.summary"),
                  [
                    text(review.data?.metadata?.title),
                    text(review.data?.metadata?.scene),
                  ]
                    .filter(Boolean)
                    .join(": ") || unknown,
                )}
                {row(
                  t("metadata.storage"),
                  review.recording_storage &&
                    ["local", "archive", "mixed"].includes(
                      review.recording_storage,
                    )
                    ? t(`metadata.tiers.${review.recording_storage}`)
                    : unknown,
                )}
              </dl>
              <p>{t("metadata.storageHint")}</p>
              <h3 className="font-medium">{t("metadata.events")}</h3>
              <p>
                {t("metadata.limit", {
                  count: total,
                  requested: ids.length,
                  limit: EVENT_LIMIT,
                })}
              </p>
              {!ids.length ? (
                <p role="status">{t("metadata.noEvents")}</p>
              ) : error ? (
                <div role="alert">
                  <p>{t("metadata.failed")}</p>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      setError(false);
                      setAttempt((current) => current + 1);
                    }}
                  >
                    {t("metadata.retry")}
                  </Button>
                </div>
              ) : !result ? (
                <p role="status">{t("metadata.loading")}</p>
              ) : (
                <>
                  {missing > 0 && (
                    <p role="status">
                      {t("metadata.missing", { count: missing })}
                    </p>
                  )}
                  {result.map((event) => (
                    <article
                      key={event.id}
                      aria-label={t("metadata.event", { id: event.id })}
                      className="rounded border p-2"
                    >
                      <dl className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                        {row(t("metadata.eventId"), event.id)}
                        {row(t("metadata.labels"), event.label)}
                        {row(t("metadata.start"), format(event.start))}
                        {row(t("metadata.end"), format(event.end))}
                        {row(t("metadata.zones"), list(event.zones))}
                        {row(t("metadata.confidence"), event.confidence)}
                        {row(t("metadata.identities"), event.identity)}
                        {row(
                          t("metadata.identityConfidence"),
                          event.identityConfidence,
                        )}
                        {row(t("metadata.plate"), event.plate)}
                        {row(t("metadata.box"), event.box)}
                        {row(t("metadata.region"), event.region)}
                        {row(t("metadata.area"), event.area)}
                        {row(t("metadata.source"), event.type)}
                        {row(t("metadata.model"), event.model)}
                        {row(t("metadata.detector"), event.detector)}
                        {row(t("metadata.modelType"), event.modelType)}
                      </dl>
                    </article>
                  ))}
                  <p>{t("metadata.geometryHint")}</p>
                </>
              )}
            </>
          )}
          <p>{t("metadata.timingHint")}</p>
          <dl className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {row(t("metadata.weather"), t("metadata.notRecorded"))}
            {row(t("metadata.adaptive"), t("metadata.notRecorded"))}
          </dl>
        </section>
      )}
    </div>
  );
}
