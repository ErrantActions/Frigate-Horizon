import { useApiHost } from "@/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

type VectorGrid = {
  updated_at: number;
  width: number;
  height: number;
  rows: number;
  columns: number;
  activity: number[];
  coded_activity: number[];
  vector_x: number[];
  vector_y: number[];
  uncertainty: string | null;
  minimum_activity: number;
  autotune?: {
    status: "learning" | "ready";
    samples: number;
    minimum_samples: number;
    held_cells: number;
    limited_cells: number;
    vector_floor: number[];
    coded_floor: number[];
    vector_signal: number[];
    coded_signal: number[];
    pixels_used: false;
    applies_to_live_detector: false;
  } | null;
};

type Preview = {
  camera: string;
  session?: string;
  available?: boolean;
  status: string;
  server_time?: number;
  data: VectorGrid | null;
};

function validPreview(value: Preview, camera: string) {
  if (
    value.camera !== camera ||
    !["warming_up", "running", "stopped", "stale", "error"].includes(
      value.status,
    )
  )
    return false;
  const grid = value.data;
  if (!grid) return value.status !== "running";
  const tuning = grid.autotune;
  if (
    tuning &&
    (tuning.pixels_used !== false ||
      tuning.applies_to_live_detector !== false ||
      !["learning", "ready"].includes(tuning.status) ||
      ![
        tuning.samples,
        tuning.minimum_samples,
        tuning.held_cells,
        tuning.limited_cells,
      ].every((count) => Number.isInteger(count) && count >= 0) ||
      [
        tuning.vector_floor,
        tuning.coded_floor,
        tuning.vector_signal,
        tuning.coded_signal,
      ].some(
        (array) =>
          !Array.isArray(array) ||
          array.length !== 576 ||
          array.some(
            (entry) => !Number.isFinite(entry) || entry < 0 || entry > 1,
          ),
      ))
  )
    return false;
  return (
    value.status === "running" &&
    Number.isFinite(grid.updated_at) &&
    Number.isFinite(value.server_time) &&
    value.server_time! - grid.updated_at >= 0 &&
    value.server_time! - grid.updated_at < 3 &&
    grid.rows === 18 &&
    grid.columns === 32 &&
    Number.isInteger(grid.width) &&
    Number.isInteger(grid.height) &&
    grid.width > 0 &&
    grid.height > 0 &&
    Number.isFinite(grid.minimum_activity) &&
    grid.minimum_activity >= 0 &&
    grid.minimum_activity <= 1 &&
    [grid.activity, grid.coded_activity].every(
      (array) =>
        Array.isArray(array) &&
        array.every((value) => value >= 0 && value <= 1),
    ) &&
    [grid.activity, grid.coded_activity, grid.vector_x, grid.vector_y].every(
      (array) =>
        Array.isArray(array) &&
        array.length === 576 &&
        array.every(Number.isFinite),
    )
  );
}

