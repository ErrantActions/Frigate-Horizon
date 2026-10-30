import axios from "axios";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useIsAdmin } from "@/hooks/use-is-admin";
import { onvifPath, useOnvif, useOnvifMutation } from "@/hooks/use-onvif";
import type {
  EventStatus,
  PresetResponse,
  RecordingResponse,
} from "@/types/onvif";
import OnvifReplayPlayer from "./OnvifReplayPlayer";
import OnvifRecordingBindingPanel from "./OnvifRecordingBindingPanel";

export function OnvifPresetPanel({ camera }: { camera: string }) {
  const { t } = useTranslation("views/onvif");
  const isAdmin = useIsAdmin();
  const [enabled, setEnabled] = useState(false);
  const [profile, setProfile] = useState("");
  const [name, setName] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const resource = useOnvif<PresetResponse>(
    camera,
    "presets",
    enabled,
    profile ? { profile_token: profile } : undefined,
  );
  const mutation = useOnvifMutation();
  const apply = async (action: "set" | "goto" | "remove", token?: string) => {
    const result = await mutation.run<PresetResponse>(
      "post",
      onvifPath(camera, "presets"),
      {
        profile_token: profile,
        action,
        token,
        name: name || undefined,
        confirm_device_change: confirmed,
      },
    );
    if (result) await resource.mutate(result, false);
    else await resource.mutate();
    setConfirmed(false);
    setName("");
  };
  return (
    <section className="flex flex-col gap-3 rounded-lg border p-4">
      <h3 className="text-lg font-medium">{t("presets.title")}</h3>
      <p className="text-sm">{t("presets.description")}</p>
      <Button
        variant="secondary"
        disabled={mutation.busy || resource.isValidating}
        onClick={() => {
          if (enabled) void resource.mutate();
          else setEnabled(true);
        }}
      >
        {t("presets.load")}
      </Button>
      {resource.error && <p role="alert">{t("readFailed")}</p>}
      {resource.data &&
        !resource.error &&
        !resource.isValidating &&
        resource.data.profiles.length === 0 && (
          <p role="status">{t("ptzProfiles.empty")}</p>
        )}
      {resource.data && (
        <label className="flex flex-col gap-1">
          {t("presets.profile")}
          <select
            className="rounded border bg-background p-2"
            value={profile}
            disabled={mutation.busy}
            onChange={(event) => {
              setProfile(event.target.value);
              setConfirmed(false);
            }}
          >
            <option value="">{t("chooseProfile")}</option>
            {resource.data.profiles.map((item) => (
              <option key={item.token} value={item.token}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
      )}
      {resource.data?.presets.map((preset) => (
        <div key={preset.token} className="flex flex-wrap items-center gap-2">
          <span>{preset.name}</span>
          {isAdmin && (
            <>
              <Button
                variant="secondary"
                disabled={!confirmed || mutation.busy}
                onClick={() => void apply("goto", preset.token)}
              >
                {t("presets.goto")}
              </Button>
              <Button
                variant="secondary"
                disabled={!confirmed || mutation.busy}
                onClick={() => void apply("remove", preset.token)}
              >
                {t("presets.remove")}
              </Button>
              <Button
                variant="secondary"
                disabled={!confirmed || mutation.busy || !name}
                onClick={() => void apply("set", preset.token)}
              >
                {t("presets.replace")}
              </Button>
            </>
          )}
        </div>
      ))}
      {isAdmin && profile && (
        <>
          <Input
            aria-label={t("presets.name")}
            value={name}
            maxLength={64}
            disabled={mutation.busy}
            onChange={(event) => setName(event.target.value)}
          />
          <label className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              checked={confirmed}
              disabled={mutation.busy}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            {t("presets.confirm")}
          </label>
          <Button
            disabled={!confirmed || mutation.busy || !name}
            onClick={() => void apply("set")}
          >
            {t("presets.create")}
          </Button>
        </>
      )}
      {mutation.failed && (
        <p role="alert">
          {t(mutation.uncertain ? "unverified" : "writeFailed")}
        </p>
      )}
    </section>
  );
}

export function OnvifEventsPanel({ camera }: { camera: string }) {
  const { t } = useTranslation("views/onvif");
  const isAdmin = useIsAdmin();
  const [enabled, setEnabled] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [integrate, setIntegrate] = useState(false);
  const [sourceToken, setSourceToken] = useState("");
  const [readSources, setReadSources] = useState(false);
  const sources = useOnvif<{
    profiles: { token: string; name: string; video_source_token: string }[];
  }>(camera, "events/sources", readSources);
  const resource = useOnvif<EventStatus>(
    camera,
    "events",
    enabled,
    undefined,
    5000,
  );
  const mutation = useOnvifMutation();
  const toggle = async (active: boolean) => {
    const result = await mutation.run<EventStatus>(
      "post",
      onvifPath(camera, "events"),
      {
        enabled: active,
        confirm_device_change: confirmed,
        integrate_events: active && integrate,
        event_expiry_seconds: 30,
        source_profile_token: active && integrate ? sourceToken : undefined,
      },
    );
    setEnabled(true);
    if (result) await resource.mutate(result, false);
    setConfirmed(false);
    if (!active) setIntegrate(false);
  };
  return (
    <section className="flex flex-col gap-3 rounded-lg border p-4">
      <h3 className="text-lg font-medium">{t("events.title")}</h3>
      <p className="text-sm">{t("events.description")}</p>
      <p className="text-sm text-muted-foreground">
        {t("events.integrationHelp")}
      </p>
      <Button
        variant="secondary"
        onClick={() => {
          if (enabled) void resource.mutate();
          else setEnabled(true);
        }}
      >
        {t("events.history")}
      </Button>
      {resource.error && <p role="alert">{t("readFailed")}</p>}
      {resource.data && (
        <p role="status">
          {t(`events.${resource.data.state}`, { defaultValue: t("unknown") })}{" "}
          {t(
            resource.data.integration === "manual_events"
              ? "events.integrationActive"
              : "events.statusOnly",
          )}
        </p>
      )}
      {isAdmin && (
        <>
          <label className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              checked={integrate}
              disabled={mutation.busy}
              onChange={(event) => {
                setIntegrate(event.target.checked);
                setConfirmed(false);
              }}
            />
            {t("events.integrationConsent")}
          </label>
          {integrate && (
            <>
              <Button
                variant="secondary"
                disabled={mutation.busy || sources.isValidating}
                onClick={() => {
                  if (readSources) void sources.mutate();
                  else setReadSources(true);
                }}
              >
                {t("events.loadSources")}
              </Button>
              <label>
                {t("events.sourceProfile")}
                <select
                  aria-label={t("events.sourceProfile")}
                  className="w-full rounded border bg-background p-2"
                  value={sourceToken}
                  disabled={mutation.busy}
                  onChange={(event) => {
                    setSourceToken(event.target.value);
                    setConfirmed(false);
                  }}
                >
                  <option value="">{t("chooseProfile")}</option>
                  {sources.data?.profiles.map((profile) => (
                    <option key={profile.token} value={profile.token}>
                      {profile.name} ({profile.video_source_token})
                    </option>
                  ))}
                </select>
              </label>
              {sources.error && <p role="alert">{t("readFailed")}</p>}
            </>
          )}
          <label className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              checked={confirmed}
              disabled={mutation.busy}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            {t("events.confirm")}
          </label>
          <div className="flex gap-2">
            <Button
              disabled={
                !confirmed || mutation.busy || (integrate && !sourceToken)
              }
              onClick={() => void toggle(true)}
            >
              {t("events.start")}
            </Button>
            <Button
              variant="secondary"
              disabled={!confirmed || mutation.busy}
              onClick={() => void toggle(false)}
            >
              {t("events.stop")}
            </Button>
          </div>
        </>
      )}
      <ul className="max-h-64 overflow-y-auto text-sm">
        {resource.data?.events
          .slice()
          .reverse()
          .map((event, index) => (
            <li key={`${event.received_at}-${index}`}>
              {new Date(event.received_at * 1000).toLocaleString()}:{" "}
              {event.topic}
            </li>
          ))}
      </ul>
      {resource.data?.signals && (
        <p role="status">
          {t("events.signalCounts", {
            active: resource.data.signals.active,
            starts: resource.data.signals.starts,
            ends: resource.data.signals.ends,
          })}
        </p>
      )}
      {resource.data?.signals?.error_code && (
        <p role="alert">{t("events.signalUnavailable")}</p>
      )}
      {mutation.failed && <p role="alert">{t("writeFailed")}</p>}
    </section>
  );
}

export function OnvifRecordingsPanel({ camera }: { camera: string }) {
  const { t } = useTranslation("views/onvif");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [result, setResult] = useState<RecordingResponse>();
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [replay, setReplay] = useState<{
    token: string;
    start: string;
    end: string;
  }>();
  const [replaySeconds, setReplaySeconds] = useState(60);
  const search = async () => {
    setBusy(true);
    setFailed(false);
    setReplay(undefined);
    try {
      const response = await axios.get<RecordingResponse>(
        onvifPath(camera, "recordings"),
        {
          params: {
            start: new Date(start).toISOString(),
            end: new Date(end).toISOString(),
          },
        },
      );
      setResult(response.data);
    } catch {
      setFailed(true);
      setResult(undefined);
    } finally {
      setBusy(false);
    }
  };
  const openReplay = (recording: {
    token: string;
    start: string;
    end: string;
  }) => {
    const end = new Date(
      Math.min(
        new Date(recording.end).getTime(),
        new Date(recording.start).getTime() + replaySeconds * 1000,
      ),
    ).toISOString();
    setReplay({ ...recording, end });
  };
  return (
    <section className="flex flex-col gap-3 rounded-lg border p-4">
      <h3 className="text-lg font-medium">{t("recordings.title")}</h3>
      <p className="text-sm">{t("recordings.description")}</p>
      <OnvifRecordingBindingPanel camera={camera} />
      <label>
        {t("recordings.start")}
        <Input
          type="datetime-local"
          value={start}
          onChange={(event) => setStart(event.target.value)}
        />
      </label>
      <label>
        {t("recordings.end")}
        <Input
          type="datetime-local"
          value={end}
          onChange={(event) => setEnd(event.target.value)}
        />
      </label>
      <Button
        variant="secondary"
        disabled={busy || !start || !end}
        onClick={() => void search()}
      >
        {t("recordings.search")}
      </Button>
      <label>
        {t("recordings.duration")}
        <Input
          type="number"
          min={1}
          max={300}
          step={1}
          value={replaySeconds || ""}
          disabled={!!replay}
          onChange={(event) => setReplaySeconds(event.target.valueAsNumber)}
        />
      </label>
      {result && (
        <>
          <p role="status">
            {t("recordings.result", {
              count: result.recordings.length,
              limit: result.limit,
              state: result.search_state,
            })}
          </p>
          {result.recordings.map((recording) => (
            <div
              key={recording.token}
              className="flex flex-wrap items-center gap-2 text-sm"
            >
              <span>
                {recording.token}: {new Date(recording.start).toLocaleString()}{" "}
                - {new Date(recording.end).toLocaleString()}
              </span>
              <Button
                variant="secondary"
                disabled={
                  busy ||
                  !!replay ||
                  !Number.isInteger(replaySeconds) ||
                  replaySeconds < 1 ||
                  replaySeconds > 300
                }
                onClick={() => openReplay(recording)}
              >
                {t("recordings.replay")}
              </Button>
            </div>
          ))}
        </>
      )}
      {replay && (
        <OnvifReplayPlayer
          key={JSON.stringify([camera, replay.token, replay.start, replay.end])}
          camera={camera}
          recording={replay}
          onClose={() => setReplay(undefined)}
        />
      )}
      {failed && <p role="alert">{t("readFailed")}</p>}
    </section>
  );
}
