import { useEffect, useState } from "react";
import axios from "axios";
import useSWR from "swr";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import Heading from "@/components/ui/heading";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { Progress } from "@/components/ui/progress";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import ActivityIndicator from "@/components/indicators/activity-indicator";
import type {
  AudioRepairItems,
  AudioRepairJob,
  AudioRepairJobs,
} from "@/types/audioRepair";
import type { FrigateConfig } from "@/types/frigateConfig";
import { formatUnixTimestampToDateTime } from "@/utils/dateUtil";

const BASE = "recordings/audio-repair/jobs";
const PAGE_SIZE = 50;
const ACTIVE = new Set([
  "scanning",
  "running",
  "pause_requested",
  "paused",
  "stop_requested",
]);

export default function AudioRepairSettingsView() {
  const { t, i18n } = useTranslation("views/audioRepair");
  const { data: config } = useSWR<FrigateConfig>("config");
  const [camera, setCamera] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [limit, setLimit] = useState("1000");
  const [selectedJobId, setSelectedJobId] = useState<string>();
  const [offset, setOffset] = useState(0);
  const [state, setState] = useState("candidate");
  const [selected, setSelected] = useState<string[]>([]);
  const [pending, setPending] = useState<string>();
  const [confirm, setConfirm] = useState<"selected" | "all">();
  const [actionFailed, setActionFailed] = useState(false);

  const {
    data: jobs,
    error: jobsError,
    mutate: refreshJobs,
  } = useSWR<AudioRepairJobs>(BASE, {
    refreshInterval: 5000,
    refreshWhenHidden: true,
  });
  const jobId = selectedJobId ?? jobs?.jobs[0]?.id;
  const {
    data: job,
    error: jobError,
    mutate: refreshJob,
  } = useSWR<AudioRepairJob>(jobId ? `${BASE}/${jobId}` : null, {
    refreshInterval: (value) =>
      value && ACTIVE.has(value.status) ? 1000 : 10000,
    refreshWhenHidden: true,
    keepPreviousData: false,
  });
  const {
    data: page,
    error: itemsError,
    mutate: refreshItems,
  } = useSWR<AudioRepairItems>(
    jobId
      ? `${BASE}/${jobId}/items?limit=${PAGE_SIZE}&offset=${offset}${state ? `&state=${state}` : ""}`
      : null,
    {
      refreshInterval: job && ACTIVE.has(job.status) ? 2000 : 10000,
      refreshWhenHidden: true,
      keepPreviousData: false,
    },
  );

  useEffect(() => {
    document.title = t("documentTitle");
  }, [t]);

  useEffect(() => {
    setSelected([]);
    setOffset(0);
  }, [jobId]);

  const formatTime = (value: number) =>
    formatUnixTimestampToDateTime(value, {
      timezone: config?.ui.timezone ?? undefined,
      time_format: config?.ui.time_format,
      locale: i18n.language,
    });
  const busy = !!pending;
  const occupied = jobs?.jobs.some((value) => ACTIVE.has(value.status));
  const mayStart =
    !!job &&
    (job.status === "ready" ||
      (job.phase === "repair" &&
        ["completed", "stopped"].includes(job.status))) &&
    job.eligible > 0;
  const candidates =
    page?.items.filter((item) => item.state === "candidate") ?? [];
  const allPageSelected =
    candidates.length > 0 &&
    candidates.every((item) => selected.includes(item.id));
  const progress =
    job?.phase === "repair"
      ? job.selected > 0
        ? Math.min(100, (job.processed / job.selected) * 100)
        : 0
      : job && job.total > 0
        ? Math.min(100, (job.scanned / job.total) * 100)
        : 0;

  const refresh = async () => {
    await Promise.all([refreshJobs(), refreshJob(), refreshItems()]);
  };

  const scan = async () => {
    const maximum = Number(limit);
    const after = start ? Date.parse(start) / 1000 : undefined;
    const before = end ? Date.parse(end) / 1000 : undefined;
    if (
      !Number.isInteger(maximum) ||
      maximum < 1 ||
      maximum > 10000 ||
      (after !== undefined && !Number.isFinite(after)) ||
      (before !== undefined && !Number.isFinite(before)) ||
      (after !== undefined && before !== undefined && after >= before)
    ) {
      toast.error(t("invalidFilters"));
      return;
    }
    setPending("scan");
    setActionFailed(false);
    try {
      const response = await axios.post<AudioRepairJob>(BASE, {
        cameras: camera ? [camera] : undefined,
        start: after,
        end: before,
        limit: maximum,
      });
      setSelectedJobId(response.data.id);
      setState("candidate");
      await refreshJobs();
    } catch (error) {
      if (!axios.isAxiosError(error)) throw error;
      setActionFailed(true);
      toast.error(t("requestFailed"));
    } finally {
      setPending(undefined);
    }
  };

  const selectItems = (ids: string[], checked: boolean) => {
    const next = checked
      ? [...new Set([...selected, ...ids])]
      : selected.filter((id) => !ids.includes(id));
    if (next.length > 500) {
      toast.error(t("selectionLimit"));
      return;
    }
    setSelected(next);
  };

  const control = async (
    action: "start" | "pause" | "resume" | "stop",
    all = false,
  ) => {
    if (!jobId) return;
    setPending(action);
    setActionFailed(false);
    try {
      await axios.post(
        `${BASE}/${jobId}/${action}`,
        action === "start"
          ? all
            ? { all_eligible: true }
            : { item_ids: selected }
          : undefined,
      );
      if (action === "start") setSelected([]);
      await refresh();
    } catch (error) {
      if (!axios.isAxiosError(error)) throw error;
      setActionFailed(true);
      toast.error(t("requestFailed"));
    } finally {
      setPending(undefined);
    }
  };

  return (
    <div className="scrollbar-container h-full overflow-y-auto px-2 pb-6 md:pr-2">
      <div className="max-w-6xl space-y-5">
        <div>
          <Heading as="h4">{t("title")}</Heading>
          <p className="mt-2 text-sm text-muted-foreground">
            {t("description")}
          </p>
          <p className="mt-2 text-sm text-muted-foreground">{t("safety")}</p>
        </div>

        <div className="grid gap-3 rounded-lg border p-4 sm:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor="audio-repair-camera">{t("filters.camera")}</Label>
            <select
              id="audio-repair-camera"
              className="h-10 w-full rounded-md border bg-background px-3 text-sm"
              value={camera}
              onChange={(event) => setCamera(event.target.value)}
              disabled={busy}
            >
              <option value="">{t("filters.allCameras")}</option>
              {Object.values(config?.cameras ?? {}).map((value) => (
                <option key={value.name} value={value.name}>
                  {value.friendly_name || value.name}
                </option>
              ))}
            </select>
          </div>
          <div className="space-y-2">
            <Label htmlFor="audio-repair-limit">{t("filters.limit")}</Label>
            <Input
              id="audio-repair-limit"
              type="number"
              min={1}
              max={10000}
              value={limit}
              onChange={(event) => setLimit(event.target.value)}
              disabled={busy}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="audio-repair-start">{t("filters.start")}</Label>
            <Input
              id="audio-repair-start"
              type="datetime-local"
              value={start}
              onChange={(event) => setStart(event.target.value)}
              disabled={busy}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="audio-repair-end">{t("filters.end")}</Label>
            <Input
              id="audio-repair-end"
              type="datetime-local"
              value={end}
              onChange={(event) => setEnd(event.target.value)}
              disabled={busy}
            />
          </div>
          <div className="sm:col-span-2">
            <Button
              onClick={() => void scan()}
              disabled={busy || occupied || !jobs || !!jobsError}
            >
              {pending === "scan" && (
                <ActivityIndicator className="mr-2 size-4" />
              )}
              {t("actions.scan")}
            </Button>
            <p className="mt-2 text-xs text-muted-foreground">
              {t("scanOnly")}
            </p>
          </div>
        </div>

        {(jobsError || jobError || itemsError || actionFailed) && (
          <div role="alert" className="space-y-2 rounded-lg border p-3 text-sm">
            <p>{t("requestFailed")}</p>
            <Button variant="secondary" onClick={() => void refresh()}>
              {t("actions.retry")}
            </Button>
          </div>
        )}

        {!jobs && !jobsError && <ActivityIndicator />}
        {jobs && jobs.jobs.length === 0 && <p>{t("noJobs")}</p>}
        {jobId && (
          <>
            <div className="space-y-2">
              <Label htmlFor="audio-repair-job">{t("job")}</Label>
              <select
                id="audio-repair-job"
                className="h-10 w-full rounded-md border bg-background px-3 text-sm"
                value={jobId}
                onChange={(event) => setSelectedJobId(event.target.value)}
                disabled={busy}
              >
                {jobs?.jobs.map((value) => (
                  <option key={value.id} value={value.id}>
                    {formatTime(value.created_at)} ·{" "}
                    {t(`status.${value.status}`)}
                  </option>
                ))}
              </select>
            </div>
            {!job && !jobError && <ActivityIndicator />}
            {job && (
              <section className="space-y-3 rounded-lg border p-4">
                <p role="status">{t(`status.${job.status}`)}</p>
                <Progress value={progress} aria-label={t("progress")} />
                <div className="grid grid-cols-2 gap-2 text-sm sm:grid-cols-3">
                  <span>
                    {t("counts.scanned", {
                      count: job.scanned,
                      total: job.total,
                    })}
                  </span>
                  <span>{t("counts.eligible", { count: job.eligible })}</span>
                  <span>{t("counts.selected", { count: job.selected })}</span>
                  <span>{t("counts.repaired", { count: job.repaired })}</span>
                  <span>{t("counts.skipped", { count: job.skipped })}</span>
                  <span>{t("counts.failed", { count: job.failed })}</span>
                </div>
                {job.current_item && (
                  <p className="text-sm">
                    {t("current", {
                      camera:
                        config?.cameras[job.current_item.camera]
                          ?.friendly_name || job.current_item.camera,
                      time: formatTime(job.current_item.start_time),
                    })}
                  </p>
                )}
                {job.error && (
                  <p role="alert">{t("jobFailed", { reason: job.error })}</p>
                )}
                <div className="flex flex-wrap gap-2">
                  <Button
                    disabled={busy || !mayStart || selected.length === 0}
                    onClick={() => setConfirm("selected")}
                  >
                    {t("actions.startSelected", { count: selected.length })}
                  </Button>
                  <Button
                    variant="secondary"
                    disabled={busy || !mayStart}
                    onClick={() => setConfirm("all")}
                  >
                    {t("actions.startAll")}
                  </Button>
                  {["scanning", "running"].includes(job.status) && (
                    <Button
                      variant="secondary"
                      disabled={busy}
                      onClick={() => void control("pause")}
                    >
                      {t("actions.pause")}
                    </Button>
                  )}
                  {job.status === "paused" && (
                    <Button
                      disabled={busy}
                      onClick={() => void control("resume")}
                    >
                      {t("actions.resume")}
                    </Button>
                  )}
                  {ACTIVE.has(job.status) &&
                    job.status !== "stop_requested" && (
                      <Button
                        variant="destructive"
                        disabled={busy}
                        onClick={() => void control("stop")}
                      >
                        {t("actions.stop")}
                      </Button>
                    )}
                </div>
                <p className="text-xs text-muted-foreground">
                  {t("controlHelp")}
                </p>
              </section>
            )}

            <div className="flex flex-wrap items-center gap-3">
              <Label htmlFor="audio-repair-state">{t("list.filter")}</Label>
              <select
                id="audio-repair-state"
                className="h-10 rounded-md border bg-background px-3 text-sm"
                value={state}
                onChange={(event) => {
                  setState(event.target.value);
                  setOffset(0);
                }}
              >
                <option value="">{t("list.all")}</option>
                {[
                  "candidate",
                  "skipped",
                  "selected",
                  "repairing",
                  "repaired",
                  "failed",
                  "stopped",
                ].map((value) => (
                  <option key={value} value={value}>
                    {t(`itemState.${value}`)}
                  </option>
                ))}
              </select>
            </div>
            {!page && !itemsError && <ActivityIndicator />}
            {page && (
              <>
                <div className="overflow-x-auto rounded-lg border">
                  <table className="w-full text-left text-sm">
                    <thead className="bg-secondary">
                      <tr>
                        <th className="p-3">
                          <Checkbox
                            aria-label={t("list.selectPage")}
                            disabled={
                              !mayStart || candidates.length === 0 || busy
                            }
                            checked={allPageSelected}
                            onCheckedChange={(checked) =>
                              selectItems(
                                candidates.map((item) => item.id),
                                checked === true,
                              )
                            }
                          />
                        </th>
                        <th className="p-3">{t("list.camera")}</th>
                        <th className="p-3">{t("list.time")}</th>
                        <th className="p-3">{t("list.video")}</th>
                        <th className="p-3">{t("list.audio")}</th>
                        <th className="p-3">{t("list.state")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {page.items.map((item) => (
                        <tr key={item.id} className="border-t align-top">
                          <td className="p-3">
                            <Checkbox
                              aria-label={t("list.selectItem", {
                                camera: item.camera,
                                time: formatTime(item.start_time),
                              })}
                              disabled={
                                !mayStart || item.state !== "candidate" || busy
                              }
                              checked={selected.includes(item.id)}
                              onCheckedChange={(checked) =>
                                selectItems([item.id], checked === true)
                              }
                            />
                          </td>
                          <td className="p-3">
                            {config?.cameras[item.camera]?.friendly_name ||
                              item.camera}
                          </td>
                          <td className="whitespace-nowrap p-3">
                            {formatTime(item.start_time)}
                          </td>
                          <td className="p-3">
                            {item.video_codec || t("unknown")}
                          </td>
                          <td className="p-3">
                            {item.has_audio === false
                              ? t("noAudio")
                              : t("audioDescription", {
                                  codec: item.audio_codec || t("unknown"),
                                  rate: item.audio_rate ?? t("unknown"),
                                })}
                          </td>
                          <td className="p-3">
                            <p>{t(`itemState.${item.state}`)}</p>
                            {item.reason && (
                              <p className="text-xs text-muted-foreground">
                                {t(`reason.${item.reason}`, {
                                  defaultValue: t("otherReason", {
                                    reason: item.reason,
                                  }),
                                })}
                              </p>
                            )}
                          </td>
                        </tr>
                      ))}
                      {page.items.length === 0 && (
                        <tr>
                          <td colSpan={6} className="p-4">
                            {t("list.empty")}
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
                <div className="flex items-center justify-between gap-2">
                  <Button
                    variant="secondary"
                    disabled={offset === 0}
                    onClick={() =>
                      setOffset((value) => Math.max(0, value - PAGE_SIZE))
                    }
                  >
                    {t("list.previous")}
                  </Button>
                  <span className="text-sm">
                    {t("list.range", {
                      start: page.total === 0 ? 0 : offset + 1,
                      end: Math.min(offset + PAGE_SIZE, page.total),
                      total: page.total,
                    })}
                  </span>
                  <Button
                    variant="secondary"
                    disabled={offset + PAGE_SIZE >= page.total}
                    onClick={() => setOffset((value) => value + PAGE_SIZE)}
                  >
                    {t("list.next")}
                  </Button>
                </div>
              </>
            )}
          </>
        )}
      </div>
      <AlertDialog
        open={!!confirm}
        onOpenChange={(open) => !open && setConfirm(undefined)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("confirm.title")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("confirm.description")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("actions.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                const all = confirm === "all";
                setConfirm(undefined);
                void control("start", all);
              }}
            >
              {t("confirm.start")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