export default function CompressedMotionInspector({
  camera,
}: {
  camera: string;
}) {
  const { t } = useTranslation(["views/settings"]);
  const stateLabels: Record<string, string> = {
    warming_up: t("compressedVectors.states.warming_up"),
    running: t("compressedVectors.states.running"),
    stopped: t("compressedVectors.states.stopped"),
    stale: t("compressedVectors.states.stale"),
    error: t("compressedVectors.states.error"),
  };
  const apiHost = useApiHost();
  const [available, setAvailable] = useState(false);
  const [preview, setPreview] = useState<Preview>();
  const [session, setSession] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState(false);
  const [minDistance, setMinDistance] = useState("0.5");
  const [maxDistance, setMaxDistance] = useState("");
  const [activity, setActivity] = useState("0");
  const [autoTune, setAutoTune] = useState(true);
  const [aboveNoise, setAboveNoise] = useState(true);
  const [heatmap, setHeatmap] = useState<"vector" | "coded">("vector");
  const canvas = useRef<HTMLCanvasElement>(null);
  const token = useRef<string | undefined>(undefined);
  const generation = useRef(0);
  const endpoint = `${apiHost}api/${encodeURIComponent(camera)}/compressed-motion-preview`;

  const release = useCallback(
    async (id: string) => {
      const response = await fetch(
        `${endpoint}?session=${encodeURIComponent(id)}`,
        {
          method: "DELETE",
          headers: { "X-CSRF-TOKEN": "1" },
          credentials: "same-origin",
          keepalive: true,
        },
      );
      if (!response.ok && response.status !== 404) {
        throw new Error("Preview stop failed");
      }
    },
    [endpoint],
  );

  useEffect(() => {
    const current = ++generation.current;
    setAvailable(false);
    setFailure(false);
    setPreview(undefined);
    setSession(undefined);
    setBusy(false);
    const request = new AbortController();
    const timeout = setTimeout(() => request.abort(), 5000);
    fetch(endpoint, {
      credentials: "same-origin",
      cache: "no-store",
      signal: request.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error("Preview availability failed");
        const value: Preview = await response.json();
        if (generation.current !== current) return;
        if (value.camera !== camera) throw new Error("Preview camera mismatch");
        setAvailable(value.available === true);
      })
      .catch(() => {
        if (generation.current === current) setFailure(true);
      })
      .finally(() => clearTimeout(timeout));
    const hidden = () => {
      if (document.hidden && token.current) {
        const id = token.current;
        token.current = undefined;
        setSession(undefined);
        setPreview(undefined);
        void release(id).catch(() =>
          toast.error(t("compressedVectors.stopFailed")),
        );
      }
    };
    document.addEventListener("visibilitychange", hidden);
    return () => {
      generation.current = current + 1;
      request.abort();
      clearTimeout(timeout);
      document.removeEventListener("visibilitychange", hidden);
      const id = token.current;
      token.current = undefined;
      if (id) {
        void release(id).catch(() =>
          toast.error(t("compressedVectors.stopFailed")),
        );
      }
    };
  }, [camera, endpoint, release, t]);

  useEffect(() => {
    if (!session) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    let request: AbortController | undefined;
    let timeout: ReturnType<typeof setTimeout>;
    const poll = async () => {
      if (disposed || document.hidden) return;
      request = new AbortController();
      timeout = setTimeout(() => request?.abort(), 5000);
      try {
        const response = await fetch(`${endpoint}?session=${session}`, {
          signal: request.signal,
          cache: "no-store",
          credentials: "same-origin",
        });
        if (!response.ok) throw new Error("Preview request failed");
        const value: Preview = await response.json();
        if (!validPreview(value, camera) || value.session !== session) {
          throw new Error("Invalid vector preview");
        }
        if (!disposed && token.current === session) {
          setPreview(value);
          setFailure(false);
          if (["error", "stopped"].includes(value.status)) {
            token.current = undefined;
            setSession(undefined);
          }
        }
      } catch {
        if (!disposed && token.current === session) {
          setFailure(true);
          setPreview(undefined);
        }
      } finally {
        clearTimeout(timeout);
        if (!disposed) timer = setTimeout(poll, 1000);
      }
    };
    void poll();
    return () => {
      disposed = true;
      clearTimeout(timer);
      clearTimeout(timeout);
      request?.abort();
    };
  }, [camera, endpoint, session]);

  useEffect(() => {
    const element = canvas.current;
    const context = element?.getContext("2d");
    if (!element || !context) return;
    context.fillStyle = "#111827";
    context.fillRect(0, 0, element.width, element.height);
    const grid = preview?.data;
    if (!grid) return;
    const tuning =
      aboveNoise && grid.autotune?.status === "ready"
        ? grid.autotune
        : undefined;
    const vectors = tuning?.vector_signal ?? grid.activity;
    const coded = tuning?.coded_signal ?? grid.coded_activity;
    const cell = element.width / grid.columns;
    for (let index = 0; index < grid.activity.length; index++) {
      const x = (index % grid.columns) * cell;
      const y = Math.floor(index / grid.columns) * cell;
      const score = heatmap === "vector" ? vectors[index] : coded[index];
      if (score > grid.minimum_activity) {
        context.fillStyle = `rgba(245,158,11,${Math.min(0.8, score)})`;
        context.fillRect(x, y, cell, cell);
      }
      context.strokeStyle = "#374151";
      context.strokeRect(x, y, cell, cell);
      const dx = grid.vector_x[index];
      const dy = grid.vector_y[index];
      const magnitude = Math.hypot(dx, dy);
      if (magnitude === 0 || vectors[index] <= grid.minimum_activity) continue;
      const length = Math.min(cell * 0.8, 4 + magnitude * 2);
      const startX = x + cell / 2;
      const startY = y + cell / 2;
      const endX = startX + (dx / magnitude) * length;
      const endY = startY + (dy / magnitude) * length;
      const angle = Math.atan2(dy, dx);
      context.strokeStyle = "#22d3ee";
      context.lineWidth = 1.5;
      context.beginPath();
      context.moveTo(startX, startY);
      context.lineTo(endX, endY);
      context.moveTo(
        endX - 4 * Math.cos(angle - 0.6),
        endY - 4 * Math.sin(angle - 0.6),
      );
      context.lineTo(endX, endY);
      context.lineTo(
        endX - 4 * Math.cos(angle + 0.6),
        endY - 4 * Math.sin(angle + 0.6),
      );
      context.stroke();
    }
  }, [preview, aboveNoise, heatmap]);

  const minimum = Number(minDistance);
  const maximum = maxDistance === "" ? null : Number(maxDistance);
  const area = Number(activity);
  const valid =
    minDistance !== "" &&
    activity !== "" &&
    Number.isFinite(minimum) &&
    minimum >= 0 &&
    minimum <= 4096 &&
    (maximum === null ||
      (Number.isFinite(maximum) && maximum > minimum && maximum <= 16384)) &&
    Number.isFinite(area) &&
    area >= 0 &&
    area <= 1;

  const start = async () => {
    const current = generation.current;
    setBusy(true);
    setFailure(false);
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-TOKEN": "1" },
        body: JSON.stringify({
          min_distance: minimum,
          max_distance: maximum,
          min_activity: area,
          auto_tune: autoTune,
        }),
      });
      if (!response.ok) throw new Error("Preview start failed");
      const value: Preview = await response.json();
      if (
        !validPreview(value, camera) ||
        !value.session ||
        !/^[a-f0-9]{32}$/.test(value.session)
      ) {
        throw new Error("Invalid preview session");
      }
      if (generation.current !== current || document.hidden) {
        await release(value.session);
        return;
      }
      token.current = value.session;
      setSession(value.session);
      setPreview(value);
    } catch {
      if (generation.current === current) setFailure(true);
    } finally {
      if (generation.current === current) setBusy(false);
    }
  };

  const stop = async () => {
    if (!session) return;
    setBusy(true);
    try {
      await release(session);
      token.current = undefined;
      setSession(undefined);
      setPreview(undefined);
      setFailure(false);
    } catch {
      setFailure(true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section
      className="space-y-3 rounded-lg border p-3"
      aria-label={t("compressedVectors.title")}
    >
      <h3 className="font-semibold">{t("compressedVectors.title")}</h3>
      <p className="text-sm text-muted-foreground">
        {t("compressedVectors.description")}
      </p>
      <div className="grid gap-3 sm:grid-cols-3">
        <div>
          <Label htmlFor="vector-min">{t("compressedVectors.minimum")}</Label>
          <Input
            id="vector-min"
            type="number"
            min={0}
            max={4096}
            step={0.5}
            value={minDistance}
            disabled={!!session || busy}
            onChange={(event) => setMinDistance(event.target.value)}
          />
        </div>
        <div>
          <Label htmlFor="vector-max">{t("compressedVectors.maximum")}</Label>
          <Input
            id="vector-max"
            type="number"
            min={0.5}
            max={16384}
            step={0.5}
            value={maxDistance}
            disabled={!!session || busy}
            onChange={(event) => setMaxDistance(event.target.value)}
          />
        </div>
        <div>
          <Label htmlFor="vector-activity">
            {t("compressedVectors.activity")}
          </Label>
          <Input
            id="vector-activity"
            type="number"
            min={0}
            max={1}
            step={0.001}
            value={activity}
            disabled={!!session || busy}
            onChange={(event) => setActivity(event.target.value)}
          />
        </div>
      </div>
      <p className="text-xs text-muted-foreground">
        {t("compressedVectors.units")}
      </p>
      <div className="flex items-center gap-2">
        <Checkbox
          id="compressed-auto-tune"
          checked={autoTune}
          disabled={!!session || busy}
          onCheckedChange={(checked) => setAutoTune(checked === true)}
        />
        <Label htmlFor="compressed-auto-tune">
          {t("compressedVectors.autoTune")}
        </Label>
      </div>
      <p className="text-xs text-muted-foreground">
        {t("compressedVectors.autoTuneHelp")}
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <Checkbox
            id="compressed-above-noise"
            checked={aboveNoise}
            onCheckedChange={(checked) => setAboveNoise(checked === true)}
          />
          <Label htmlFor="compressed-above-noise">
            {t("compressedVectors.aboveNoise")}
          </Label>
        </div>
        <Label htmlFor="compressed-heatmap">
          {t("compressedVectors.heatmap")}
        </Label>
        <select
          id="compressed-heatmap"
          className="rounded border bg-background p-1 text-sm"
          value={heatmap}
          onChange={(event) =>
            setHeatmap(event.target.value === "coded" ? "coded" : "vector")
          }
        >
          <option value="vector">
            {t("compressedVectors.vectorActivity")}
          </option>
          <option value="coded">{t("compressedVectors.codedActivity")}</option>
        </select>
      </div>
      <div className="flex gap-2">
        <Button
          disabled={!available || busy || !!session || !valid}
          onClick={() => void start()}
        >
          {t("compressedVectors.start")}
        </Button>
        <Button disabled={!session || busy} onClick={() => void stop()}>
          {t("compressedVectors.stop")}
        </Button>
      </div>
      {!available && !failure && (
        <p role="status">{t("compressedVectors.unavailable")}</p>
      )}
      {failure && <p role="alert">{t("compressedVectors.failed")}</p>}
      {preview?.status && (
        <p role="status">
          {t("compressedVectors.status", {
            state: stateLabels[preview.status] ?? stateLabels.error,
          })}
        </p>
      )}
      {preview?.data?.uncertainty && <p>{t("compressedVectors.uncertain")}</p>}
      {preview?.data?.autotune && (
        <p data-testid="compressed-noise-status" role="status">
          {t(
            preview.data.autotune.status === "ready"
              ? "compressedVectors.noiseReady"
              : "compressedVectors.noiseLearning",
            {
              samples: preview.data.autotune.samples,
              minimum: preview.data.autotune.minimum_samples,
              held: preview.data.autotune.held_cells,
            },
          )}
        </p>
      )}
      {!!preview?.data?.autotune?.limited_cells && (
        <p>
          {t("compressedVectors.noiseLimited", {
            count: preview.data.autotune.limited_cells,
          })}
        </p>
      )}
      <canvas
        ref={canvas}
        width={960}
        height={540}
        className="aspect-video w-full rounded"
        aria-label={t("compressedVectors.canvas")}
        data-testid="compressed-vector-canvas"
      />
      <p className="text-xs text-muted-foreground">
        {t("compressedVectors.legend")}
      </p>
    </section>
  );
}
