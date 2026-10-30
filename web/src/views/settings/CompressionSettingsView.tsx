import { useEffect, useState } from "react";
import axios from "axios";
import useSWR from "swr";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import Heading from "@/components/ui/heading";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
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
import {
  DEFAULT_COMPRESSION_POLICY,
  COMPRESSION_ENCODERS,
  isCompressionEncoder,
  type CompressionItems,
  type CompressionJobs,
  type CompressionPolicy,
} from "@/types/compression";

const BASE = "recordings/compression/jobs";
const ACTIVE = new Set([
  "running",
  "pause_requested",
  "paused",
  "stop_requested",
]);

export default function CompressionSettingsView() {
  const { t } = useTranslation("views/compression");
  const { data: config, mutate: refreshConfig } = useSWR<{
    storage?: { compression?: CompressionPolicy };
  }>("config");
  const { data, error, mutate } = useSWR<CompressionJobs>(BASE, {
    refreshInterval: 2000,
    refreshWhenHidden: true,
  });
  const [policy, setPolicy] = useState(DEFAULT_COMPRESSION_POLICY);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [selected, setSelected] = useState<string>();
  const [offset, setOffset] = useState(0);
  const job = data?.jobs.find((item) => item.id === selected) ?? data?.jobs[0];
  const { data: audit, mutate: refreshAudit } = useSWR<CompressionItems>(
    job ? `${BASE}/${job.id}/items?limit=50&offset=${offset}` : null,
    { refreshInterval: 2000 },
  );
  useEffect(() => {
    if (config)
      setPolicy(config.storage?.compression ?? DEFAULT_COMPRESSION_POLICY);
  }, [config]);
  useEffect(() => {
    document.title = t("documentTitle");
  }, [t]);

  const action = async (control?: "pause" | "resume" | "stop") => {
    setBusy(true);
    try {
      if (control && job) {
        await axios.post(`${BASE}/${job.id}/control`, { action: control });
      } else {
        const response = await axios.post<{ id: string }>(BASE, {
          confirm: true,
        });
        setSelected(response.data.id);
        setOffset(0);
      }
      await Promise.all([mutate(), refreshAudit()]);
    } catch {
      toast.error(t("actionFailed"));
    } finally {
      setBusy(false);
    }
  };
  const save = async () => {
    setBusy(true);
    try {
      await axios.put("config/set", {
        requires_restart: 0,
        config_data: { storage: { compression: policy } },
      });
      await Promise.all([refreshConfig(), mutate()]);
      toast.success(t("saved"));
    } catch {
      toast.error(t("saveFailed"));
    } finally {
      setBusy(false);
    }
  };
  const numberInput = (
    key:
      | "min_age_days"
      | "target_mbps"
      | "max_files"
      | "max_batch_minutes"
      | "min_free_gib"
      | "cpu_threads"
      | "svt_preset",
    min: number,
    max: number,
    step = 1,
  ) => (
    <div className="space-y-2">
      <Label htmlFor={`compression-${key}`}>{t(`fields.${key}`)}</Label>
      <Input
        id={`compression-${key}`}
        type="number"
        min={min}
        max={max}
        step={step}
        required
        value={policy[key]}
        onChange={(event) =>
          setPolicy({ ...policy, [key]: Number(event.target.value) })
        }
      />
    </div>
  );
  return (
    <div className="flex max-w-4xl flex-col gap-5 p-2">
      <Heading as="h2">{t("title")}</Heading>
      <p className="text-sm text-muted-foreground">{t("description")}</p>
      <p className="rounded-lg border p-3 text-sm">{t("safety")}</p>
      {error && <p role="alert">{t("unavailable")}</p>}
      {data && !data.readers_leased && <p role="alert">{t("readerGate")}</p>}
      {data?.recovery_required && <p role="alert">{t("recoveryGate")}</p>}
      {data && !data.reservations_ready && (
        <p role="alert">{t("reservationGate")}</p>
      )}
      {data && !data.archive_supported && <p>{t("localOnly")}</p>}
      {data?.archive_supported && <p>{t("archiveHelp")}</p>}
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <div className="flex items-center gap-2">
          <Checkbox
            id="compression-enabled"
            checked={policy.enabled}
            onCheckedChange={(value) =>
              setPolicy({ ...policy, enabled: value === true })
            }
          />
          <Label htmlFor="compression-enabled">{t("fields.enabled")}</Label>
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor="compression-encoder">{t("fields.encoder")}</Label>
            <select
              id="compression-encoder"
              className="w-full rounded border bg-background p-2"
              value={policy.encoder}
              onChange={(event) => {
                if (!isCompressionEncoder(event.target.value)) {
                  toast.error(t("invalidEncoder"));
                  return;
                }
                setPolicy({ ...policy, encoder: event.target.value });
              }}
            >
              {COMPRESSION_ENCODERS.map((encoder) => (
                <option key={encoder} value={encoder}>
                  {t(`encoders.${encoder}`)}
                </option>
              ))}
            </select>
          </div>
          {numberInput("min_age_days", 1, 3650, 0.1)}
          {numberInput("target_mbps", 0.1, 100, 0.1)}
          {policy.encoder.startsWith("lib") &&
            numberInput("cpu_threads", 1, 128)}
          {policy.encoder === "libsvtav1" && numberInput("svt_preset", 0, 13)}
          <div className="space-y-2">
            <Label htmlFor="compression-frame-rate">
              {t("fields.frame_rate")}
            </Label>
            <Input
              id="compression-frame-rate"
              type="number"
              min={1}
              max={60}
              step={1}
              value={policy.frame_rate ?? ""}
              onChange={(event) =>
                setPolicy({
                  ...policy,
                  frame_rate:
                    event.target.value === ""
                      ? null
                      : Number(event.target.value),
                })
              }
            />
          </div>
          {numberInput("max_files", 1, 1000)}
          {numberInput("max_batch_minutes", 1, 1440)}
          {numberInput("min_free_gib", 0.25, 1024, 0.25)}
        </div>
        <p className="text-sm text-muted-foreground">{t("encoderHelp")}</p>
        <p className="text-sm text-muted-foreground">{t("frameRateHelp")}</p>
        <div className="flex items-center gap-2">
          <Checkbox
            id="compression-weekly"
            checked={policy.window.enabled}
            onCheckedChange={(value) =>
              setPolicy({
                ...policy,
                window: { ...policy.window, enabled: value === true },
              })
            }
          />
          <Label htmlFor="compression-weekly">{t("fields.weekly")}</Label>
        </div>
        <p className="text-sm text-muted-foreground">{t("weeklyHelp")}</p>
        {policy.window.enabled && (
          <div className="grid gap-4 sm:grid-cols-3">
            {(["weekday", "start_hour", "duration_minutes"] as const).map(
              (key) => (
                <div key={key} className="space-y-2">
                  <Label htmlFor={`compression-${key}`}>
                    {t(`fields.${key}`)}
                  </Label>
                  <Input
                    id={`compression-${key}`}
                    type="number"
                    min={key === "duration_minutes" ? 1 : 0}
                    max={
                      key === "weekday" ? 6 : key === "start_hour" ? 23 : 1440
                    }
                    required
                    value={policy.window[key]}
                    onChange={(event) =>
                      setPolicy({
                        ...policy,
                        window: {
                          ...policy.window,
                          [key]: Number(event.target.value),
                        },
                      })
                    }
                  />
                </div>
              ),
            )}
          </div>
        )}
        <Button type="submit" disabled={busy || !config}>
          {t("save")}
        </Button>
      </form>
      <Button
        className="self-start"
        disabled={
          busy ||
          !data?.enabled ||
          !data.readers_leased ||
          !data.reservations_ready ||
          data.recovery_required ||
          data.jobs.some((item) => ACTIVE.has(item.status))
        }
        onClick={() => setConfirm(true)}
      >
        {t("start")}
      </Button>
      {job && (
        <section className="space-y-3" aria-label={t("progress")}>
          <Label htmlFor="compression-job">{t("history")}</Label>
          <select
            id="compression-job"
            className="w-full rounded border bg-background p-2"
            value={job.id}
            onChange={(event) => {
              setSelected(event.target.value);
              setOffset(0);
            }}
          >
            {data?.jobs.map((item) => (
              <option key={item.id} value={item.id}>
                {new Date(item.created_at * 1000).toLocaleString()} (
                {t(`status.${item.status}`)})
              </option>
            ))}
          </select>
          <p role="status">{t(`status.${job.status}`)}</p>
          <Progress
            aria-label={t("progress")}
            value={Math.min(100, (job.processed / job.max_files) * 100)}
          />
          <p>{t("counts", job)}</p>
          <p className="text-sm text-muted-foreground">{t("savingsHelp")}</p>
          <p>
            {t("jobPolicy", {
              bitrate: job.target_mbps,
              days: job.min_age_days,
              encoder: t(`encoders.${job.encoder}`),
            })}
          </p>
          {job.reason && (
            <p>
              {t(`reasons.${job.reason}`, { defaultValue: t("unknownReason") })}
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <Button
              disabled={busy || job.status !== "running"}
              onClick={() => void action("pause")}
            >
              {t("pause")}
            </Button>
            <Button
              disabled={busy || job.status !== "paused"}
              onClick={() => void action("resume")}
            >
              {t("resume")}
            </Button>
            <Button
              variant="destructive"
              disabled={busy || !ACTIVE.has(job.status)}
              onClick={() => void action("stop")}
            >
              {t("stop")}
            </Button>
          </div>
          {audit?.items.map((item) => (
            <div key={item.id} className="rounded border p-2 text-sm">
              <p>
                {item.camera}:{" "}
                {new Date(item.start_time * 1000).toLocaleString()}
              </p>
              <p>
                {t(`itemState.${item.state}`, {
                  defaultValue: t("unknownReason"),
                })}
              </p>
              {item.reason && (
                <p>
                  {t(`reasons.${item.reason}`, {
                    defaultValue: t("unknownReason"),
                  })}
                </p>
              )}
            </div>
          ))}
          <div className="flex gap-2">
            <Button
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - 50))}
            >
              {t("previous")}
            </Button>
            <Button
              disabled={!audit || offset + 50 >= audit.total}
              onClick={() => setOffset(offset + 50)}
            >
              {t("next")}
            </Button>
          </div>
        </section>
      )}
      <AlertDialog open={confirm} onOpenChange={setConfirm}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("confirmTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("confirmDescription")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("cancel")}</AlertDialogCancel>
            <AlertDialogAction onClick={() => void action()}>
              {t("confirm")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
