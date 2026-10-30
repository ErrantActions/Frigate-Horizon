import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useIsAdmin } from "@/hooks/use-is-admin";
import { onvifPath, useOnvif, useOnvifMutation } from "@/hooks/use-onvif";
import type { FocusResponse } from "@/types/onvif";

export default function OnvifFocusPanel({ camera }: { camera: string }) {
  const { t } = useTranslation("views/onvif");
  const isAdmin = useIsAdmin();
  const [enabled, setEnabled] = useState(false);
  const [profile, setProfile] = useState("");
  const [position, setPosition] = useState<number>();
  const [speed, setSpeed] = useState<number>();
  const [confirmed, setConfirmed] = useState(false);
  const resource = useOnvif<FocusResponse>(
    camera,
    "focus",
    enabled,
    profile ? { profile_token: profile } : undefined,
  );
  const mutation = useOnvifMutation();
  const focus = resource.data?.focus;
  const reset = () => {
    setPosition(undefined);
    setSpeed(undefined);
    setConfirmed(false);
  };
  const apply = async () => {
    const result = await mutation.run<FocusResponse>(
      "put",
      onvifPath(camera, "focus"),
      {
        profile_token: resource.data?.profile_token,
        position,
        speed,
        expected_revision: focus?.revision,
        confirm_device_change: confirmed,
      },
    );
    if (result) await resource.mutate(result, false);
    else await resource.mutate();
    reset();
  };
  return (
    <section className="flex flex-col gap-3 rounded-lg border p-4">
      <h3 className="text-lg font-medium">{t("focus.title")}</h3>
      <p className="text-sm">{t("focus.description")}</p>
      <Button
        variant="secondary"
        disabled={resource.isValidating || mutation.busy}
        onClick={() => {
          reset();
          if (enabled) void resource.mutate();
          else setEnabled(true);
        }}
      >
        {t("focus.load")}
      </Button>
      {resource.error && <p role="alert">{t("readFailed")}</p>}
      {resource.data && (
        <label className="flex flex-col gap-1">
          {t("imaging.source")}
          <select
            className="rounded border bg-background p-2"
            value={profile || resource.data.profile_token || ""}
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
        <p role="status">{t("focus.unsupported")}</p>
      )}
      {focus && (
        <>
          {!focus.editable && <p role="status">{t("focus.manualRequired")}</p>}
          <label className="flex flex-col gap-1">
            {t("focus.position")}
            <Input
              type="number"
              step="any"
              min={focus.position_range.minimum}
              max={focus.position_range.maximum}
              value={position ?? focus.position ?? ""}
              disabled={!isAdmin || !focus.editable || mutation.busy}
              onChange={(event) => {
                setPosition(
                  Number.isFinite(event.target.valueAsNumber)
                    ? event.target.valueAsNumber
                    : undefined,
                );
                setConfirmed(false);
              }}
            />
          </label>
          {focus.speed_range && (
            <label className="flex flex-col gap-1">
              {t("focus.speed")}
              <Input
                type="number"
                step="any"
                min={focus.speed_range.minimum}
                max={focus.speed_range.maximum}
                value={speed ?? ""}
                disabled={!isAdmin || !focus.editable || mutation.busy}
                onChange={(event) => {
                  setSpeed(
                    Number.isFinite(event.target.valueAsNumber)
                      ? event.target.valueAsNumber
                      : undefined,
                  );
                  setConfirmed(false);
                }}
              />
            </label>
          )}
          {isAdmin && (
            <>
              <label className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={confirmed}
                  disabled={mutation.busy || !focus.editable}
                  onChange={(event) => setConfirmed(event.target.checked)}
                />
                {t("focus.confirm")}
              </label>
              <Button
                disabled={
                  !confirmed ||
                  position === undefined ||
                  mutation.busy ||
                  !focus.editable
                }
                onClick={() => void apply()}
              >
                {t("focus.apply")}
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
