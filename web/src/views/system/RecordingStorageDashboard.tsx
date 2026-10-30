import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import * as SliderPrimitive from "@radix-ui/react-slider";
import { usePersistence } from "@/hooks/use-persistence";
import { cn } from "@/lib/utils";
import {
  RecordingStorageSettings,
  RecordingStorageTiers,
  StorageCapacity,
} from "@/types/storage";
import axios from "axios";
import { FormEvent, ReactNode, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  LuArrowRight,
  LuDatabase,
  LuHardDrive,
  LuSettings2,
} from "react-icons/lu";
import useSWR from "swr";
import isEqual from "lodash/isEqual";
import { FrigateConfig } from "@/types/frigateConfig";

export default function RecordingStorageDashboard({
  children,
  isActive = true,
}: {
  children: ReactNode;
  isActive?: boolean;
}) {
  const { t } = useTranslation("views/storage");
  const { data, error, mutate } = useSWR<RecordingStorageTiers>(
    "recordings/storage/tiers",
    { refreshInterval: isActive ? 5000 : 0, isPaused: () => !isActive },
  );
  const { data: config, mutate: refreshConfig } =
    useSWR<FrigateConfig>("config");
  const [theme, setTheme] = usePersistence<"clean" | "control-room">(
    "recording-storage-theme",
    "clean",
  );
  const [draft, setDraft] = useState<Partial<RecordingStorageSettings> | null>(
    null,
  );
  const [expected, setExpected] = useState<Partial<RecordingStorageSettings>>(
    {},
  );
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [confirmRetention, setConfirmRetention] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const settings = data ? { ...data.settings, ...draft } : undefined;
  const reducesRetention =
    settings &&
    data &&
    (settings.retain_days < data.settings.retain_days ||
      (settings.max_age_days != null &&
        settings.max_age_days < (data.settings.max_age_days ?? Infinity)) ||
      (settings.max_archive_gib != null &&
        settings.max_archive_gib <
          (data.settings.max_archive_gib ?? Infinity)) ||
      (settings.archive_full_behavior === "expire_oldest" &&
        data.settings.archive_full_behavior !== "expire_oldest"));

  function update<K extends keyof RecordingStorageSettings>(
    key: K,
    value: RecordingStorageSettings[K],
  ) {
    if (!settings) return;
    updateSettings({ [key]: value });
  }

  function updateSettings(values: Partial<RecordingStorageSettings>) {
    if (!settings || !data) return;
    const nextExpected = { ...expected };
    for (const key of Object.keys(
      values,
    ) as (keyof RecordingStorageSettings)[]) {
      if (!(key in nextExpected)) {
        Object.assign(nextExpected, { [key]: data.settings[key] ?? null });
      }
    }
    setExpected(nextExpected);
    setDraft({ ...draft, ...values });
    setMessage(null);
    setConfirmRetention(false);
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!settings || !draft || (reducesRetention && !confirmRetention)) return;
    if (
      settings.start_percent !== undefined &&
      settings.drain_percent >= settings.start_percent
    ) {
      setSaveError(t("thresholds.invalid"));
      return;
    }
    setSaving(true);
    setSaveError(null);
    setMessage(null);
    try {
      const latest = await axios.get<RecordingStorageTiers>(
        "recordings/storage/tiers",
      );
      if (
        Object.entries(expected).some(
          ([key, value]) =>
            !isEqual(
              latest.data.settings[key as keyof RecordingStorageSettings] ??
                null,
              value,
            ),
        )
      ) {
        setSaveError(t("saveConflict"));
        await mutate();
        return;
      }
      const result = await axios.put<{ success: boolean }>("config/set", {
        config_data: { storage: draft },
        expected_storage: expected,
        requires_restart: 0,
        update_topic: "config/storage",
      });
      if (!result.data.success) throw new Error(t("saveFailed"));
      setDraft(null);
      setExpected({});
      setConfirmRetention(false);
      setMessage(t("saved"));
    } catch (error) {
      setSaveError(
        axios.isAxiosError<{ code?: string }>(error) &&
          error.response?.data.code === "storage_settings_conflict"
          ? t("saveConflict")
          : axios.isAxiosError<{ message?: string }>(error)
            ? (error.response?.data.message ?? t("saveFailed"))
            : t("saveFailed"),
      );
    } finally {
      setSaving(false);
    }
    await Promise.all([mutate(), refreshConfig()]).catch(() =>
      setSaveError(t("refreshFailed")),
    );
  }

  const state = data?.health.state ?? "starting";
  const transfer = data?.health.transfer;
  const capacities = data?.health.capacity;
  const sharedFilesystem =
    capacities?.local?.filesystem_id != null &&
    capacities.local.filesystem_id === capacities.archive?.filesystem_id;
  const percentage = data
    ? ((capacities?.local?.usage_bytes != null
        ? capacities.local.usage_bytes / 2 ** 20
        : data.local.usage_mib) /
        (data.settings.local_budget_gib * 1024)) *
      100
    : 0;
  const formatGiB = (mib: number) =>
    t("gib", {
      value: (mib / 1024).toLocaleString(undefined, {
        maximumFractionDigits: 1,
      }),
    });
  const controlRoom = theme === "control-room";
  const formatBytes = (bytes: number | null | undefined) =>
    bytes == null
      ? t("capacity.unknown")
      : t("bytes", { value: bytes.toLocaleString() });
  const duration = (seconds: number) =>
    t("estimatedMinutes", { value: Math.max(1, Math.ceil(seconds / 60)) });
  const formatTransferRate = (bytesPerSecond: number) =>
    t("progress.megabytesPerSecond", {
      value: (bytesPerSecond / 1_000_000).toLocaleString(undefined, {
        maximumFractionDigits: 1,
      }),
    });
  const renderCapacity = (capacity: StorageCapacity | undefined) => (
    <div className="mt-4 border-t pt-3 text-xs text-muted-foreground">
      <div className="mb-2 flex justify-between gap-3">
        <span>{t("capacity.headroom")}</span>
        <span className="tabular-nums">
          {formatBytes(capacity?.effective_headroom_bytes)}
        </span>
      </div>
      <p className="mb-2 font-medium">
        {t("capacity.state", {
          state: t(`capacity.states.${capacity?.state ?? "unknown"}`),
        })}
      </p>
      <details>
        <summary className="cursor-pointer font-medium">
          {t("capacity.details")}
        </summary>
        <dl className="grid grid-cols-2 gap-x-3 gap-y-2">
          {(
            [
              ["physicalTotal", capacity?.filesystem_total_bytes],
              ["physicalAvailable", capacity?.filesystem_available_bytes],
              ["quotaUsed", capacity?.usage_bytes],
              ["quotaRemaining", capacity?.budget_remaining_bytes],
              ["reservations", capacity?.reserved_bytes],
              ["physicalReservations", capacity?.physical_reserved_bytes],
              ["writtenReservations", capacity?.written_reservation_bytes],
              ["reserve", capacity?.reserve_bytes],
            ] as const
          ).map(([key, value]) => (
            <div key={key} className="contents">
              <dt>{t(`capacity.${key}`)}</dt>
              <dd className="text-right tabular-nums">
                {key === "quotaRemaining" && capacity?.budget_bytes === null
                  ? t("capacity.noQuota")
                  : formatBytes(value)}
              </dd>
            </div>
          ))}
          {(capacity?.over_budget_bytes ?? 0) > 0 && (
            <>
              <dt className="text-danger">{t("capacity.overBudget")}</dt>
              <dd className="text-right text-danger">
                {formatBytes(capacity?.over_budget_bytes)}
              </dd>
            </>
          )}
        </dl>
        <p className="mt-2">{t("capacity.protectedIncluded")}</p>
        {capacity?.reservation_accounting_known === false && (
          <p className="mt-2">{t("capacity.maintenanceUnknown")}</p>
        )}
        {capacity?.sampled_at != null && (
          <p className="mt-2">
            {t("capacity.sampled", {
              time: new Date(capacity.sampled_at * 1000).toLocaleString(),
            })}
          </p>
        )}
      </details>
    </div>
  );
  const numericFields = [
    { key: "parallel_transfers", min: 1, max: 50 },
    { key: "local_budget_gib", min: 0.001, max: undefined },
    { key: "retain_days", min: 0.001, max: undefined },
    { key: "transfer_mib_per_second", min: 0, max: undefined },
    { key: "archive_reserve_gib", min: 0, max: undefined },
  ] as const;

  return (
    <div
      className={cn(
        "rounded-2xl p-3 text-foreground transition-colors md:p-5",
        controlRoom
          ? "border bg-background p-2 md:p-3 [&_.storage-status]:font-mono"
          : "bg-background",
      )}
      data-storage-theme={theme}
    >
      <header className="mb-5 flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="mb-1 text-xs font-semibold uppercase tracking-widest text-muted-foreground">
            {t("eyebrow")}
          </div>
          <h2 className="text-2xl font-semibold tracking-tight">
            {t("title")}
          </h2>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
            {t("description")}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <div
            className="flex rounded-lg border p-1"
            role="group"
            aria-label={t("theme")}
          >
            {(["clean", "control-room"] as const).map((value) => (
              <Button
                key={value}
                size="sm"
                variant={theme === value ? "select" : "ghost"}
                aria-pressed={theme === value}
                onClick={() => setTheme(value)}
              >
                {t(`themes.${value}`)}
              </Button>
            ))}
          </div>
          <Button
            variant="outline"
            onClick={() => setShowSettings(!showSettings)}
            aria-expanded={showSettings}
            aria-controls="recording-storage-settings"
          >
            <LuSettings2 className="mr-2 size-4" />
            {t("settings")}
          </Button>
        </div>
      </header>

      {error && (
        <p role="alert" className="mb-4 text-danger">
          {t("loadFailed")}
        </p>
      )}
      {!data && !error && <p role="status">{t("loading")}</p>}
      {data?.usage_status &&
        (data.usage_status.stale || data.usage_status.error) && (
          <p role="status" className="mb-4 text-sm text-muted-foreground">
            {t(data.usage_status.error ? "usageUnavailable" : "usageStale")}
          </p>
        )}
      {data && settings && (
        <>
          <div className="mb-5 grid gap-4 lg:grid-cols-[1fr_auto_1fr]">
            <section className="rounded-xl border bg-background_alt p-5">
              <div className="flex items-center justify-between gap-2">
                <h3 className="flex items-center gap-2 font-medium">
                  <LuHardDrive className="size-5 text-selected" />
                  {t("local.title")}
                </h3>
                <span className="rounded-full bg-selected/10 px-2.5 py-1 text-xs text-selected">
                  {t("local.badge")}
                </span>
              </div>
              <div className="mt-4 text-3xl font-semibold tabular-nums">
                {capacities?.local?.usage_bytes === null
                  ? t("capacity.unknown")
                  : formatGiB(
                      capacities?.local?.usage_bytes != null
                        ? capacities.local.usage_bytes / 2 ** 20
                        : data.local.usage_mib,
                    )}
              </div>
              <p className="mt-1 text-sm text-muted-foreground">
                {t("local.budget", { budget: data.settings.local_budget_gib })}
              </p>
              <div
                className="my-3 h-2 overflow-hidden rounded-full bg-secondary"
                role="progressbar"
                aria-label={t("local.usage")}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.min(100, Math.round(percentage))}
                aria-valuetext={t("local.percent", {
                  percent: Math.round(percentage),
                })}
              >
                <div
                  className={cn(
                    "h-full rounded-full transition-all",
                    percentage >= 100 ? "bg-danger" : "bg-selected",
                  )}
                  style={{
                    width: `${Math.min(100, Math.max(0, percentage))}%`,
                  }}
                />
              </div>
              <p className="text-xs text-muted-foreground">
                {t("thresholds.summary", {
                  start: data.settings.start_percent ?? 100,
                  stop: data.settings.drain_percent,
                })}
              </p>
              {renderCapacity(capacities?.local)}
            </section>
            <div
              className="storage-status flex flex-col items-center justify-center gap-2 py-2 text-sm"
              aria-live="polite"
            >
              <LuArrowRight
                className={cn(
                  "size-6",
                  state === "moving" &&
                    "text-selected motion-safe:animate-pulse",
                )}
              />
              <span
                className={cn(
                  state === "blocked" ? "text-danger" : "text-muted-foreground",
                )}
              >
                {t(`states.${state}`)}
              </span>
              {state === "ready" && transfer?.phase === "buffering" && (
                <span className="text-xs text-muted-foreground">
                  {t("progress.buffering")}
                </span>
              )}
              {data.health.backlog_mib !== undefined &&
                data.health.backlog_mib > 0 && (
                  <span className="text-xs text-muted-foreground">
                    {t("backlog", { size: formatGiB(data.health.backlog_mib) })}
                  </span>
                )}
            </div>
            <section className="rounded-xl border bg-background_alt p-5">
              <div className="flex items-center justify-between gap-2">
                <h3 className="flex items-center gap-2 font-medium">
                  <LuDatabase className="size-5 text-selected" />
                  {t("archive.title")}
                </h3>
                <span className="rounded-full border px-2.5 py-1 text-xs">
                  {t("archive.badge")}
                </span>
              </div>
              <div className="mt-4 text-3xl font-semibold tabular-nums">
                {capacities?.archive?.usage_bytes === null
                  ? t("capacity.unknown")
                  : formatGiB(
                      capacities?.archive?.usage_bytes != null
                        ? capacities.archive.usage_bytes / 2 ** 20
                        : data.archive.usage_mib,
                    )}
              </div>
              <p className="mt-1 text-sm text-muted-foreground">
                {t("archive.retention", { days: data.settings.retain_days })}
              </p>
              {data.settings.max_archive_gib != null && (
                <p className="mt-1 text-sm text-muted-foreground">
                  {t("archive.budget", {
                    budget: data.settings.max_archive_gib,
                  })}
                </p>
              )}
              {data.settings.max_age_days != null && (
                <p className="mt-1 text-sm text-muted-foreground">
                  {t("archive.maxAge", { days: data.settings.max_age_days })}
                </p>
              )}
              <p className="mt-3 break-all font-mono text-xs text-muted-foreground">
                {data.settings.archive_path}
              </p>
              {data.health.archive_free_mib !== undefined &&
                state !== "blocked" && (
                  <p className="mt-2 text-xs text-muted-foreground">
                    {t("archive.free", {
                      size: formatGiB(data.health.archive_free_mib),
                    })}
                  </p>
                )}
              {renderCapacity(capacities?.archive)}
            </section>
          </div>
          <p className="mb-4 text-xs text-muted-foreground">
            {t(
              sharedFilesystem ? "capacity.shared" : "capacity.filesystemHint",
            )}
          </p>
          {transfer && (
            <section
              className="mb-5 rounded-xl border bg-background_alt p-5"
              aria-label={t("progress.title")}
            >
              <h3 className="font-medium">{t("progress.title")}</h3>
              <p className="mt-2 text-sm" role="status">
                {t(`progress.phases.${transfer.phase}`)}
              </p>
              {transfer.active && transfer.trigger && (
                <p className="mt-2 text-xs text-muted-foreground">
                  {t(`progress.triggers.${transfer.trigger}`)}
                </p>
              )}
              <details className="mt-3">
                <summary className="cursor-pointer text-sm font-medium">
                  {t("progress.details")}
                </summary>
                {transfer.current_file && (
                  <div className="mt-3 space-y-2 text-sm">
                    <p>
                      {t("progress.current", {
                        camera:
                          data.cameras?.[transfer.current_file.camera]
                            ?.friendly_name ?? transfer.current_file.camera,
                        file: transfer.current_file.name,
                        start: new Date(
                          transfer.current_file.start_time * 1000,
                        ).toLocaleString(),
                        end: new Date(
                          transfer.current_file.end_time * 1000,
                        ).toLocaleString(),
                      })}
                    </p>
                    <p>
                      {t("progress.fileBytes", {
                        copied: formatBytes(transfer.current_file.copied_bytes),
                        verified: formatBytes(
                          transfer.current_file.readback_bytes,
                        ),
                        size: formatBytes(transfer.current_file.size_bytes),
                      })}
                    </p>
                    <progress
                      className="h-2 w-full accent-selected"
                      aria-label={t("progress.fileVerification")}
                      max={Math.max(1, transfer.current_file.size_bytes)}
                      value={transfer.current_file.readback_bytes}
                    />
                  </div>
                )}
                {transfer.active_files && transfer.active_files.length > 1 && (
                  <div className="mt-3 space-y-2 text-sm">
                    <p>
                      {t("progress.parallel", {
                        count: transfer.active_files.length,
                      })}
                    </p>
                    {transfer.active_files.slice(1).map((file) => (
                      <div
                        key={file.id ?? `${file.camera}/${file.name}`}
                        className="rounded border p-2"
                      >
                        <p>
                          {t("progress.current", {
                            camera:
                              data.cameras?.[file.camera]?.friendly_name ??
                              file.camera,
                            file: file.name,
                            start: new Date(
                              file.start_time * 1000,
                            ).toLocaleString(),
                            end: new Date(
                              file.end_time * 1000,
                            ).toLocaleString(),
                          })}
                        </p>
                        <p>{t(`progress.phases.${file.phase ?? "waiting"}`)}</p>
                        <p>
                          {t("progress.fileBytes", {
                            copied: formatBytes(file.copied_bytes),
                            verified: formatBytes(file.readback_bytes),
                            size: formatBytes(file.size_bytes),
                          })}
                        </p>
                      </div>
                    ))}
                  </div>
                )}
              </details>
              <p className="mt-3 text-sm">
                {t("progress.batch", {
                  moved: formatBytes(transfer.verified_bytes),
                  planned: formatBytes(transfer.initial_plan_bytes),
                  files: transfer.verified_files,
                  plannedFiles: transfer.initial_plan_files,
                })}
              </p>
              <progress
                className="mt-2 h-2 w-full accent-selected"
                aria-label={t("progress.initialPlan")}
                max={Math.max(1, transfer.initial_plan_files)}
                value={Math.min(
                  transfer.verified_files,
                  transfer.initial_plan_files,
                )}
              />
              <p className="mt-2 text-xs text-muted-foreground">
                {t("progress.initialPlanHint")}
              </p>
              <p className="mt-2 text-sm">
                {t("progress.live", {
                  incoming: formatBytes(transfer.incoming_bytes),
                  usage: formatBytes(transfer.local_usage_bytes),
                  target: formatBytes(transfer.drain_target_bytes),
                  remaining: formatBytes(transfer.remaining_bytes),
                })}
              </p>
              {transfer.copy_rate_bytes_per_second != null && (
                <p className="mt-2 text-sm">
                  {t(
                    transfer.aggregate_rates
                      ? "progress.aggregateCopyRate"
                      : "progress.copyRate",
                    {
                      rate: formatTransferRate(
                        transfer.copy_rate_bytes_per_second,
                      ),
                    },
                  )}
                </p>
              )}
              {transfer.verification_rate_bytes_per_second != null && (
                <p className="mt-2 text-sm">
                  {t(
                    transfer.aggregate_rates
                      ? "progress.aggregateVerificationRate"
                      : "progress.verificationRate",
                    {
                      rate: formatTransferRate(
                        transfer.verification_rate_bytes_per_second,
                      ),
                    },
                  )}
                </p>
              )}
              <p className="mt-2 text-xs text-muted-foreground">
                {transfer.rate_bytes_per_second != null
                  ? t("progress.rate", {
                      rate: formatTransferRate(transfer.rate_bytes_per_second),
                    })
                  : t("progress.measuring")}
                {transfer.eta_seconds != null &&
                  ` ${t("progress.eta", { time: duration(transfer.eta_seconds) })}`}
                {transfer.buffer_seconds != null &&
                  ` ${t("progress.buffer", { time: duration(transfer.buffer_seconds) })}`}
              </p>
              {transfer.cannot_outpace_ingest && state !== "blocked" && (
                <p className="mt-2 text-sm text-danger">
                  {t("progress.cannotOutpace")}
                </p>
              )}
            </section>
          )}
          {state === "blocked" && (
            <p
              role="alert"
              className="mb-4 rounded-lg border border-danger/40 bg-danger/10 p-3 text-sm"
            >
              {t("blocked")}
              {data.health.error && (
                <span className="mt-1 block font-mono text-xs">
                  {data.health.error}
                </span>
              )}
            </p>
          )}
          {data.health.settings_pending && (
            <p
              role="status"
              className="mb-4 rounded-lg border bg-background_alt p-3 text-sm text-muted-foreground"
            >
              {t("pending")}
            </p>
          )}
          {showSettings && (
            <form
              id="recording-storage-settings"
              onSubmit={save}
              className="mb-5 space-y-5 rounded-xl border bg-background_alt p-5"
            >
              <div className="flex items-center justify-between gap-4">
                <div>
                  <Label htmlFor="archive-enabled">{t("fields.enabled")}</Label>
                  <p className="mt-1 text-sm text-muted-foreground">
                    {t("liveHint")}
                  </p>
                </div>
                <Switch
                  id="archive-enabled"
                  checked={settings.enabled}
                  disabled={saving}
                  onCheckedChange={(value) => update("enabled", value)}
                />
              </div>
              <div className="grid gap-4 sm:grid-cols-2">
                {(["archive_path", "mount_id"] as const).map((key) => (
                  <div key={key} className="space-y-2">
                    <Label htmlFor={`storage-${key}`}>
                      {t(`fields.${key}`)}
                    </Label>
                    <Input
                      id={`storage-${key}`}
                      value={settings[key]}
                      required
                      disabled={
                        data.settings.enabled ||
                        data.archive.recordings > 0 ||
                        data.health.settings_pending ||
                        saving
                      }
                      onChange={(event) => update(key, event.target.value)}
                    />
                  </div>
                ))}
                {numericFields
                  .filter(
                    ({ key }) =>
                      key !== "parallel_transfers" ||
                      settings.parallel_transfers !== undefined,
                  )
                  .map(({ key, min, max }) => (
                    <div key={key} className="space-y-2">
                      <Label htmlFor={`storage-${key}`}>
                        {t(`fields.${key}`)}
                      </Label>
                      <Input
                        id={`storage-${key}`}
                        type="number"
                        min={min}
                        max={max}
                        step="any"
                        required
                        disabled={saving}
                        value={
                          Number.isNaN(settings[key])
                            ? ""
                            : (settings[key] ?? 0)
                        }
                        onChange={(event) =>
                          update(key, event.target.valueAsNumber)
                        }
                      />
                    </div>
                  ))}
                {settings.start_percent !== undefined ? (
                  <fieldset
                    className="space-y-4 sm:col-span-2"
                    disabled={saving}
                  >
                    <legend className="text-sm font-medium">
                      {t("thresholds.title")}
                    </legend>
                    <p className="text-sm text-muted-foreground">
                      {t("thresholds.hint")}
                    </p>
                    <div className="flex flex-wrap justify-between gap-2 text-sm">
                      <span>
                        {t("thresholds.stopAt", {
                          size: formatGiB(
                            (settings.local_budget_gib *
                              1024 *
                              settings.drain_percent) /
                              100,
                          ),
                          percent: settings.drain_percent,
                        })}
                      </span>
                      <span>
                        {t("thresholds.startAt", {
                          size: formatGiB(
                            (settings.local_budget_gib *
                              1024 *
                              settings.start_percent) /
                              100,
                          ),
                          percent: settings.start_percent,
                        })}
                      </span>
                    </div>
                    <div className="relative pb-7 pt-3">
                      <SliderPrimitive.Root
                        className="relative flex w-full touch-none select-none items-center"
                        min={0}
                        max={100}
                        step={1}
                        minStepsBetweenThumbs={1}
                        disabled={
                          saving ||
                          !Number.isFinite(settings.local_budget_gib) ||
                          settings.local_budget_gib <= 0
                        }
                        value={[settings.drain_percent, settings.start_percent]}
                        onValueChange={([stop, start]) => {
                          if (
                            stop === undefined ||
                            start === undefined ||
                            stop >= start
                          )
                            return;
                          updateSettings({
                            drain_percent: stop,
                            start_percent: start,
                            local_budget_gib: settings.local_budget_gib,
                          });
                        }}
                      >
                        <SliderPrimitive.Track className="relative h-2 w-full grow overflow-hidden rounded-full bg-secondary">
                          <SliderPrimitive.Range className="absolute h-full bg-primary" />
                        </SliderPrimitive.Track>
                        <SliderPrimitive.Thumb
                          aria-label={t("thresholds.stopLabel")}
                          aria-valuetext={formatGiB(
                            (settings.local_budget_gib *
                              1024 *
                              settings.drain_percent) /
                              100,
                          )}
                          className="block size-5 rounded-full border-2 border-primary bg-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        />
                        <SliderPrimitive.Thumb
                          aria-label={t("thresholds.startLabel")}
                          aria-valuetext={formatGiB(
                            (settings.local_budget_gib *
                              1024 *
                              settings.start_percent) /
                              100,
                          )}
                          className="block size-5 rounded-full border-2 border-primary bg-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        />
                      </SliderPrimitive.Root>
                      {!data.usage_status?.error &&
                        !data.usage_status?.stale &&
                        Number.isFinite(settings.local_budget_gib) &&
                        settings.local_budget_gib > 0 && (
                          <div
                            className="pointer-events-none absolute top-1 h-6 border-l-2 border-muted-foreground"
                            style={{
                              left: `${Math.min(100, Math.max(0, (data.local.usage_mib / (settings.local_budget_gib * 1024)) * 100))}%`,
                            }}
                            aria-hidden="true"
                          />
                        )}
                      <div className="mt-3 flex justify-between text-xs text-muted-foreground">
                        <span>{formatGiB(0)}</span>
                        <span>
                          {formatGiB(settings.local_budget_gib * 1024)}
                        </span>
                      </div>
                    </div>
                    <p className="text-sm text-muted-foreground" role="status">
                      {t("thresholds.current", {
                        size:
                          data.usage_status?.error || data.usage_status?.stale
                            ? t("capacity.unknown")
                            : formatGiB(data.local.usage_mib),
                      })}
                    </p>
                    <Button
                      type="button"
                      variant="outline"
                      disabled={saving}
                      onClick={() =>
                        updateSettings({
                          start_percent: 60,
                          drain_percent: 40,
                          local_budget_gib: settings.local_budget_gib,
                        })
                      }
                    >
                      {t("thresholds.recommended")}
                    </Button>
                  </fieldset>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    {t("thresholds.upgrade")}
                  </p>
                )}
                {(["max_age_days", "max_archive_gib"] as const).map((key) => (
                  <div key={key} className="space-y-2">
                    <Label htmlFor={`storage-${key}`}>
                      {t(`fields.${key}`)}
                    </Label>
                    <Input
                      id={`storage-${key}`}
                      type="number"
                      min={0.001}
                      step="any"
                      disabled={saving}
                      value={settings[key] ?? ""}
                      onChange={(event) =>
                        update(
                          key,
                          event.target.value === ""
                            ? null
                            : event.target.valueAsNumber,
                        )
                      }
                    />
                  </div>
                ))}
                <div className="space-y-2">
                  <Label htmlFor="storage-full-behavior">
                    {t("fields.archive_full_behavior")}
                  </Label>
                  <select
                    id="storage-full-behavior"
                    className="w-full rounded-md border bg-background p-2"
                    disabled={saving}
                    value={settings.archive_full_behavior ?? "pause"}
                    onChange={(event) =>
                      update(
                        "archive_full_behavior",
                        event.target.value as "pause" | "expire_oldest",
                      )
                    }
                  >
                    <option value="pause">{t("fullBehavior.pause")}</option>
                    <option value="expire_oldest">
                      {t("fullBehavior.expire_oldest")}
                    </option>
                  </select>
                </div>
              </div>
              <p className="text-sm text-muted-foreground">{t("limitsHint")}</p>
              <p className="text-xs text-muted-foreground">{t("mountHint")}</p>
              <div className="flex items-center justify-between gap-4">
                <div>
                  <Label htmlFor="archive-emergency">
                    {t("fields.emergency_delete")}
                  </Label>
                  <p className="mt-1 text-sm text-muted-foreground">
                    {t("emergencyHint")}
                  </p>
                </div>
                <Switch
                  id="archive-emergency"
                  checked={settings.emergency_delete}
                  disabled={saving}
                  onCheckedChange={(value) => update("emergency_delete", value)}
                />
              </div>
              <p className="text-sm text-muted-foreground">
                {t("retentionHint")}
              </p>
              {reducesRetention && (
                <label className="flex items-start gap-3 rounded-lg border border-danger/40 p-3 text-sm">
                  <input
                    type="checkbox"
                    className="mt-1"
                    checked={confirmRetention}
                    onChange={(event) =>
                      setConfirmRetention(event.target.checked)
                    }
                  />
                  {t("confirmRetention")}
                </label>
              )}
              {saveError && (
                <p role="alert" className="text-danger">
                  {saveError}
                </p>
              )}
              {message && (
                <p role="status" className="text-selected">
                  {message}
                </p>
              )}
              <div className="flex gap-2">
                <Button
                  type="submit"
                  variant="select"
                  disabled={
                    saving ||
                    !draft ||
                    (!!reducesRetention && !confirmRetention)
                  }
                >
                  {saving ? t("saving") : t("save")}
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  disabled={saving || !draft}
                  onClick={() => {
                    setDraft(null);
                    setExpected({});
                    setSaveError(null);
                    setConfirmRetention(false);
                  }}
                >
                  {t("cancel")}
                </Button>
              </div>
              <details className="text-sm text-muted-foreground">
                <summary className="cursor-pointer font-medium">
                  {t("restart.title")}
                </summary>
                <p className="mt-2">{t("restart.live")}</p>
                <p className="mt-2">{t("restart.process")}</p>
                <p className="mt-2">{t("restart.mount")}</p>
              </details>
            </form>
          )}
        </>
      )}
      {children}
      {config && (
        <details className="mt-5 rounded-xl border p-4 text-sm">
          <summary className="cursor-pointer font-medium">
            {t("retention.title")}
          </summary>
          <p className="mt-2 text-muted-foreground">
            {t("retention.explanation")}
          </p>
          <p className="mt-2">
            {t("retention.global", {
              continuous:
                config.record.continuous?.days ?? t("capacity.unknown"),
              motion: config.record.motion?.days ?? t("capacity.unknown"),
              archive: data?.settings.retain_days ?? t("capacity.unknown"),
            })}
          </p>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-left text-xs">
              <caption className="sr-only">{t("retention.title")}</caption>
              <thead>
                <tr>
                  {(
                    [
                      "camera",
                      "continuous",
                      "motion",
                      "alerts",
                      "detections",
                      "minimum",
                    ] as const
                  ).map((key) => (
                    <th key={key} className="p-2">
                      {t(`retention.${key}`)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {Object.entries(config.cameras).map(([name, camera]) => {
                  const record = camera.record;
                  const archive =
                    data &&
                    (data.settings.enabled || data.archive.recordings > 0)
                      ? data.settings.retain_days
                      : 0;
                  const continuous = record.continuous?.days;
                  const minimum =
                    continuous != null && continuous > 0
                      ? Math.max(continuous, archive)
                      : null;
                  return (
                    <tr key={name} className="border-t">
                      <th className="p-2 font-medium">
                        {camera.friendly_name || name}
                        {(!camera.enabled || !record.enabled) && (
                          <span className="block font-normal text-muted-foreground">
                            {t("retention.disabled")}
                          </span>
                        )}
                      </th>
                      {[
                        continuous,
                        record.motion?.days,
                        record.alerts.retain.days,
                        record.detections.retain.days,
                        continuous === undefined ? undefined : minimum,
                      ].map((days, index) => (
                        <td key={index} className="p-2 tabular-nums">
                          {days === undefined
                            ? t("capacity.unknown")
                            : days === null
                              ? t("retention.eventOnly")
                              : t("retention.days", { count: days })}
                        </td>
                      ))}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {data?.settings.max_age_days != null && (
            <p className="mt-2 text-muted-foreground">
              {t("retention.maximum", { days: data.settings.max_age_days })}
            </p>
          )}
        </details>
      )}
    </div>
  );
}
