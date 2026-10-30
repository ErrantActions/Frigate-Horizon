import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { useOnvif } from "@/hooks/use-onvif";
import type { MediaCatalog } from "@/types/onvif";

export default function OnvifProfilesPanel({ camera }: { camera: string }) {
  const { t } = useTranslation("views/onvif");
  const [service, setService] = useState<"media" | "media2">("media");
  const [requested, setRequested] = useState<string>();
  const resource = useOnvif<MediaCatalog>(
    camera,
    `profiles/${service}`,
    requested === service,
  );
  return (
    <section className="flex flex-col gap-3 rounded-lg border p-4">
      <h3 className="text-lg font-medium">{t("catalog.title")}</h3>
      <p className="text-sm">{t("catalog.description")}</p>
      <label>
        {t("encoder.service")}
        <select
          className="ml-2 rounded border bg-background p-2"
          value={service}
          onChange={(event) =>
            setService(event.target.value as "media" | "media2")
          }
        >
          <option value="media">{t("encoder.media1")}</option>
          <option value="media2">{t("encoder.media2")}</option>
        </select>
      </label>
      <Button
        variant="secondary"
        disabled={resource.isValidating}
        onClick={() => {
          if (requested === service) void resource.mutate();
          else setRequested(service);
        }}
      >
        {t("catalog.load")}
      </Button>
      {resource.error && <p role="alert">{t("readFailed")}</p>}
      {resource.data?.state === "unsupported" && (
        <p role="status">{t("catalog.empty")}</p>
      )}
      {resource.data?.profiles.map((profile) => (
        <div className="rounded border p-3 text-sm" key={profile.token}>
          <h4 className="font-medium">
            {profile.name} ({profile.token})
          </h4>
          {(
            [
              "video_source_token",
              "audio_source_token",
              "video_encoder_token",
              "audio_encoder_token",
              "ptz_configuration_token",
            ] as const
          ).map((key) => (
            <p key={key}>
              {t(`catalog.${key}`)}: {profile[key] ?? t("notAdvertised")}
            </p>
          ))}
        </div>
      ))}
    </section>
  );
}
