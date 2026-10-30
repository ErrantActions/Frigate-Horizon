import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { useIsAdmin } from "@/hooks/use-is-admin";
import { onvifPath, useOnvif, useOnvifMutation } from "@/hooks/use-onvif";
import type { AudioEncoderResponse } from "@/types/onvif";

export default function OnvifAudioPanel({ camera }: { camera: string }) {
  const { t } = useTranslation("views/onvif");
  const [service, setService] = useState<"media" | "media2">("media");
  return (
    <div className="flex flex-col gap-3">
      <label className="flex flex-col gap-1">
        {t("encoder.service")}
        <select
          aria-label={t("encoder.service")}
          className="rounded border bg-background p-2"
          value={service}
          onChange={(event) =>
            setService(event.target.value as "media" | "media2")
          }
        >
          <option value="media">{t("encoder.media1")}</option>
          <option value="media2">{t("encoder.media2")}</option>
        </select>
      </label>
      <AudioControls
        key={`${camera}-${service}`}
        camera={camera}
        service={service}
      />
    </div>
  );
}

function AudioControls({
  camera,
  service,
}: {
  camera: string;
  service: "media" | "media2";
}) {
  const { t } = useTranslation("views/onvif");
  const isAdmin = useIsAdmin();
  const [enabled, setEnabled] = useState(false);
  const [profile, setProfile] = useState("");
  const [changes, setChanges] = useState<{
    encoding?: string;
    bitrate_kbps?: number;
    sample_rate_khz?: number;
  }>({});
  const [confirmed, setConfirmed] = useState(false);
  const [persist, setPersist] = useState(false);
  const resource = useOnvif<AudioEncoderResponse>(
    camera,
    "audio-encoder",
    enabled,
    profile ? { service, profile_token: profile } : { service },
  );
  const mutation = useOnvifMutation();
  const encoder = resource.data?.encoder;
  const encoding = changes.encoding ?? encoder?.encoding;
  const rows =
    encoder?.options.filter((row) => row.encoding === encoding) ?? [];
  const reset = () => {
    setChanges({});
    setConfirmed(false);
    setPersist(false);
  };
  const apply = async () => {
    const result = await mutation.run<AudioEncoderResponse>(
      "put",
      onvifPath(camera, "audio-encoder"),
      {
        profile_token: profile,
        changes,
        expected_revision: encoder?.revision,
        confirm_device_change: confirmed,
        force_persistence: persist,
        service,
      },
    );
    if (result) {
      await resource.mutate(result, false);
      reset();
    } else {
      await resource.mutate();
      setConfirmed(false);
    }
  };
  return (
    <section className="flex flex-col gap-3 rounded-lg border p-4">
      <h3 className="text-lg font-medium">{t("audio.title")}</h3>
      <p className="text-sm">{t("audio.description")}</p>
      <Button
        variant="secondary"
        disabled={resource.isValidating || mutation.busy}
        onClick={() => {
          reset();
          if (enabled) void resource.mutate();
          else setEnabled(true);
        }}
      >
        {t("audio.load")}
      </Button>
      {resource.error && <p role="alert">{t("readFailed")}</p>}
      {resource.data && (
        <label className="flex flex-col gap-1">
          {t("audio.profile")}
          <select
            aria-label={t("audio.profile")}
            className="rounded border bg-background p-2"
            value={profile}
            disabled={mutation.busy}
            onChange={(event) => {
              setProfile(event.target.value);
              reset();
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
      {resource.data?.state === "unsupported" && (
        <p role="status">{t("audio.unsupported")}</p>
      )}
      {encoder && (
        <>
          {encoder.persistence_required && (
            <p role="status">{t("audio.persistenceRequired")}</p>
          )}
          <p>
            {t("encoder.shared", {
              profiles: encoder.shared_profiles.join(", "),
            })}
          </p>
          {!encoder.editable && <p role="status">{t("readOnly")}</p>}
          <label className="flex flex-col gap-1">
            {t("audio.encoding")}
            <select
              aria-label={t("audio.encoding")}
              className="rounded border bg-background p-2"
              disabled={!isAdmin || !encoder.editable || mutation.busy}
              value={encoding}
              onChange={(event) => {
                setChanges({ encoding: event.target.value });
                setConfirmed(false);
              }}
            >
              <option value={encoder.encoding}>{encoder.encoding}</option>
              {[...new Set(encoder.options.map((row) => row.encoding))]
                .filter((item) => item !== encoder.encoding)
                .map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
            </select>
          </label>
          {(["bitrate_kbps", "sample_rate_khz"] as const).map((key) => {
            const values = [
              ...new Set(
                rows.flatMap((row) =>
                  key === "bitrate_kbps"
                    ? row.bitrates_kbps
                    : row.sample_rates_khz,
                ),
              ),
            ];
            return (
              <label key={key} className="flex flex-col gap-1">
                {t(`audio.${key}`)}
                <select
                  aria-label={t(`audio.${key}`)}
                  className="rounded border bg-background p-2"
                  value={changes[key] ?? encoder[key] ?? ""}
                  disabled={!isAdmin || !values.length || mutation.busy}
                  onChange={(event) => {
                    setChanges((previous) => ({
                      ...previous,
                      [key]: Number(event.target.value),
                    }));
                    setConfirmed(false);
                  }}
                >
                  <option value={encoder[key] ?? ""}>
                    {encoder[key] ?? t("unknown")}
                  </option>
                  {values
                    .filter((item) => item !== encoder[key])
                    .map((item) => (
                      <option key={item} value={item}>
                        {item}
                      </option>
                    ))}
                </select>
              </label>
            );
          })}
          {isAdmin && (
            <>
              <label className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={confirmed}
                  disabled={mutation.busy}
                  onChange={(event) => setConfirmed(event.target.checked)}
                />
                {t("audio.confirm")}
              </label>
              <label className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={persist}
                  disabled={mutation.busy}
                  onChange={(event) => setPersist(event.target.checked)}
                />
                {t("persist")}
              </label>
              <Button
                disabled={
                  !confirmed ||
                  mutation.busy ||
                  !Object.keys(changes).length ||
                  (!!encoder.persistence_required && !persist)
                }
                onClick={() => void apply()}
              >
                {t("audio.apply")}
              </Button>
            </>
          )}
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
