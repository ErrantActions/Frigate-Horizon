import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { TimeRange } from "@/types/timeline";
import { parseCaptureTimestamp } from "@/utils/playbackNavigation";
import RecordingBookmarks from "./RecordingBookmarks";
import ReviewMetadata from "./ReviewMetadata";
import type { ReviewSegment } from "@/types/review";

type Props = {
  camera: string;
  timestamp: number;
  timezone: string;
  review?: ReviewSegment;
  selection?: TimeRange;
  inpoint?: number;
  loop: boolean;
  skipQuiet: boolean;
  hasActivity: boolean;
  navigationUnavailable: boolean;
  bounds: TimeRange;
  onMarkIn: () => void;
  onMarkOut: () => void;
  onLoop: (enabled: boolean) => void;
  onSkipQuiet: (enabled: boolean) => void;
  onClear: () => void;
  onExport: () => void;
  onShare: () => void;
  onSeek: (timestamp: number) => void;
};

export default function InvestigationToolbar({
  camera,
  timestamp,
  timezone,
  review,
  selection,
  inpoint,
  loop,
  skipQuiet,
  hasActivity,
  navigationUnavailable,
  bounds,
  onMarkIn,
  onMarkOut,
  onLoop,
  onSkipQuiet,
  onClear,
  onExport,
  onShare,
  onSeek,
}: Props) {
  const { t } = useTranslation("components/player");
  const [jump, setJump] = useState("");
  const [invalid, setInvalid] = useState(false);
  const [toolsOpen, setToolsOpen] = useState(false);
  const format = (value: number) =>
    new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "long",
      timeZone: timezone,
    }).format(value * 1000);
  return (
    <section
      aria-label={t("investigation.title")}
      className="max-h-[35dvh] w-full shrink-0 overflow-y-auto rounded-xl border bg-background_alt p-3 text-sm"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <time
          dateTime={new Date(timestamp * 1000).toISOString()}
          className="font-mono tabular-nums"
        >
          {format(timestamp)}{" "}
          <span className="text-xs text-muted-foreground">({timezone})</span>
        </time>
        <Button size="sm" variant="outline" onClick={onShare}>
          {t("investigation.share")}
        </Button>
      </div>
      {inpoint !== undefined && (
        <p className="mt-2 text-xs text-muted-foreground">
          {selection
            ? t("investigation.range", {
                start: format(selection.after),
                end: format(selection.before),
              })
            : t("investigation.inpoint", { time: format(inpoint) })}
        </p>
      )}
      <details
        className="mt-2"
        onToggle={(event) => {
          if (event.target === event.currentTarget)
            setToolsOpen(event.currentTarget.open);
        }}
      >
        <summary className="cursor-pointer font-medium">
          <span>{t("investigation.tools")}</span>
          {(inpoint !== undefined || loop || skipQuiet) && (
            <span className="ml-2 text-xs text-muted-foreground">
              {t("investigation.active")}
            </span>
          )}
        </summary>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Button size="sm" variant="outline" onClick={onMarkIn}>
            {t("investigation.markIn")}
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={inpoint === undefined || timestamp <= inpoint}
            onClick={onMarkOut}
          >
            {t("investigation.markOut")}
          </Button>
          <Button
            size="sm"
            variant={loop ? "select" : "outline"}
            disabled={!selection}
            aria-pressed={loop}
            onClick={() => onLoop(!loop)}
          >
            {t("investigation.loop")}
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={!selection}
            onClick={onExport}
          >
            {t("investigation.export")}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            disabled={inpoint === undefined}
            onClick={onClear}
          >
            {t("investigation.clear")}
          </Button>
        </div>
        <details className="mt-2">
          <summary className="cursor-pointer font-medium">
            {t("investigation.navigation")}
          </summary>
          {navigationUnavailable && (
            <p role="status" className="mt-2 text-danger">
              {t("investigation.invalidActivity")}
            </p>
          )}
          <label className="mt-3 flex items-center gap-2">
            <input
              type="checkbox"
              checked={skipQuiet}
              disabled={!hasActivity || loop}
              onChange={(event) => onSkipQuiet(event.target.checked)}
            />
            {t("investigation.skip")}
          </label>
          <p className="mt-1 text-xs text-muted-foreground">
            {t("investigation.skipHint")}
          </p>
          <form
            className="mt-3 flex flex-wrap items-end gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              const value = parseCaptureTimestamp(jump);
              if (
                value === undefined ||
                value < bounds.after ||
                value >= bounds.before
              ) {
                setInvalid(true);
                return;
              }
              setInvalid(false);
              onSeek(value);
            }}
          >
            <label className="min-w-0 flex-1 space-y-1">
              <span>{t("investigation.jump")}</span>
              <Input
                type="text"
                placeholder={t("investigation.jumpExample")}
                value={jump}
                onChange={(event) => setJump(event.target.value)}
                required
              />
            </label>
            <Button size="sm" type="submit">
              {t("investigation.go")}
            </Button>
          </form>
          {invalid && (
            <p role="alert" className="mt-2 text-danger">
              {t("investigation.invalidTime")}
            </p>
          )}
          <p className="mt-1 text-xs text-muted-foreground">
            {t("investigation.jumpHint")}
          </p>
        </details>
        <ReviewMetadata
          camera={camera}
          review={review}
          timezone={timezone}
          visible={toolsOpen}
        />
        <RecordingBookmarks
          key={`${camera}:${bounds.after}:${bounds.before}`}
          camera={camera}
          timestamp={timestamp}
          selection={selection}
          bounds={bounds}
          timezone={timezone}
          visible={toolsOpen}
          onSeek={onSeek}
        />
      </details>
    </section>
  );
}
