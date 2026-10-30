import type { SectionConfigOverrides } from "./types";

const weather: SectionConfigOverrides = {
  base: {
    restartRequired: [],
    overrideFields: [],
    fieldOrder: [
      "enabled",
      "zip_code",
      "latitude",
      "longitude",
      "refresh_seconds",
      "stale_after_seconds",
    ],
    advancedFields: [
      "latitude",
      "longitude",
      "refresh_seconds",
      "stale_after_seconds",
    ],
    uiSchema: {
      zip_code: { "ui:widget": "text", "ui:placeholder": "12345" },
      latitude: { "ui:widget": "updown" },
      longitude: { "ui:widget": "updown" },
    },
  },
};

export default weather;
