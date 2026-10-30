import type { WidgetProps } from "@rjsf/utils";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { ConfigFormContext } from "@/types/configForm";
import type { PresetResponse } from "@/types/onvif";
import { useOnvif } from "@/hooks/use-onvif";
import { Button } from "@/components/ui/button";
import { getSizedFieldClassName } from "../utils";
import ActivityIndicator from "@/components/indicators/activity-indicator";
import { cn } from "@/lib/utils";

const AUTO_VALUE = "__auto__";

export function OnvifProfileWidget(props: WidgetProps) {
  const { id, value, disabled, readonly, onChange, schema, options } = props;
  const { t } = useTranslation(["views/settings", "views/onvif"]);

  const formContext = props.registry?.formContext as
    ConfigFormContext | undefined;
  const cameraName = formContext?.cameraName;
  const isCameraLevel = formContext?.level === "camera";
  const hasOnvifHost = !!formContext?.fullCameraConfig?.onvif?.host;

  const [requestedCamera, setRequestedCamera] = useState<string>();
  const canRead = isCameraLevel && !!cameraName && hasOnvifHost;
  const { data, error, isValidating, mutate } = useOnvif<PresetResponse>(
    cameraName ?? "",
    "presets",
    canRead && requestedCamera === cameraName,
  );

  const profiles = data?.profiles ?? [];
  const fieldClassName = getSizedFieldClassName(options, "md");
  const hasProfiles = profiles.length > 0;
  const selected = value ?? AUTO_VALUE;

  return (
    <div className={cn("flex flex-col gap-2", fieldClassName)}>
      <Select
        value={String(selected)}
        onValueChange={(val) => {
          onChange(val === AUTO_VALUE ? null : val);
        }}
        disabled={disabled || readonly}
      >
        <SelectTrigger id={id} className={cn("text-left", fieldClassName)}>
          <SelectValue placeholder={schema.title || t("onvif.profileAuto")} />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={AUTO_VALUE}>{t("onvif.profileAuto")}</SelectItem>
          {profiles.map((p) => (
            <SelectItem key={p.token} value={p.token}>
              {p.name !== p.token ? `${p.name} (${p.token})` : p.token}
            </SelectItem>
          ))}
          {!hasProfiles && value && value !== AUTO_VALUE && (
            <SelectItem value={String(value)}>{String(value)}</SelectItem>
          )}
        </SelectContent>
      </Select>
      {canRead && (
        <>
          <p className="text-sm text-muted-foreground">
            {t("ptzProfiles.savedConnection", { ns: "views/onvif" })}
          </p>
          <Button
            type="button"
            variant="secondary"
            disabled={disabled || readonly || isValidating}
            onClick={() => {
              if (requestedCamera === cameraName) void mutate();
              else setRequestedCamera(cameraName);
            }}
          >
            {t("ptzProfiles.load", { ns: "views/onvif" })}
          </Button>
          {isValidating && (
            <div role="status" className="flex items-center gap-2">
              <ActivityIndicator className="size-4" />
              <span className="text-sm text-muted-foreground">
                {t("onvif.profileLoading")}
              </span>
            </div>
          )}
          {error && (
            <p role="alert" className="text-sm">
              {t("readFailed", { ns: "views/onvif" })}
            </p>
          )}
          {data && !error && !isValidating && !hasProfiles && (
            <p role="status" className="text-sm text-muted-foreground">
              {t("ptzProfiles.empty", { ns: "views/onvif" })}
            </p>
          )}
        </>
      )}
    </div>
  );
}
