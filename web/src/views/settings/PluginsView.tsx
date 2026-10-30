import { useState } from "react";
import axios from "axios";
import useSWR, { mutate } from "swr";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import Heading from "@/components/ui/heading";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { Textarea } from "@/components/ui/textarea";
import ActivityIndicator from "@/components/indicators/activity-indicator";

type Manifest = {
  id: string;
  name: string;
  version: string;
  actions: string[];
};
type InstalledPlugin = {
  manifest: Manifest;
  enabled: boolean;
  developer: boolean;
  state: string;
  bindings: Record<string, string>;
  devices: { id: string; name: string; two_way_audio: boolean }[];
  recording_health?: Record<
    string,
    { latest_end_time: number | null; fresh: boolean }
  >;
  event_inference?: Record<
    string,
    {
      enabled: boolean;
      window_seconds: number;
      active: boolean;
      remaining_seconds: number;
    }
  >;
};
type PluginCatalog = {
  developer_mode: boolean;
  catalog: Manifest[];
  installed: InstalledPlugin[];
};

export default function PluginsView() {
  const { t } = useTranslation("views/plugins");
  const {
    data,
    error,
    mutate: refresh,
  } = useSWR<PluginCatalog>("plugins", {
    refreshInterval: 5000,
    refreshWhenHidden: false,
  });
  const [busy, setBusy] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const [interrupt, setInterrupt] = useState(false);
  const [url, setUrl] = useState("");
  const [checksum, setChecksum] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [cameraNames, setCameraNames] = useState<Record<string, string>>({});
  const [days, setDays] = useState("3");
  const [cloudGaps, setCloudGaps] = useState(false);
  const [action, setAction] = useState("");
  const [params, setParams] = useState("{}");

  async function run(operation: () => Promise<unknown>) {
    setBusy(true);
    try {
      const result = await operation();
      await Promise.all([refresh(), mutate("config")]);
      if (result !== false) toast.success(t("saved"));
    } catch (error) {
      if (!axios.isAxiosError(error)) throw error;
      toast.error(t("operationFailed"));
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  async function authenticate() {
    await run(async () => {
      const result = await axios.post<{ state: string }>(
        "plugins/ring/actions",
        {
          method: "authenticate",
          params: { email, password, code },
        },
      );
      if (result.data.state === "running") {
        setPassword("");
        setCode("");
      } else if (result.data.state === "two_factor_required") {
        toast.info(t("twoFactorPrompt"));
      } else {
        toast.error(t("authenticationFailed"));
      }
      return result.data.state === "running";
    });
  }

  function customAction(plugin: InstalledPlugin) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(params);
    } catch {
      toast.error(t("invalidJson"));
      return;
    }
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      Array.isArray(parsed)
    ) {
      toast.error(t("invalidJson"));
      return;
    }
    void run(() =>
      axios.post(`plugins/${plugin.manifest.id}/actions`, {
        method: action,
        params: parsed,
      }),
    );
  }

  if (error) return <p role="alert">{t("loadFailed")}</p>;
  if (!data) return <ActivityIndicator />;

  return (
    <div
      role="region"
      aria-label={t("title")}
      className="flex max-w-5xl flex-col gap-6 p-4"
    >
      <Heading as="h2">{t("title")}</Heading>
      <p className="text-sm text-muted-foreground">{t("description")}</p>
      <section className="space-y-3 rounded-lg border p-4">
        <Heading as="h3">{t("catalog")}</Heading>
        <p className="text-sm text-muted-foreground">{t("catalogNotice")}</p>
        {data.catalog.map((plugin) => (
          <div
            key={plugin.id}
            className="flex items-center justify-between gap-4"
          >
            <span>
              {plugin.name} {plugin.version}
            </span>
            <Button
              disabled={
                busy ||
                data.installed.some((item) => item.manifest.id === plugin.id)
              }
              onClick={() =>
                void run(() =>
                  axios.post("plugins/install", { bundled_ring: true }),
                )
              }
            >
              {t("install")}
            </Button>
          </div>
        ))}
      </section>

      <section className="space-y-3 rounded-lg border p-4">
        <Heading as="h3">{t("developerMode")}</Heading>
        <p className="text-sm text-destructive">{t("developerWarning")}</p>
        <Label className="flex items-start gap-2">
          <Checkbox
            checked={acknowledged}
            onCheckedChange={(value) => setAcknowledged(value === true)}
          />
          {t("trustAcknowledgement")}
        </Label>
        <Button
          disabled={busy || (!data.developer_mode && !acknowledged)}
          onClick={() =>
            void run(() =>
              axios.put("plugins/developer-mode", {
                enabled: !data.developer_mode,
                acknowledge_code_execution: acknowledged,
              }),
            )
          }
        >
          {data.developer_mode
            ? t("disableDeveloperMode")
            : t("enableDeveloperMode")}
        </Button>
        {data.developer_mode && (
          <div className="space-y-3">
            <Label htmlFor="plugin-url">{t("packageUrl")}</Label>
            <Input
              id="plugin-url"
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              placeholder="https://"
            />
            <Label htmlFor="plugin-checksum">{t("checksum")}</Label>
            <Input
              id="plugin-checksum"
              value={checksum}
              onChange={(event) =>
                setChecksum(event.target.value.trim().toLowerCase())
              }
            />
            <Button
              disabled={
                busy ||
                !acknowledged ||
                !url ||
                !/^[a-f0-9]{64}$/.test(checksum)
              }
              onClick={() =>
                void run(() =>
                  axios.post("plugins/install", {
                    url,
                    sha256: checksum,
                    acknowledge_code_execution: acknowledged,
                  }),
                )
              }
            >
              {t("installDeveloper")}
            </Button>
          </div>
        )}
      </section>

      <Label className="flex items-start gap-2">
        <Checkbox
          checked={interrupt}
          onCheckedChange={(value) => setInterrupt(value === true)}
        />
        {t("interruptionAcknowledgement")}
      </Label>
      {data.installed.map((plugin) => (
        <section
          key={plugin.manifest.id}
          className="space-y-4 rounded-lg border p-4"
        >
          <Heading as="h3">
            {plugin.manifest.name} {plugin.manifest.version}
            {plugin.developer && (
              <span className="ml-2 text-sm text-destructive">
                {t("developerTag")}
              </span>
            )}
          </Heading>
          <p role="status">
            {t("status", {
              state: t(`states.${plugin.state}`, {
                defaultValue: t("states.error"),
              }),
            })}
          </p>
          <div className="flex flex-wrap gap-2">
            <Button
              disabled={
                busy ||
                (plugin.enabled &&
                  Object.keys(plugin.bindings).length > 0 &&
                  !interrupt)
              }
              onClick={() =>
                void run(() =>
                  axios.put(`plugins/${plugin.manifest.id}/enabled`, {
                    enabled: !plugin.enabled,
                    acknowledge_camera_interruption: interrupt,
                  }),
                )
              }
            >
              {plugin.enabled ? t("disable") : t("enable")}
            </Button>
            <Button
              variant="destructive"
              disabled={busy || Object.keys(plugin.bindings).length > 0}
              onClick={() =>
                void run(() => axios.delete(`plugins/${plugin.manifest.id}`))
              }
            >
              {t("uninstall")}
            </Button>
          </div>
          {Object.keys(plugin.bindings).length > 0 && (
            <p className="text-sm text-muted-foreground">
              {t("removeCamerasFirst")}
            </p>
          )}
          {plugin.enabled && plugin.manifest.id === "ring" && (
            <div className="space-y-3">
              <p className="text-sm text-muted-foreground">
                {t("ringCloudNotice")}
              </p>
              <Label htmlFor="ring-email">{t("email")}</Label>
              <Input
                id="ring-email"
                type="email"
                autoComplete="username"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
              />
              <Label htmlFor="ring-password">{t("password")}</Label>
              <Input
                id="ring-password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
              <Label htmlFor="ring-code">{t("twoFactorCode")}</Label>
              <Input
                id="ring-code"
                autoComplete="one-time-code"
                value={code}
                onChange={(event) => setCode(event.target.value)}
              />
              <Button
                disabled={busy || !email || !password}
                onClick={() => void authenticate()}
              >
                {t("connectRing")}
              </Button>
              <Button
                variant="secondary"
                disabled={
                  busy ||
                  (Object.keys(plugin.bindings).length > 0 && !interrupt)
                }
                onClick={() =>
                  void run(() =>
                    axios.post("plugins/ring/actions", {
                      method: "disconnect",
                      acknowledge_camera_interruption: interrupt,
                    }),
                  )
                }
              >
                {t("disconnectRing")}
              </Button>
            </div>
          )}
          {plugin.enabled &&
            plugin.manifest.id !== "ring" &&
            plugin.manifest.actions.length > 0 && (
              <div className="space-y-3">
                <Label htmlFor={`action-${plugin.manifest.id}`}>
                  {t("action")}
                </Label>
                <select
                  id={`action-${plugin.manifest.id}`}
                  className="rounded border bg-background p-2"
                  value={action}
                  onChange={(event) => setAction(event.target.value)}
                >
                  <option value="">{t("selectAction")}</option>
                  {plugin.manifest.actions.map((method) => (
                    <option key={method} value={method}>
                      {method}
                    </option>
                  ))}
                </select>
                <Label htmlFor={`params-${plugin.manifest.id}`}>
                  {t("parameters")}
                </Label>
                <Textarea
                  id={`params-${plugin.manifest.id}`}
                  value={params}
                  onChange={(event) => setParams(event.target.value)}
                />
                <Button
                  disabled={busy || !plugin.manifest.actions.includes(action)}
                  onClick={() => customAction(plugin)}
                >
                  {t("runAction")}
                </Button>
              </div>
            )}
          {plugin.enabled && plugin.devices.length > 0 && (
            <div className="space-y-3">
              <p className="text-sm text-destructive">
                {t("recordingWarning")}
              </p>
              <Label className="flex items-start gap-2">
                <Checkbox
                  checked={cloudGaps}
                  onCheckedChange={(value) => setCloudGaps(value === true)}
                />
                {t("cloudAcknowledgement")}
              </Label>
              <Label htmlFor={`days-${plugin.manifest.id}`}>
                {t("retentionDays")}
              </Label>
              <Input
                id={`days-${plugin.manifest.id}`}
                type="number"
                min={1}
                max={365}
                value={days}
                onChange={(event) => setDays(event.target.value)}
              />
              {plugin.devices.map((device) => {
                const key = `${plugin.manifest.id}:${device.id}`;
                const bound = plugin.bindings[device.id];
                return (
                  <div key={device.id} className="space-y-2 rounded border p-3">
                    <p>{device.name}</p>
                    {bound ? (
                      <div>
                        <p>{t("boundCamera", { camera: bound })}</p>
                        <Label className="my-2 flex items-start gap-2">
                          <Checkbox
                            checked={
                              plugin.event_inference?.[bound]?.enabled ?? false
                            }
                            disabled={busy}
                            onCheckedChange={(value) =>
                              void run(() =>
                                axios.put(
                                  `plugins/${plugin.manifest.id}/cameras/${bound}/event-inference`,
                                  {
                                    enabled: value === true,
                                    window_seconds: 30,
                                    triggers: ["motion"],
                                  },
                                ),
                              )
                            }
                          />
                          {t("eventInference")}
                        </Label>
                        <p className="text-sm text-muted-foreground">
                          {plugin.event_inference?.[bound]?.active
                            ? t("inferenceActive", {
                                seconds: Math.ceil(
                                  plugin.event_inference[bound]
                                    .remaining_seconds,
                                ),
                              })
                            : t("inferenceIdle")}
                        </p>
                        <p role="status">
                          {plugin.recording_health?.[bound]?.fresh
                            ? t("recordingFresh")
                            : t("recordingStale")}
                        </p>
                        <p className="text-sm text-muted-foreground">
                          {plugin.recording_health?.[bound]?.latest_end_time
                            ? t("lastRecording", {
                                time: new Date(
                                  plugin.recording_health[bound]
                                    .latest_end_time! * 1000,
                                ).toLocaleString(),
                              })
                            : t("noCompletedRecording")}
                        </p>
                      </div>
                    ) : (
                      <>
                        <Label htmlFor={`camera-${key}`}>
                          {t("cameraName")}
                        </Label>
                        <Input
                          id={`camera-${key}`}
                          value={cameraNames[key] ?? ""}
                          onChange={(event) =>
                            setCameraNames((previous) => ({
                              ...previous,
                              [key]: event.target.value,
                            }))
                          }
                        />
                        <Button
                          disabled={
                            busy ||
                            !cloudGaps ||
                            !/^[a-zA-Z0-9_-]{1,64}$/.test(
                              cameraNames[key] ?? "",
                            ) ||
                            !Number.isInteger(Number(days)) ||
                            Number(days) < 1 ||
                            Number(days) > 365
                          }
                          onClick={() =>
                            void run(() =>
                              axios.post(
                                `plugins/${plugin.manifest.id}/cameras`,
                                {
                                  device_id: device.id,
                                  camera: cameraNames[key],
                                  continuous_days: Number(days),
                                  acknowledge_cloud_gaps: cloudGaps,
                                },
                              ),
                            )
                          }
                        >
                          {t("addCamera")}
                        </Button>
                      </>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </section>
      ))}
    </div>
  );
}
