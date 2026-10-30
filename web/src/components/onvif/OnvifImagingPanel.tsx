import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useIsAdmin } from "@/hooks/use-is-admin";
import { onvifPath, useOnvif, useOnvifMutation } from "@/hooks/use-onvif";
import type {
  DeviceValue,
  ImageResponse,
  OwnershipStatus,
} from "@/types/onvif";
import OnvifImageCopyPanel from "./OnvifImageCopyPanel";

export default function OnvifImagingPanel({ camera }: { camera: string }) {
  return <ImagingControls key={camera} camera={camera} />;
}

function ImagingControls({ camera }: { camera: string }) {
  const { t } = useTranslation("views/onvif");
  const isAdmin = useIsAdmin();
  const [enabled, setEnabled] = useState(false);
  const [profile, setProfile] = useState("");
  const [changes, setChanges] = useState<Record<string, DeviceValue>>({});
  const [confirmed, setConfirmed] = useState(false);
  const [persist, setPersist] = useState(false);
  const { data, error, isValidating, mutate } = useOnvif<ImageResponse>(
    camera,
    "imaging",
    enabled,
    profile ? { profile_token: profile } : undefined,
  );
  const ownership = useOnvif<OwnershipStatus>(
    camera,
    "ownership",
    enabled,
    undefined,
    5000,
  );
  const mutation = useOnvifMutation();
  const [importText, setImportText] = useState("");
  const [invalidImport, setInvalidImport] = useState(false);
  const reset = () => {
    setChanges({});
    setConfirmed(false);
    setPersist(false);
  };
  const apply = async () => {
    const expected = Object.fromEntries(
      (data?.imaging?.controls ?? [])
        .filter((control) => control.key in changes)
        .map((control) => [control.key, control.value]),
    );
    const result = await mutation.run<ImageResponse>(
      "put",
      onvifPath(camera, "imaging"),
      {
        profile_token: data?.profile_token,
        changes,
        expected_values: expected,
        confirm_device_change: confirmed,
        force_persistence: persist,
        override_seconds: 900,
      },
    );
    // Read back after failure, but never repeat the write.
    if (result) {
      await mutate(result, false);
      reset();
    } else {
      await mutate();
      setConfirmed(false);
    }
    await ownership.mutate();
  };
  const importSettings = () => {
    try {
      const parsed: unknown = JSON.parse(importText);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error();
      }
      const controls = data?.imaging?.controls ?? [];
      const entries = Object.entries(parsed);
      if (!entries.length || entries.length > 40) throw new Error();
      for (const [key, value] of entries) {
        const control = controls.find((item) => item.key === key);
        if (
          !control?.available ||
          !(
            (typeof value === "string" && control.choices.includes(value)) ||
            (typeof value === "number" &&
              Number.isFinite(value) &&
              control.limits &&
              value >= control.limits.minimum &&
              value <= control.limits.maximum)
          )
        ) {
          throw new Error();
        }
      }
      setChanges(Object.fromEntries(entries) as Record<string, DeviceValue>);
      setConfirmed(false);
      setInvalidImport(false);
    } catch {
      setInvalidImport(true);
    }
  };
  return (
    <section className="flex flex-col gap-3 rounded-lg border p-4">
      <h3 className="text-lg font-medium">{t("imaging.title")}</h3>
      <p className="text-sm text-muted-foreground">
        {t("imaging.description")}
      </p>
      <p className="text-sm text-muted-foreground">
        {t("imaging.noiseReduction")}
      </p>
      <Button
        variant="secondary"
        disabled={isValidating || mutation.busy}
        onClick={() => {
          reset();
          if (enabled) void mutate();
          else setEnabled(true);
        }}
      >
        {enabled ? t("refresh") : t("connect")}
      </Button>
      {error && <p role="alert">{t("readFailed")}</p>}
      {data && (
        <>
          <label className="flex flex-col gap-1">
            {t("imaging.source")}
            <select
              aria-label={t("imaging.source")}
              className="rounded border bg-background p-2"
              value={profile || data.profile_token || ""}
              disabled={mutation.busy}
              onChange={(event) => {
                setProfile(event.target.value);
                reset();
              }}
            >
              <option value="">{t("chooseProfile")}</option>
              {data.profiles.map((item) => (
                <option key={item.token} value={item.token}>
                  {item.name} ({item.video_source_token ?? t("unknown")})
                </option>
              ))}
            </select>
          </label>
          {data.state === "choose_profile" && (
            <p role="status">{t("imaging.chooseSource")}</p>
          )}
          {data.state === "read_only" && <p>{t("readOnly")}</p>}
          <div className="grid gap-3 sm:grid-cols-2">
            {data.imaging?.controls
              .filter((control) => control.available || control.value !== null)
              .map((control) => (
                <label
                  key={control.key}
                  className="flex flex-col gap-1 text-sm"
                >
                  {t(`controls.${control.key}`)}
                  {control.choices.length > 0 ? (
                    <select
                      aria-label={t(`controls.${control.key}`)}
                      className="rounded border bg-background p-2"
                      disabled={!isAdmin || mutation.busy || !control.available}
                      value={String(
                        changes[control.key] ?? control.value ?? "",
                      )}
                      onChange={(event) =>
                        setChanges((previous) => ({
                          ...previous,
                          [control.key]: event.target.value,
                        }))
                      }
                    >
                      {!control.choices.includes(String(control.value)) && (
                        <option value={String(control.value ?? "")}>
                          {control.value ?? t("unknown")}
                        </option>
                      )}
                      {control.choices.map((choice) => (
                        <option key={choice} value={choice}>
                          {choice}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <Input
                      aria-label={t(`controls.${control.key}`)}
                      type="number"
                      step="any"
                      disabled={!isAdmin || mutation.busy || !control.available}
                      min={control.limits?.minimum}
                      max={control.limits?.maximum}
                      value={changes[control.key] ?? control.value ?? ""}
                      onChange={(event) => {
                        const value = event.target.valueAsNumber;
                        setChanges((previous) => {
                          const next = { ...previous };
                          if (Number.isFinite(value)) next[control.key] = value;
                          else delete next[control.key];
                          return next;
                        });
                      }}
                    />
                  )}
                  {control.limits && (
                    <span className="text-muted-foreground">
                      {t(control.limits_source === "onvif_standard" ? "standardRange" : "range", {
                        min: control.limits.minimum,
                        max: control.limits.maximum,
                      })}
                    </span>
                  )}
                </label>
              ))}
          </div>
          {ownership.data && (
            <p role="status">
              {t("ownership.label")}:{" "}
              {t(`ownership.${ownership.data.phase}`, {
                defaultValue: t("unknown"),
              })}
              {ownership.data.manual_remaining_seconds > 0 &&
                ` (${t("ownership.remaining", { seconds: Math.ceil(ownership.data.manual_remaining_seconds) })})`}
            </p>
          )}
          {isAdmin && data.imaging && (
            <>
              <OnvifImageCopyPanel
                key={JSON.stringify(changes)}
                source={camera}
                changes={changes}
              />
              <details className="rounded border p-2">
                <summary>{t("profiles.transfer")}</summary>
                <p className="my-2 text-sm">{t("profiles.transferHelp")}</p>
                <textarea
                  aria-label={t("profiles.values")}
                  className="min-h-32 w-full rounded border bg-background p-2 font-mono text-xs"
                  value={importText}
                  onChange={(event) => setImportText(event.target.value)}
                />
                <div className="flex flex-wrap gap-2">
                  <Button variant="secondary" onClick={importSettings}>
                    {t("profiles.validate")}
                  </Button>
                  <Button
                    variant="secondary"
                    onClick={() =>
                      setImportText(
                        JSON.stringify(
                          Object.fromEntries(
                            data
                              .imaging!.controls.filter(
                                (item) => item.available && item.value !== null,
                              )
                              .map((item) => [item.key, item.value]),
                          ),
                          null,
                          2,
                        ),
                      )
                    }
                  >
                    {t("profiles.capture")}
                  </Button>
                </div>
                {invalidImport && <p role="alert">{t("profiles.invalid")}</p>}
              </details>
              <label className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={confirmed}
                  disabled={mutation.busy}
                  onChange={(event) => setConfirmed(event.target.checked)}
                />
                {t("confirm")}
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
                  !confirmed || mutation.busy || !Object.keys(changes).length
                }
                onClick={() => void apply()}
              >
                {t("apply")}
              </Button>
              {(ownership.data?.blocked ||
                ownership.data?.phase === "manual_override") && (
                <Button
                  variant="secondary"
                  disabled={!confirmed || mutation.busy}
                  onClick={async () => {
                    await mutation.run("post", onvifPath(camera, "resume"), {
                      confirm_device_change: true,
                    });
                    setConfirmed(false);
                    await ownership.mutate();
                  }}
                >
                  {t("ownership.resume")}
                </Button>
              )}
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
