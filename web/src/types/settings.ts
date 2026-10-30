/**
 * The Settings page section index.
 *
 * This is the single source for which sections exist, what order they appear
 * in, and which group each one belongs to. It lives outside
 * `pages/Settings.tsx` so that callers which only need to link to a section
 * (the command menu) do not pull every settings view into their bundle.
 * `pages/Settings.tsx` maps these keys onto the components that render them.
 */

export const settingsViewGroups = [
  {
    label: "personalSettings",
    views: ["uiSettings", "notifications"],
  },
  {
    label: "cameraConnections",
    views: [
      "cameraManagement",
      "cameraFfmpeg",
      "cameraLivePlayback",
      "cameraBirdseye",
      "cameraTimestampStyle",
      "cameraMqttConfig",
    ],
  },
  {
    label: "pictureEnvironment",
    views: ["profiles", "globalWeather", "cameraEnvironment", "cameraOnvif"],
  },
  {
    label: "cameraDetection",
    views: [
      "cameraDetect",
      "cameraObjects",
      "cameraMotion",
      "masksAndZones",
      "motionTuner",
      "cameraReview",
      "cameraAudioEvents",
      "cameraAudioTranscription",
      "cameraFaceRecognition",
      "cameraLpr",
    ],
  },
  {
    label: "cameraRecordingGroup",
    views: ["cameraRecording", "cameraSnapshots", "cameraNotifications"],
  },
  {
    label: "enrichments",
    views: [
      "integrationSemanticSearch",
      "integrationGenerativeAi",
      "integrationFaceRecognition",
      "integrationLpr",
      "integrationObjectClassification",
      "triggers",
      "integrationAudioTranscription",
      "frigateplus",
    ],
  },
  {
    label: "accountsSecurity",
    views: [
      "users",
      "roles",
      "systemAuthentication",
      "systemTls",
      "systemProxy",
    ],
  },
  {
    label: "cameraDefaults",
    views: [
      "globalDetect",
      "globalObjects",
      "globalMotion",
      "globalFfmpeg",
      "globalRecording",
      "globalSnapshots",
      "globalReview",
      "globalAudioEvents",
      "globalLivePlayback",
      "globalTimestampStyle",
    ],
  },
  {
    label: "advancedSystem",
    views: [
      "plugins",
      "systemGo2rtcStreams",
      "systemDetectorsAndModel",
      "systemDatabase",
      "systemMqtt",
      "systemBirdseye",
      "systemNetworking",
      "systemUi",
      "systemLogging",
      "systemEnvironmentVariables",
      "systemTelemetry",
      "mediaSync",
      "recordingAudioRepair",
      "recordingCompression",
      "regionGrid",
    ],
  },
] as const;

/** `enrichments` and `debug` are reachable in the UI but have no menu entry. */
export type SettingsType =
  | (typeof settingsViewGroups)[number]["views"][number]
  | "enrichments"
  | "debug";

export const allSettingsViews: SettingsType[] = [
  ...settingsViewGroups.flatMap((group) => group.views),
  "enrichments",
  "debug",
];

const CAMERA_TOOL_VIEWS = new Set<SettingsType>([
  "masksAndZones",
  "motionTuner",
  "triggers",
  "regionGrid",
  "debug",
]);

export function isCameraSettingsView(view: SettingsType): boolean {
  return (
    (view.startsWith("camera") && view !== "cameraManagement") ||
    CAMERA_TOOL_VIEWS.has(view)
  );
}

export const cameraSettingsGroups = settingsViewGroups
  .map((group) => ({
    label: group.label,
    views: group.views.filter(isCameraSettingsView),
  }))
  .filter((group) => group.views.length > 0);

/** Sections a viewer may open. Everything else is admin only. */
export const ALLOWED_VIEWS_FOR_VIEWER: SettingsType[] = [
  "uiSettings",
  "notifications",
];
