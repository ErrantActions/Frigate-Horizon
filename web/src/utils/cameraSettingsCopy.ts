import cloneDeep from "lodash/cloneDeep";
import get from "lodash/get";
import set from "lodash/set";
import unset from "lodash/unset";
import type { RJSFSchema } from "@rjsf/utils";
import type { FrigateConfig } from "@/types/frigateConfig";
import type { ConfigSectionData, JsonObject } from "@/types/configForm";
import type { SaveAllPreviewItem } from "@/components/overlay/detail/SaveAllPreviewPopover";
import { isJsonObject } from "@/lib/utils";
import { extractSectionSchema } from "@/hooks/use-config-schema";
import {
  flattenOverrides,
  getBaseCameraSectionValue,
  getEffectiveHiddenFields,
  mergeProfileOverrides,
  prepareSectionSavePayload,
  sanitizeSectionData,
} from "@/utils/configUtil";

export const CAMERA_COPY_CATEGORIES = {
  motion: ["motion"],
  detection: ["detect", "objects"],
  recording: ["record", "snapshots"],
  review: ["review"],
  audio: ["audio", "audio_transcription"],
  notifications: ["notifications"],
  display: ["live", "timestamp_style", "birdseye"],
  enrichments: ["face_recognition", "lpr"],
} as const;

export type CameraCopyCategory = keyof typeof CAMERA_COPY_CATEGORIES;

export type CameraSettingsCopyPlan = {
  configData: JsonObject;
  updateTopics: string[];
  replacePaths: string[];
  needsRestart: boolean;
  preview: SaveAllPreviewItem[];
  issues: { camera: string; section: string }[];
};

const CAMERA_LOCAL_FIELDS = new Set([
  "mask",
  "raw_mask",
  "coordinates",
  "calibration",
  "required_zones",
  "headers",
  "authorization",
  "user",
  "path",
  "paths",
  "streams",
]);
const CONNECTION_FIELD =
  /(?:password|secret|token|api_?key|credentials|username|uri|url|address|host)$/i;

function preserveCameraLocalFields(draft: JsonObject, target: JsonObject) {
  for (const key of Object.keys(draft)) {
    if (CAMERA_LOCAL_FIELDS.has(key) || CONNECTION_FIELD.test(key)) {
      if (target[key] === undefined) {
        delete draft[key];
      } else {
        draft[key] = cloneDeep(target[key]);
      }
    } else {
      const value = draft[key];
      if (isJsonObject(value)) {
        const targetValue = target[key];
        preserveCameraLocalFields(
          value,
          isJsonObject(targetValue) ? targetValue : {},
        );
      }
    }
  }
}

export function buildCameraSettingsCopyPlan({
  config,
  schema,
  sourceCamera,
  targets,
  categories,
}: {
  config: FrigateConfig;
  schema: RJSFSchema;
  sourceCamera: string;
  targets: string[];
  categories: CameraCopyCategory[];
}): CameraSettingsCopyPlan {
  const plan: CameraSettingsCopyPlan = {
    configData: {},
    updateTopics: [],
    replacePaths: [],
    needsRestart: false,
    preview: [],
    issues: [],
  };
  const sections = [
    ...new Set(
      categories.flatMap<string>((key) => [...CAMERA_COPY_CATEGORIES[key]]),
    ),
  ];
  for (const camera of [...new Set(targets)]) {
    if (
      camera === sourceCamera ||
      !config.cameras[camera] ||
      !config.cameras[sourceCamera]
    ) {
      plan.issues.push({ camera, section: "" });
      continue;
    }
    for (const section of sections) {
      const source = getBaseCameraSectionValue(config, sourceCamera, section);
      const target = getBaseCameraSectionValue(config, camera, section);
      if (
        !extractSectionSchema(schema, section, "camera") ||
        !isJsonObject(source) ||
        !isJsonObject(target)
      ) {
        plan.issues.push({ camera, section });
        continue;
      }
      const editable = (data: ConfigSectionData, name: string) =>
        sanitizeSectionData(
          data,
          getEffectiveHiddenFields(section, "camera", {
            fullConfig: config,
            fullCameraConfig: config.cameras[name],
            level: "camera",
            cameraName: name,
            formData: data,
          }),
        );
      const targetData = editable(target, camera);
      const draft = mergeProfileOverrides(
        targetData,
        editable(source, sourceCamera),
      );
      preserveCameraLocalFields(draft, targetData);
      if (section === "detect") {
        for (const field of ["width", "height"]) {
          if (targetData[field] === undefined) {
            unset(draft, field);
          } else {
            draft[field] = targetData[field];
          }
        }
      }
      const payload = prepareSectionSavePayload({
        pendingDataKey: `${camera}::${section}`,
        pendingData: draft,
        config,
        fullSchema: schema,
      });
      if (!payload) continue;
      if (!payload.updateTopic?.startsWith(`config/cameras/${camera}/`)) {
        plan.issues.push({ camera, section });
        continue;
      }
      set(plan.configData, payload.basePath, payload.sanitizedOverrides);
      plan.updateTopics.push(payload.updateTopic);
      plan.replacePaths.push(...(payload.replacePaths ?? []));
      plan.needsRestart ||= payload.needsRestart;
      for (const change of flattenOverrides(payload.sanitizedOverrides)) {
        plan.preview.push({
          scope: "camera",
          cameraName: camera,
          fieldPath: `${section}${change.path ? `.${change.path}` : ""}`,
          value: change.value === "" ? "" : get(draft, change.path),
        });
      }
    }
  }
  plan.updateTopics = [...new Set(plan.updateTopics)];
  plan.replacePaths = [...new Set(plan.replacePaths)];
  return plan;
}
