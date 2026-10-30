import axios from "axios";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { onvifPath } from "@/hooks/use-onvif";
import type { FrigateConfig } from "@/types/frigateConfig";
import type { DeviceValue, ImageResponse } from "@/types/onvif";

type Target = {
  profile: string;
  response?: ImageResponse;
  conflicts?: string[];
  confirmed: boolean;
  busy: boolean;
  outcome?: "verified" | "unverified" | "readFailed";
};

/** Every target has an independently read source, capability preview, and consent. */
export default function OnvifImageCopyPanel({
  source,
  changes,
}: {
  source: string;
  changes: Record<string, DeviceValue>;
}) {
  const { t } = useTranslation("views/onvif");
  const { data: config } = useSWR<FrigateConfig>("config");
  const [targets, setTargets] = useState<Record<string, Target>>({});
  const [consent, setConsent] = useState(false);
  const [applying, setApplying] = useState(false);
  const names = Object.entries(config?.cameras ?? {})
    .filter(
      ([name, camera]) =>
        name !== source && camera.enabled && camera.onvif?.host,
    )
    .map(([name]) => name);
  const update = (name: string, partial: Partial<Target>) =>
    setTargets((previous) => ({
      ...previous,
      [name]: {
        ...(previous[name] ?? { profile: "", confirmed: false, busy: false }),
        ...partial,
      },
    }));
  const preview = async (name: string) => {
    update(name, { busy: true, confirmed: false, outcome: undefined });
    try {
      const response = await axios.get<ImageResponse>(
        onvifPath(name, "imaging"),
        {
          params: targets[name]?.profile
            ? { profile_token: targets[name].profile }
            : undefined,
        },
      );
      const controls = response.data.imaging?.controls ?? [];
      const conflicts = Object.entries(changes)
        .filter(([key, value]) => {
          const target = controls.find((item) => item.key === key);
          return (
            !target?.available ||
            !(
              (typeof value === "string" && target.choices.includes(value)) ||
              (typeof value === "number" &&
                target.limits &&
                value >= target.limits.minimum &&
                value <= target.limits.maximum)
            )
          );
        })
        .map(([key]) => key);
      const valueFor = (key: string) =>
        changes[key] ?? controls.find((item) => item.key === key)?.value;
      for (const [field, mode] of Object.entries({
        exposure_time: "exposure_mode",
        exposure_gain: "exposure_mode",
        exposure_iris: "exposure_mode",
        white_balance_cr_gain: "white_balance_mode",
        white_balance_cb_gain: "white_balance_mode",
      })) {
        if (field in changes && valueFor(mode) !== "MANUAL")
          conflicts.push(field);
      }
      for (const [lower, upper] of [
        ["exposure_min_time", "exposure_max_time"],
        ["exposure_min_gain", "exposure_max_gain"],
        ["exposure_min_iris", "exposure_max_iris"],
        ["focus_near_limit", "focus_far_limit"],
      ]) {
        const minimum = valueFor(lower);
        const maximum = valueFor(upper);
        if (
          typeof minimum === "number" &&
          typeof maximum === "number" &&
          minimum > maximum
        ) {
          conflicts.push(lower, upper);
        }
      }
      update(name, { response: response.data, conflicts, busy: false });
    } catch {
      update(name, {
        response: undefined,
        conflicts: undefined,
        busy: false,
        outcome: "readFailed",
      });
    }
  };
  const selected = Object.entries(targets).filter(
    ([, target]) =>
      target.confirmed &&
      target.response?.imaging &&
      !target.conflicts?.length &&
      target.response.profile_token &&
      !target.busy,
  );
  const apply = async () => {
    setApplying(true);
    setConsent(false);
    await Promise.all(
      selected.map(async ([name, target]) => {
        update(name, { busy: true, confirmed: false });
        const controls = target.response!.imaging!.controls;
        try {
          await axios.put(onvifPath(name, "imaging"), {
            profile_token: target.response!.profile_token,
            changes,
            expected_values: Object.fromEntries(
              Object.keys(changes).map((key) => [
                key,
                controls.find((item) => item.key === key)?.value ?? null,
              ]),
            ),
            confirm_device_change: true,
            force_persistence: false,
            override_seconds: 900,
          });
          update(name, {
            outcome: "verified",
            busy: false,
            response: undefined,
          });
        } catch {
          // A target failing must not trigger retries or rollback writes on other cameras.
          update(name, {
            outcome: "unverified",
            busy: false,
            response: undefined,
          });
        }
      }),
    );
    setApplying(false);
  };
  const signature = JSON.stringify(changes);
  return (
    <details className="rounded border p-2" key={signature}>
      <summary>{t("copy.title")}</summary>
      <p className="my-2 text-sm">{t("copy.help")}</p>
      {!Object.keys(changes).length ? (
        <p>{t("copy.chooseChanges")}</p>
      ) : (
        <>
          <p className="text-sm">
            {Object.keys(changes)
              .map((key) => t(`controls.${key}`))
              .join(", ")}
          </p>
          {names.map((name) => {
            const target = targets[name];
            return (
              <div
                className="my-3 flex flex-col gap-2 rounded border p-3"
                key={name}
              >
                <span>{name}</span>
                <Button
                  variant="secondary"
                  disabled={applying || target?.busy}
                  onClick={() => void preview(name)}
                >
                  {t("copy.preview")}
                </Button>
                {target?.response && (
                  <label className="flex flex-col gap-1">
                    {t("imaging.source")}
                    <select
                      className="rounded border bg-background p-2"
                      value={
                        target.profile || target.response.profile_token || ""
                      }
                      disabled={applying || target.busy}
                      onChange={(event) =>
                        update(name, {
                          profile: event.target.value,
                          response: undefined,
                          confirmed: false,
                        })
                      }
                    >
                      <option value="">{t("chooseProfile")}</option>
                      {target.response.profiles.map((profile) => (
                        <option key={profile.token} value={profile.token}>
                          {profile.name} ({profile.video_source_token})
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                {target?.conflicts?.length ? (
                  <p role="status">
                    {t("copy.incompatible", {
                      fields: target.conflicts
                        .map((key) => t(`controls.${key}`))
                        .join(", "),
                    })}
                  </p>
                ) : (
                  target?.response?.imaging && (
                    <>
                      <p role="status">{t("copy.compatible")}</p>
                      <ul className="text-xs">
                        {target.response.imaging.controls
                          .filter((item) => item.key in changes)
                          .map((item) => (
                            <li key={item.key}>
                              {t(`controls.${item.key}`)}:{" "}
                              {item.value ?? t("unknown")} → {changes[item.key]}
                              {item.limits &&
                                ` (${t("range", { min: item.limits.minimum, max: item.limits.maximum })})`}
                            </li>
                          ))}
                      </ul>
                      <label className="flex items-start gap-2 text-sm">
                        <input
                          type="checkbox"
                          checked={target.confirmed}
                          disabled={applying || target.busy}
                          onChange={(event) =>
                            update(name, { confirmed: event.target.checked })
                          }
                        />
                        {t("copy.targetConsent", { camera: name })}
                      </label>
                    </>
                  )
                )}
                {target?.outcome && (
                  <p role="status">{t(`copy.${target.outcome}`)}</p>
                )}
              </div>
            );
          })}
          <label className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              checked={consent}
              disabled={applying}
              onChange={(event) => setConsent(event.target.checked)}
            />
            {t("copy.consent")}
          </label>
          <Button
            className="mt-2"
            disabled={!consent || applying || !selected.length}
            onClick={() => void apply()}
          >
            {t("copy.apply", { count: selected.length })}
          </Button>
        </>
      )}
    </details>
  );
}
