import { useApiHost } from "@/api";
import { Button } from "@/components/ui/button";
import {
  AdaptiveMotionCell,
  AdaptiveMotionField,
  AdaptiveMotionSnapshot,
  adaptiveMotionFields,
} from "@/types/adaptiveMotion";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

const POLL_MS = 2000;
const TIMEOUT_MS = 5000;

function validGrid(snapshot: AdaptiveMotionSnapshot) {
  const grid = snapshot.grid;
  return (
    grid &&
    [
      grid.columns,
      grid.rows,
      grid.analysis_width,
      grid.analysis_height,
      grid.display_columns,
      grid.display_rows,
    ].every((value) => Number.isInteger(value) && value >= 0) &&
    grid.display_columns <= grid.columns &&
    grid.display_rows <= grid.rows &&
    grid.display_columns * grid.display_rows <= 4096 &&
    Array.isArray(grid.values) &&
    grid.values.length === grid.display_columns * grid.display_rows &&
    grid.values.every(Number.isFinite) &&
    (grid.minimum === undefined || Number.isFinite(grid.minimum)) &&
    (grid.maximum === undefined || Number.isFinite(grid.maximum)) &&
    (grid.minimum === undefined ||
      grid.maximum === undefined ||
      grid.minimum <= grid.maximum)
  );
}

function minuteLabel(minute: number) {
  return `${String(Math.floor(minute / 60) % 24).padStart(2, "0")}:${String(minute % 60).padStart(2, "0")}`;
}

