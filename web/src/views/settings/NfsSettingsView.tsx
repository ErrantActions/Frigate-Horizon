import { useState } from "react";
import axios from "axios";
import copy from "copy-to-clipboard";
import useSWR from "swr";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import Heading from "@/components/ui/heading";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { NfsPlan, NfsProbe, NfsShare, NfsStatus } from "@/types/nfs";
import type { RecordingStorageTiers } from "@/types/storage";

const BASE = "storage/nfs";
const newShare = (role: NfsShare["role"]): NfsShare => ({
  role,
  server: "",
  export: "",
  version: "4.1",
  connections: 8,
});

export default function NfsSettingsView() {
  const { t } = useTranslation("views/nfs");
  const { data: status, error, mutate } = useSWR<NfsStatus>(BASE);
  const { data: tiers, mutate: refreshTiers } = useSWR<RecordingStorageTiers>(
    "recordings/storage/tiers",
    { refreshInterval: 10000 },
  );
  const [shares, setShares] = useState<NfsShare[]>([newShare("archive")]);
  const [probes, setProbes] = useState<Record<number, NfsProbe>>({});
  const [plan, setPlan] = useState<NfsPlan>();
  const [directConfirmed, setDirectConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [adaptive, setAdaptive] = useState<boolean>();
  const [maximum, setMaximum] = useState<number>();
  const direct = shares.some((share) => share.role === "recordings");
  const adaptiveValue = adaptive ?? tiers?.settings.adaptive_transfers ?? false;
  const maximumValue = maximum ?? tiers?.settings.parallel_transfers ?? 8;
  const transfer = tiers?.health.transfer?.adaptive_transfers;
  const budgetExceedsFilesystem =
    tiers?.health.capacity?.local?.filesystem_total_bytes != null &&
    (status?.local_budget_gib ?? 0) * 2 ** 30 >
      tiers.health.capacity.local.filesystem_total_bytes;

  const updateShare = (index: number, patch: Partial<NfsShare>) => {
    setShares((current) =>
      current.map((share, offset) =>
        offset === index ? { ...share, ...patch } : share,
      ),
    );
    setProbes({});
    setPlan(undefined);
    setDirectConfirmed(false);
  };
  const testServer = async (index: number) => {
    setBusy(true);
    try {
      const { data } = await axios.post<NfsProbe>(
        `${BASE}/probe`,
        { server: shares[index].server },
        { timeout: 6000 },
      );
      setProbes((current) => ({ ...current, [index]: data }));
    } catch {
      toast.error(t("probe.requestFailed"));
    } finally {
      setBusy(false);
    }
  };
  const generate = async () => {
    setBusy(true);
    setPlan(undefined);
    try {
      const { data } = await axios.post<NfsPlan>(`${BASE}/plan`, {
        shares,
        confirm_direct_recordings: directConfirmed,
      });
      setPlan(data);
    } catch {
      toast.error(t("planFailed"));
    } finally {
      setBusy(false);
    }
  };
  const saveAdaptive = async () => {
    if (!tiers) return;
    setBusy(true);
    try {
      const result = await axios.put<{ success: boolean }>("config/set", {
        requires_restart: 0,
        update_topic: "config/storage",
        config_data: {
          storage: {
            adaptive_transfers: adaptiveValue,
            parallel_transfers: maximumValue,
          },
        },
        expected_storage: {
          adaptive_transfers: tiers.settings.adaptive_transfers,
          parallel_transfers: tiers.settings.parallel_transfers,
        },
      });
      if (!result.data.success) throw new Error(t("saveFailed"));
      await refreshTiers();
      toast.success(t("saved"));
    } catch {
      toast.error(t("saveFailed"));
    } finally {
      setBusy(false);
    }
  };
  const copyOutput = async (value: string) => {
    try {
      if (await copy(value)) toast.success(t("copied"));
      else toast.error(t("copyFailed"));
    } catch {
      toast.error(t("copyFailed"));
    }
  };
  const output = (title: string, value: string) => (
    <section className="space-y-2">
      <Heading as="h3">{title}</Heading>
      <textarea
        aria-label={title}
        className="min-h-40 w-full rounded-md border bg-background p-3 font-mono text-xs"
        readOnly
        value={value}
      />
      <Button variant="outline" onClick={() => void copyOutput(value)}>
        {t("copy")}
      </Button>
    </section>
  );

  return (
    <div className="flex max-w-4xl flex-col gap-5 p-2">
      <Heading as="h2">{t("title")}</Heading>
      <p className="text-sm text-muted-foreground">{t("description")}</p>
      <p className="rounded-lg border p-3 text-sm">{t("localFirstHelp")}</p>
      <p className="text-sm text-muted-foreground">{t("safety")}</p>
      {error && <p role="alert">{t("unavailable")}</p>}
      {budgetExceedsFilesystem && (
        <p role="alert" className="rounded-lg border border-destructive p-3">
          {t("bufferTooLarge")}
        </p>
      )}
      <section className="space-y-3">
        <Heading as="h3">{t("mounts.title")}</Heading>
        <Button variant="outline" disabled={busy}         onClick={() =>
          void mutate().catch(() => toast.error(t("unavailable")))
        }>
          {t("mounts.refresh")}
        </Button>
        {status &&
          (["archive", "recordings"] as const).map((role) => {
            const mount = status.mounts[role];
            return (
              <div key={role} className="rounded-lg border p-3 text-sm">
                <p className="font-medium">{t(`roles.${role}`)}</p>
                <p>{mount.container_path}</p>
                <p>
                  {mount.mounted ? t("mounts.detected") : t("mounts.absent")}
                </p>
                {mount.source && <p className="break-all">{mount.source}</p>}
                {mount.mounted && (
                  <p>
                    {t("mounts.connections", {
                      count: mount.configured_connections ?? 0,
                      value: mount.configured_connections ?? t("unknown"),
                    })}
                  </p>
                )}
              </div>
            );
          })}
        <p className="text-sm text-muted-foreground">
          {t("mounts.metadataOnly")}
        </p>
      </section>
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          void generate();
        }}
      >
        {shares.map((share, index) => (
          <fieldset
            key={index}
            className="space-y-3 rounded-lg border p-3"
            disabled={busy}
          >
            <legend className="px-1">{t("share", { count: index + 1 })}</legend>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <Label htmlFor={`nfs-role-${index}`}>{t("fields.role")}</Label>
                <select
                  id={`nfs-role-${index}`}
                  className="w-full rounded border bg-background p-2"
                  value={share.role}
                  onChange={(event) => {
                    if (
                      event.target.value === "archive" ||
                      event.target.value === "recordings"
                    ) {
                      updateShare(index, { role: event.target.value });
                    }
                  }}
                >
                  <option value="archive">{t("roles.archive")}</option>
                  <option value="recordings">{t("roles.recordings")}</option>
                </select>
              </div>
              <div className="space-y-1">
                <Label htmlFor={`nfs-server-${index}`}>
                  {t("fields.server")}
                </Label>
                <Input
                  id={`nfs-server-${index}`}
                  required
                  maxLength={253}
                  value={share.server}
                  onChange={(event) =>
                    updateShare(index, { server: event.target.value })
                  }
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor={`nfs-export-${index}`}>
                  {t("fields.export")}
                </Label>
                <Input
                  id={`nfs-export-${index}`}
                  required
                  maxLength={1024}
                  value={share.export}
                  onChange={(event) =>
                    updateShare(index, { export: event.target.value })
                  }
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor={`nfs-version-${index}`}>
                  {t("fields.version")}
                </Label>
                <select
                  id={`nfs-version-${index}`}
                  className="w-full rounded border bg-background p-2"
                  value={share.version}
                  onChange={(event) => {
                    if (["3", "4.1", "4.2"].includes(event.target.value)) {
                      updateShare(index, {
                        version: event.target.value as NfsShare["version"],
                      });
                    }
                  }}
                >
                  <option value="4.1">4.1</option>
                  <option value="4.2">4.2</option>
                  <option value="3">3</option>
                </select>
              </div>
              <div className="space-y-1">
                <Label htmlFor={`nfs-connections-${index}`}>
                  {t("fields.connections")}
                </Label>
                <Input
                  id={`nfs-connections-${index}`}
                  type="number"
                  required
                  min={1}
                  max={16}
                  step={1}
                  value={share.connections}
                  onChange={(event) =>
                    updateShare(index, {
                      connections: Number(event.target.value),
                    })
                  }
                />
              </div>
            </div>
            <p className="text-sm text-muted-foreground">
              {t("connectionsHelp")}
            </p>
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                variant="outline"
                disabled={!share.server.trim()}
                onClick={() => void testServer(index)}
              >
                {t("probe.button")}
              </Button>
              <Button
                type="button"
                variant="outline"
                disabled={shares.length === 1}
                onClick={() => {
                  setShares(shares.filter((_, offset) => offset !== index));
                  setProbes({});
                  setPlan(undefined);
                  setDirectConfirmed(false);
                }}
              >
                {t("remove")}
              </Button>
            </div>
            {probes[index] && (
              <p role="status" className="text-sm">
                {probes[index].reachable
                  ? t("probe.reachable")
                  : t(`probe.errors.${probes[index].error ?? "unreachable"}`)}
              </p>
            )}
          </fieldset>
        ))}
        <Button
          type="button"
          variant="outline"
          disabled={busy || shares.length === 2}
          onClick={() => {
            setShares([
              ...shares,
              newShare(shares[0].role === "archive" ? "recordings" : "archive"),
            ]);
            setPlan(undefined);
          }}
        >
          {t("addShare")}
        </Button>
        {direct && (
          <div className="space-y-2 rounded-lg border border-destructive p-3">
            <p>{t("directWarning")}</p>
            <div className="flex items-start gap-2">
              <Checkbox
                id="nfs-direct-confirm"
                disabled={busy}
                checked={directConfirmed}
                onCheckedChange={(value) => {
                  setDirectConfirmed(value === true);
                  setPlan(undefined);
                }}
              />
              <Label htmlFor="nfs-direct-confirm">{t("directConfirm")}</Label>
            </div>
          </div>
        )}
        <Button
          type="submit"
          disabled={busy || !status || (direct && !directConfirmed)}
        >
          {t("generate")}
        </Button>
      </form>
      {plan && (
        <div className="space-y-5 rounded-lg border p-4">
          <Heading as="h3">{t("output.title")}</Heading>
          <p role="status">{t("output.noChanges")}</p>
          <p className="text-sm">{t("output.mergeWarning")}</p>
          {output(t("output.volumes"), plan.volume_definitions_yaml)}
          {output(t("output.service"), plan.service_volumes_yaml)}
          {plan.frigate_storage_yaml &&
            output(t("output.storage"), plan.frigate_storage_yaml)}
          {plan.archive_marker && (
            <p className="text-sm">{t("output.marker", plan.archive_marker)}</p>
          )}
          <p className="text-sm">
            {t("output.permissions", {
              uid: status?.runtime_uid ?? t("unknown"),
              gid: status?.runtime_gid ?? t("unknown"),
            })}
          </p>
          <p className="text-sm text-muted-foreground">
            {t("output.maintenance")}
          </p>
        </div>
      )}
      <form
        className="space-y-3 rounded-lg border p-3"
        onSubmit={(event) => {
          event.preventDefault();
          void saveAdaptive();
        }}
      >
        <Heading as="h3">{t("adaptive.title")}</Heading>
        <p className="text-sm">{t("adaptive.help")}</p>
        <div className="flex items-center gap-2">
          <Checkbox
            id="nfs-adaptive"
            disabled={busy}
            checked={adaptiveValue}
            onCheckedChange={(value) => setAdaptive(value === true)}
          />
          <Label htmlFor="nfs-adaptive">{t("adaptive.enabled")}</Label>
        </div>
        <Label htmlFor="nfs-maximum">{t("adaptive.maximum")}</Label>
        <Input
          id="nfs-maximum"
          className="max-w-40"
          type="number"
          required
          min={1}
          max={50}
          step={1}
          disabled={busy}
          value={maximumValue}
          onChange={(event) => setMaximum(Number(event.target.value))}
        />
        {transfer && (
          <p role="status">
            {t("adaptive.current", {
              count: transfer.effective_workers,
              maximum: transfer.maximum_workers,
            })}
          </p>
        )}
        <Button
          type="submit"
          disabled={
            busy ||
            !tiers ||
            tiers.settings.adaptive_transfers === undefined ||
            tiers.settings.parallel_transfers === undefined
          }
        >
          {t("adaptive.save")}
        </Button>
      </form>
    </div>
  );
}
