import type { SectionConfigOverrides } from "./types";

const environment: SectionConfigOverrides = {
  base: {
    restartRequired: [],
    overrideFields: [],
    fieldOrder: [
      "enabled",
      "manual_profile",
      "timezone",
      "solar",
      "schedules",
      "weather_rules",
      "weather_dwell_seconds",
      "glare",
      "allow_device_changes",
      "image_profiles",
    ],
    advancedFields: [
      "weather_dwell_seconds",
      "allow_device_changes",
      "image_profiles",
    ],
    uiSchema: {
      timezone: {
        "ui:widget": "timezoneSelect",
        "ui:options": { allowBrowserTimezone: false },
      },
      manual_profile: {
        "ui:widget": "cameraProfile",
        "ui:options": { allowAutomatic: true },
      },
      solar: {
        day: { "ui:widget": "cameraProfile" },
        night: { "ui:widget": "cameraProfile" },
        sunrise_profile: { "ui:widget": "cameraProfile" },
        sunrise_start_minutes: { "ui:options": { signed: true } },
        sunrise_end_minutes: { "ui:options": { signed: true } },
      },
      schedules: {
        items: { profile: { "ui:widget": "cameraProfile" } },
      },
      weather_rules: {
        items: { profile: { "ui:widget": "cameraProfile" } },
      },
      glare: {
        profile: { "ui:widget": "cameraProfile" },
        zones: { "ui:widget": "zoneNames" },
      },
    },
  },
};

export default environment;
