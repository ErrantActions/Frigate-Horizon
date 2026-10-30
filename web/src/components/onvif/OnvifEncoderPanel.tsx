import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { onvifPath, useOnvif, useOnvifMutation } from "@/hooks/use-onvif";
import { useIsAdmin } from "@/hooks/use-is-admin";
import type { EncoderResponse } from "@/types/onvif";

export default function OnvifEncoderPanel({ camera }: { camera: string }) {
  const { t } = useTranslation("views/onvif");
  const [service, setService] = useState<"media" | "media2">("media");
  return (
    <section className="flex flex-col gap-3 rounded-lg border p-4">
      <h3 className="text-lg font-medium">{t("encoder.title")}</h3>
      <p className="text-sm text-muted-foreground">
        {t("encoder.description")}
      </p>
      <label className="flex flex-col gap-1">
        {t("encoder.service")}
        <select
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
      <EncoderControls
        key={`${camera}-${service}`}
        camera={camera}
        service={service}
      />
    </section>
  );
}

function EncoderControls({
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
  const [confirmed, setConfirmed] = useState(false);
  const [persist, setPersist] = useState(false);
  const [changes, setChanges] = useState<
    Record<string, string | number | { width: number; height: number }>
  >({});
  const resource = useOnvif<EncoderResponse>(
    camera,
    `encoder/${service}`,
    enabled,
    profile ? { profile_token: profile } : undefined,
  );
  const mutation = useOnvifMutation();
  const currentEncoder = resource.data?.encoder;
  const selectedEncoding =
    typeof changes.encoding === "string"
      ? changes.encoding
      : currentEncoder?.encoding;
  const encoder = currentEncoder && {
    ...currentEncoder,
    options:
      currentEncoder.codec_options?.[selectedEncoding ?? ""] ??
      currentEncoder.options,
    frame_rates:
      currentEncoder.codec_frame_rates?.[selectedEncoding ?? ""] ??
      currentEncoder.frame_rates,
  };
  const apply = async () => {
    const result = await mutation.run<EncoderResponse>(
      "put",
      onvifPath(camera, `encoder/${service}`),
      {
        profile_token: profile,
        changes,
        expected_revision: encoder?.revision,
        confirm_device_change: confirmed,
        force_persistence: persist,
      },
    );
    if (result) {
      await resource.mutate(result, false);
      setChanges({});
    } else {
      await resource.mutate();
    }
    setConfirmed(false);
  };
  return (
    <>
      <Button
        variant="secondary"
        disabled={resource.isValidating || mutation.busy}
        onClick={() => {
          setChanges({});
          setConfirmed(false);
          if (enabled) void resource.mutate();
          else setEnabled(true);
        }}
      >
        {enabled ? t("refresh") : t("encoder.load")}
      </Button>
      {resource.error && <p role="alert">{t("readFailed")}</p>}
      {resource.data && (
        <label className="flex flex-col gap-1">
          {t("encoder.profile")}
          <select
            className="rounded border bg-background p-2"
            value={profile}
            disabled={mutation.busy}
            onChange={(event) => {
              setProfile(event.target.value);
              setChanges({});
              setConfirmed(false);
              setPersist(false);
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
      {encoder && (
        <>
          <p>{t("encoder.codec", { codec: encoder.encoding })}</p>
          <label className="flex flex-col gap-1">
            {t("encoder.targetCodec")}
            <select
              className="rounded border bg-background p-2"
              disabled={!isAdmin || mutation.busy}
              value={selectedEncoding}
              onChange={(event) => {
                setChanges({ encoding: event.target.value });
                setConfirmed(false);
              }}
            >
              <option value={currentEncoder?.encoding}>
                {currentEncoder?.encoding}
              </option>
              {Object.keys(encoder.codec_options ?? {})
                .filter((codec) => codec !== currentEncoder?.encoding)
                .map((codec) => (
                  <option key={codec} value={codec}>
                    {codec}
                  </option>
                ))}
            </select>
          </label>
          {(encoder.codec_profiles?.[selectedEncoding ?? ""]?.length ?? 0) >
            0 && (
            <label className="flex flex-col gap-1">
              {t("encoder.codecProfile")}
              <select
                className="rounded border bg-background p-2"
                disabled={!isAdmin || mutation.busy}
                value={String(
                  changes.codec_profile ??
                    (selectedEncoding === currentEncoder?.encoding
                      ? (encoder.codec_profile ?? "")
                      : ""),
                )}
                onChange={(event) =>
                  setChanges((previous) => ({
                    ...previous,
                    codec_profile: event.target.value,
                  }))
                }
              >
                <option value="">{t("encoder.chooseCodecProfile")}</option>
                {encoder.codec_profiles?.[selectedEncoding ?? ""]?.map(
                  (profile) => (
                    <option key={profile} value={profile}>
                      {profile}
                    </option>
                  ),
                )}
              </select>
            </label>
          )}
          {selectedEncoding !== currentEncoder?.encoding && (
            <p role="status">{t("encoder.switchHelp")}</p>
          )}
          <p className="text-sm">
            {t("encoder.shared", {
              profiles: encoder.shared_profiles.join(", "),
            })}
          </p>
          {!encoder.editable && <p>{t("readOnly")}</p>}
          <label className="flex flex-col gap-1">
            {t("encoder.resolution")}
            <select
              className="rounded border bg-background p-2"
              disabled={!isAdmin || !encoder.editable || mutation.busy}
              value={
                changes.resolution
                  ? JSON.stringify(changes.resolution)
                  : JSON.stringify(encoder.resolution)
              }
              onChange={(event) =>
                setChanges((previous) => ({
                  ...previous,
                  resolution: JSON.parse(event.target.value),
                }))
              }
            >
              <option value={JSON.stringify(encoder.resolution)}>
                {encoder.resolution
                  ? `${encoder.resolution.width} × ${encoder.resolution.height}`
                  : t("unknown")}
              </option>
              {encoder.options.resolutions
                .filter(
                  (item) =>
                    JSON.stringify(item) !== JSON.stringify(encoder.resolution),
                )
                .map((item) => (
                  <option
                    key={`${item.width}-${item.height}`}
                    value={JSON.stringify(item)}
                  >
                    {item.width} × {item.height}
                  </option>
                ))}
            </select>
          </label>
          <div className="grid gap-3 sm:grid-cols-2">
            {(
              [
                "frame_rate",
                "bitrate_kbps",
                "quality",
                "gop_length",
                "encoding_interval",
              ] as const
            ).map((key) => {
              const limits = encoder.options[key];
              const discrete = key === "frame_rate" && service === "media2";
              return (
                <label key={key} className="flex flex-col gap-1 text-sm">
                  {t(`encoder.${key}`)}
                  {discrete ? (
                    <select
                      className="rounded border bg-background p-2"
                      disabled={
                        !isAdmin ||
                        mutation.busy ||
                        !encoder.frame_rates.length ||
                        !encoder.editable
                      }
                      value={Number(changes[key] ?? encoder[key] ?? "")}
                      onChange={(event) =>
                        setChanges((previous) => ({
                          ...previous,
                          [key]: Number(event.target.value),
                        }))
                      }
                    >
                      <option value={encoder.frame_rate ?? ""}>
                        {encoder.frame_rate ?? t("unknown")}
                      </option>
                      {encoder.frame_rates
                        .filter((value) => value !== encoder.frame_rate)
                        .map((value) => (
                          <option key={value} value={value}>
                            {value}
                          </option>
                        ))}
                    </select>
                  ) : (
                    <Input
                      type="number"
                      min={limits?.minimum}
                      max={limits?.maximum}
                      step={key === "quality" ? "any" : 1}
                      disabled={
                        !isAdmin ||
                        mutation.busy ||
                        !limits ||
                        !encoder.editable
                      }
                      value={
                        changes[key] !== undefined
                          ? Number(changes[key])
                          : (encoder[key] ?? "")
                      }
                      onChange={(event) => {
                        const value = event.target.valueAsNumber;
                        setChanges((previous) => {
                          const next = { ...previous };
                          if (Number.isFinite(value)) next[key] = value;
                          else delete next[key];
                          return next;
                        });
                      }}
                    />
                  )}
                  {limits && (
                    <span>
                      {t("range", { min: limits.minimum, max: limits.maximum })}
                    </span>
                  )}
                </label>
              );
            })}
          </div>
          {isAdmin && encoder.editable && (
            <>
              {encoder.persistence_required && (
                <p role="status">{t("encoder.persistenceRequired")}</p>
              )}
              <label className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={confirmed}
                  disabled={mutation.busy}
                  onChange={(event) => setConfirmed(event.target.checked)}
                />
                {t("encoder.confirm")}
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
                  (encoder.persistence_required && !persist)
                }
                onClick={() => void apply()}
              >
                {t("apply")}
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
    </>
  );
}