export default function AdaptiveMotionInspector({
  camera,
}: {
  camera: string;
}) {
  const { t, i18n } = useTranslation(["views/settings"]);
  const apiHost = useApiHost();
  const [field, setField] = useState<AdaptiveMotionField>("adjustment");
  const [selection, setSelection] = useState<{ column: number; row: number }>();
  const [snapshot, setSnapshot] = useState<AdaptiveMotionSnapshot>();
  const [failure, setFailure] = useState<
    "unavailable" | "request" | "invalid"
  >();
  const [visible, setVisible] = useState(!document.hidden);
  const [imageVersion, setImageVersion] = useState<number>();
  const [imageReady, setImageReady] = useState(false);
  const [imageError, setImageError] = useState(false);
  const [zoom, setZoom] = useState(1);
  const [numbers, setNumbers] = useState(false);
  const [showGrid, setShowGrid] = useState(true);
  const [showHeatmap, setShowHeatmap] = useState(true);
  const [liveStream, setLiveStream] = useState(true);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const numberFormat = useMemo(
    () => new Intl.NumberFormat(i18n.language, { maximumFractionDigits: 2 }),
    [i18n.language],
  );

  useEffect(() => {
    if (!camera) return;
    let disposed = false;
    let pollTimer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    let requestTimer: ReturnType<typeof setTimeout> | undefined;
    setSnapshot(undefined);
    setFailure(undefined);

    const poll = async () => {
      if (disposed || document.hidden) return;
      const request = new AbortController();
      controller = request;
      requestTimer = setTimeout(() => request.abort(), TIMEOUT_MS);
      setImageVersion(Date.now());
      try {
        const params = new URLSearchParams({ field, resolution: "64" });
        if (selection) {
          params.set("column", String(selection.column));
          params.set("row", String(selection.row));
        }
        const response = await fetch(
          `${apiHost}api/${encodeURIComponent(camera)}/adaptive-motion?${params}`,
          {
            signal: request.signal,
            cache: "no-store",
            credentials: "same-origin",
          },
        );
        if (disposed || document.hidden || controller !== request) return;
        if (!response.ok) {
          setFailure(response.status === 404 ? "unavailable" : "request");
          setSnapshot(undefined);
          return;
        }
        const data: AdaptiveMotionSnapshot = await response.json();
        if (disposed || document.hidden || controller !== request) return;
        if (
          data.camera !== camera ||
          !["disabled", "shadow", "active"].includes(data.mode) ||
          ![
            "disabled",
            "warming_up",
            "learning",
            "paused",
            "stale",
            "error",
          ].includes(data.status) ||
          data.grid?.field !== field ||
          !validGrid(data)
        ) {
          setFailure("invalid");
          setSnapshot(undefined);
          return;
        }
        setSnapshot(data);
        setFailure(undefined);
      } catch {
        if (!disposed && !document.hidden && controller === request) {
          setFailure("request");
          setSnapshot(undefined);
        }
      } finally {
        if (controller === request) {
          clearTimeout(requestTimer);
          controller = undefined;
          if (!disposed && !document.hidden) {
            pollTimer = setTimeout(poll, POLL_MS);
          }
        }
      }
    };
    const suspend = () => {
      clearTimeout(pollTimer);
      clearTimeout(requestTimer);
      controller?.abort();
      controller = undefined;
    };
    const onVisibility = () => {
      setVisible(!document.hidden);
      suspend();
      if (!document.hidden) void poll();
    };
    document.addEventListener("visibilitychange", onVisibility);
    void poll();
    return () => {
      disposed = true;
      suspend();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [apiHost, camera, field, selection]);

  const grid =
    snapshot?.grid.field === field &&
    snapshot.grid.display_columns > 0 &&
    snapshot.grid.display_rows > 0 &&
    snapshot.grid.analysis_width > 0 &&
    snapshot.grid.analysis_height > 0
      ? snapshot.grid
      : undefined;
  const scale = useMemo(() => {
    const values = grid?.values ?? [0];
    return {
      min: grid?.minimum ?? Math.min(...values),
      max: grid?.maximum ?? Math.max(...values),
    };
  }, [grid]);

  useEffect(() => {
    if (
      grid &&
      selection &&
      (selection.column >= grid.columns || selection.row >= grid.rows)
    ) {
      setSelection(undefined);
    }
  }, [grid, selection]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const draw = () => {
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      if (!width || !height) return;
      const ratio = Math.min(1, 4096 / width, 4096 / height);
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
      const context = canvas.getContext("2d");
      if (!context) return;
      context.scale(ratio, ratio);
      if (!grid || !imageReady) return;
      const cellWidth = width / grid.display_columns;
      const cellHeight = height / grid.display_rows;
      grid.values.forEach((value, index) => {
        const column = index % grid.display_columns;
        const row = Math.floor(index / grid.display_columns);
        const strength =
          Math.abs(value) /
          (value < 0 ? Math.abs(scale.min) || 1 : scale.max || 1);
        context.fillStyle =
          value < 0
            ? `rgba(0, 120, 255, ${strength * 0.7})`
            : `rgba(255, 120, 0, ${strength * 0.7})`;
        if (showHeatmap)
          context.fillRect(
            column * cellWidth,
            row * cellHeight,
            cellWidth,
            cellHeight,
          );
        context.strokeStyle = "rgba(255,255,255,0.35)";
        context.lineWidth = 0.5;
        if (showGrid)
          context.strokeRect(
            column * cellWidth,
            row * cellHeight,
            cellWidth,
            cellHeight,
          );
        const label = numberFormat.format(value);
        context.font = "12px sans-serif";
        if (
          numbers &&
          cellHeight >= 20 &&
          cellWidth >= context.measureText(label).width + 6
        ) {
          context.textAlign = "center";
          context.textBaseline = "middle";
          context.lineWidth = 3;
          context.strokeStyle = "black";
          context.strokeText(
            label,
            (column + 0.5) * cellWidth,
            (row + 0.5) * cellHeight,
          );
          context.fillStyle = "white";
          context.fillText(
            label,
            (column + 0.5) * cellWidth,
            (row + 0.5) * cellHeight,
          );
        }
      });
      if (selection) {
        context.strokeStyle = "yellow";
        context.lineWidth = 3;
        context.strokeRect(
          (selection.column / grid.columns) * width,
          (selection.row / grid.rows) * height,
          width / grid.columns,
          height / grid.rows,
        );
      }
    };
    const observer = new ResizeObserver(draw);
    observer.observe(canvas);
    draw();
    return () => observer.disconnect();
  }, [
    grid,
    imageReady,
    numberFormat,
    numbers,
    scale,
    selection,
    zoom,
    showGrid,
    showHeatmap,
  ]);

  const cell = snapshot?.cell;
  const selectedCell =
    cell &&
    selection &&
    cell.column === selection.column &&
    cell.row === selection.row
      ? cell
      : undefined;
  const cellFields: (keyof AdaptiveMotionCell)[] = [
    "adjustment",
    "applied_adjustment",
    "effective_threshold",
    "activity",
    "episode_count",
    "p",
    "i",
    "d",
    "hold_remaining_seconds",
    "quiet_seconds",
    "decay_rate",
  ];
  const profile = snapshot?.profile;
  const dateLabel = (timestamp: number | null) =>
    timestamp === null
      ? t("adaptiveMotion.unknown")
      : new Date(timestamp * 1000).toLocaleString(i18n.language);

  return (
    <section className="space-y-3 p-2" aria-label={t("adaptiveMotion.title")}>
      <p className="text-sm text-muted-foreground">
        {t("adaptiveMotion.description")}
      </p>
      <p className="text-sm text-muted-foreground">
        {t("adaptiveMotion.gridConfiguration")}
      </p>
      <p className="text-sm text-muted-foreground">
        {t("adaptiveMotion.fieldSemantics")}
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={liveStream}
            onChange={(event) => setLiveStream(event.target.checked)}
          />
          {t("adaptiveMotion.liveStream")}
        </label>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={showGrid}
            onChange={(event) => setShowGrid(event.target.checked)}
          />
          {t("adaptiveMotion.showGrid")}
        </label>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={showHeatmap}
            onChange={(event) => setShowHeatmap(event.target.checked)}
          />
          {t("adaptiveMotion.showHeatmap")}
        </label>
        <label className="flex items-center gap-2">
          {t("adaptiveMotion.field")}
          <select
            aria-label={t("adaptiveMotion.field")}
            className="rounded border bg-background p-2"
            value={field}
            onChange={(event) =>
              setField(event.target.value as AdaptiveMotionField)
            }
          >
            {adaptiveMotionFields.map((value) => (
              <option key={value} value={value}>
                {t(`adaptiveMotion.fields.${value}`)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={numbers}
            onChange={(event) => setNumbers(event.target.checked)}
          />
          {t("adaptiveMotion.numbers")}
        </label>
        <label className="flex items-center gap-2">
          {t("adaptiveMotion.zoom")}
          <input
            aria-label={t("adaptiveMotion.zoom")}
            type="range"
            min={1}
            max={8}
            step={1}
            value={zoom}
            onChange={(event) => setZoom(Number(event.target.value))}
          />
          <output>{t("adaptiveMotion.zoomValue", { zoom })}</output>
        </label>
      </div>
      <div role="status" className="space-y-1 text-sm">
        {!visible && <p>{t("adaptiveMotion.suspended")}</p>}
        {failure ? (
          <p role="alert">{t(`adaptiveMotion.errors.${failure}`)}</p>
        ) : !snapshot ? (
          <p>{t("adaptiveMotion.loading")}</p>
        ) : (
          <>
            <p>
              {t("adaptiveMotion.modeLabel", {
                mode: t(`adaptiveMotion.modes.${snapshot.mode}`),
                status: t(`adaptiveMotion.statuses.${snapshot.status}`),
              })}
            </p>
            <p className="font-medium">
              {t(
                snapshot.applied && snapshot.mode === "active"
                  ? "adaptiveMotion.applied"
                  : "adaptiveMotion.notApplied",
              )}
            </p>
            <p>
              {t("adaptiveMotion.updated", {
                time: dateLabel(snapshot.updated_at),
              })}
            </p>
            <p>
              {t("adaptiveMotion.baseline", {
                baseline: snapshot.baseline_threshold,
                maximum: snapshot.max_adjustment,
                holds: snapshot.positive_holds,
              })}
            </p>
            {snapshot.warning && (
              <p role="alert">
                {t("adaptiveMotion.warning", {
                  message: t([
                    `adaptiveMotion.warnings.${snapshot.warning}`,
                    "adaptiveMotion.warnings.unknown",
                  ]),
                })}
              </p>
            )}
            {snapshot.error && (
              <p role="alert">
                {t("adaptiveMotion.error", {
                  message: t([
                    `adaptiveMotion.analysisErrors.${snapshot.error}`,
                    "adaptiveMotion.analysisErrors.unknown",
                  ]),
                })}
              </p>
            )}
            {profile ? (
              <>
                <p>
                  {t("adaptiveMotion.window", {
                    index: profile.window_index,
                    start: minuteLabel(profile.start_minute),
                    end: minuteLabel(profile.end_minute),
                    timezone: profile.timezone,
                  })}
                </p>
                <p>
                  {t("adaptiveMotion.wind", {
                    gust: profile.gust_band_mph ?? t("adaptiveMotion.unknown"),
                    sector:
                      profile.wind_direction_sector ??
                      t("adaptiveMotion.unknown"),
                    state: t([
                      `adaptiveMotion.weatherStates.${profile.weather_state}`,
                      "adaptiveMotion.weatherStates.unknown",
                    ]),
                    time: dateLabel(profile.weather_observed_at),
                  })}
                </p>
              </>
            ) : (
              <p>{t("adaptiveMotion.noProfile")}</p>
            )}
          </>
        )}
      </div>
      {grid && (
        <>
          <p className="text-sm">
            {t("adaptiveMotion.resolution", {
              width: grid.analysis_width,
              height: grid.analysis_height,
              columns: grid.columns,
              rows: grid.rows,
              displayColumns: grid.display_columns,
              displayRows: grid.display_rows,
            })}
          </p>
          <p className="text-sm text-muted-foreground">
            {t("adaptiveMotion.numericHint")}
          </p>
          <p className="text-sm" data-testid="adaptive-matrix-extrema">
            {t(
              grid.minimum !== undefined && grid.maximum !== undefined
                ? "adaptiveMotion.matrixRange"
                : "adaptiveMotion.overviewRange",
              {
                minimum: numberFormat.format(scale.min),
                maximum: numberFormat.format(scale.max),
              },
            )}
          </p>
          <div
            className="flex items-center gap-2 text-sm"
            aria-label={t("adaptiveMotion.legend")}
          >
            <span>
              {t("adaptiveMotion.overviewScale", {
                field: t(`adaptiveMotion.fields.${field}`),
              })}
            </span>
            <span>{numberFormat.format(scale.min)}</span>
            <span className="h-3 w-28 bg-gradient-to-r from-blue-500 via-transparent to-orange-500" />
            <span>{numberFormat.format(scale.max)}</span>
          </div>
        </>
      )}
      {imageError && <p role="alert">{t("adaptiveMotion.errors.image")}</p>}
      <div
        className="max-w-full overflow-auto rounded border"
        data-testid="adaptive-motion-viewport"
      >
        <div className="relative" style={{ width: `${zoom * 100}%` }}>
          {visible && imageVersion !== undefined && (
            <img
              src={
                liveStream
                  ? `${apiHost}api/${encodeURIComponent(camera)}?fps=5&height=720`
                  : `${apiHost}api/${encodeURIComponent(camera)}/latest.webp?height=720&cache=${imageVersion}`
              }
              alt={t("adaptiveMotion.image", { camera })}
              className="block h-auto w-full max-w-none"
              onLoad={() => {
                setImageReady(true);
                setImageError(false);
              }}
              onError={() => {
                setImageReady(false);
                setImageError(true);
              }}
            />
          )}
          <canvas
            ref={canvasRef}
            className="absolute inset-0 size-full cursor-crosshair"
            aria-label={t("adaptiveMotion.canvas")}
            data-testid="adaptive-motion-canvas"
            onClick={(event) => {
              if (!grid || !imageReady) return;
              const bounds = event.currentTarget.getBoundingClientRect();
              setSelection({
                column: Math.max(
                  0,
                  Math.min(
                    grid.columns - 1,
                    Math.floor(
                      ((event.clientX - bounds.left) / bounds.width) *
                        grid.columns,
                    ),
                  ),
                ),
                row: Math.max(
                  0,
                  Math.min(
                    grid.rows - 1,
                    Math.floor(
                      ((event.clientY - bounds.top) / bounds.height) *
                        grid.rows,
                    ),
                  ),
                ),
              });
            }}
          />
        </div>
      </div>
      <p className="text-sm">{t("adaptiveMotion.selectHint")}</p>
      {grid && (
        <div className="flex flex-wrap items-center gap-2">
          {(["column", "row"] as const).map((axis) => (
            <label key={axis} className="flex items-center gap-2">
              {t(`adaptiveMotion.${axis}`)}
              <input
                type="number"
                className="w-20 rounded border bg-background p-1"
                min={0}
                max={(axis === "column" ? grid.columns : grid.rows) - 1}
                value={selection?.[axis] ?? 0}
                onChange={(event) => {
                  const value = Number(event.target.value);
                  const limit = axis === "column" ? grid.columns : grid.rows;
                  if (Number.isInteger(value) && value >= 0 && value < limit) {
                    setSelection((previous) => ({
                      column: previous?.column ?? 0,
                      row: previous?.row ?? 0,
                      [axis]: value,
                    }));
                  }
                }}
              />
            </label>
          ))}
          {selection && (
            <Button size="sm" onClick={() => setSelection(undefined)}>
              {t("adaptiveMotion.clearSelection")}
            </Button>
          )}
        </div>
      )}
      {selection && (
        <div
          className="space-y-2 rounded border p-3"
          data-testid="adaptive-motion-cell"
        >
          <h4>{t("adaptiveMotion.cell", selection)}</h4>
          {selectedCell ? (
            <>
              <p className="text-sm text-muted-foreground">
                {t("adaptiveMotion.exactCellHint")}
              </p>
              <dl className="grid grid-cols-2 gap-2 text-sm">
                {cellFields.map((key) => (
                  <div key={key}>
                    <dt className="text-muted-foreground">
                      {t(`adaptiveMotion.cellFields.${key}`)}
                    </dt>
                    <dd>{numberFormat.format(selectedCell[key])}</dd>
                  </div>
                ))}
              </dl>
            </>
          ) : (
            <p>{t("adaptiveMotion.cellLoading")}</p>
          )}
        </div>
      )}
    </section>
  );
}
