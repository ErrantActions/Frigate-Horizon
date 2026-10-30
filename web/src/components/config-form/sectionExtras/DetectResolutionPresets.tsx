import { useId, useState } from "react";
import { useTranslation } from "react-i18next";
import type { SectionRendererProps } from "./registry";
import DynamicDetectionResolutionStatus from "./DynamicDetectionResolutionStatus";

const presets = [
  { id: "p360", width: 640, height: 360 },
  { id: "p480", width: 640, height: 480 },
  { id: "p720", width: 1280, height: 720 },
  { id: "p1080", width: 1920, height: 1080 },
  { id: "p1440", width: 2560, height: 1440 },
  { id: "p2160", width: 3840, height: 2160 },
] as const;

export default function DetectResolutionPresets({
  formContext,
  selectedCamera,
}: SectionRendererProps) {
  const { t } = useTranslation("views/settings");
  const id = useId();
  const [customScope, setCustomScope] = useState<string>();
  const data = formContext?.formData;
  const update = formContext?.onFormDataChange;
  if (!data || !update) return null;
  const scope =
    selectedCamera ?? formContext?.cameraName ?? formContext?.level ?? "global";
  const selected =
    customScope === scope
      ? "custom"
      : data.width == null && data.height == null
        ? "source"
        : (presets.find(
            (preset) =>
              preset.width === data.width && preset.height === data.height,
          )?.id ?? "custom");

  return (
    <div
      className="mb-4 space-y-2 rounded-lg border p-3"
      data-testid="detect-resolution-presets"
    >
      <label htmlFor={id} className="block text-sm font-medium">
        {t("detectResolution.title")}
      </label>
      <select
        id={id}
        className="w-full max-w-sm rounded-md border bg-background p-2 text-sm"
        value={selected}
        onChange={(event) => {
          const value = event.target.value;
          if (value === "custom") {
            setCustomScope(scope);
            return;
          }
          setCustomScope(undefined);
          if (value === "source") {
            update({ ...data, width: null, height: null });
            return;
          }
          const preset = presets.find((item) => item.id === value);
          if (preset)
            update({ ...data, width: preset.width, height: preset.height });
        }}
      >
        <option value="source">{t("detectResolution.source")}</option>
        {presets.map((preset) => (
          <option key={preset.id} value={preset.id}>
            {t(`detectResolution.presets.${preset.id}`)}
          </option>
        ))}
        <option value="custom">{t("detectResolution.custom")}</option>
      </select>
      <p className="text-sm text-muted-foreground">
        {t("detectResolution.help")}
      </p>
      <p className="text-xs text-muted-foreground">
        {t("detectResolution.sourceHelp")}
      </p>
      {selected === "p2160" && (
        <p role="status" className="text-sm text-warning">
          {t("detectResolution.highResolution")}
        </p>
      )}
      {selectedCamera && (
        <DynamicDetectionResolutionStatus
          key={selectedCamera}
          camera={selectedCamera}
          config={formContext?.fullCameraConfig}
        />
      )}
    </div>
  );
}
