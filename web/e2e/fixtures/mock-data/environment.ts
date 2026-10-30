import type {
  CameraEnvironmentStatus,
  WeatherStatus,
} from "../../../src/types/environment";

export const DISABLED_WEATHER: WeatherStatus = {
  enabled: false,
  state: "disabled",
  data: null,
  error_code: null,
  next_refresh_seconds: 0,
  service_running: true,
};

export const DISABLED_CAMERA_ENVIRONMENT: CameraEnvironmentStatus = {
  camera: "front_door",
  phase: "disabled",
  desired: {
    profiles: [],
    reason: "disabled",
    base_reason: "disabled",
    schedule_id: null,
    weather_rule_id: null,
    pending_weather_profile: null,
    weather_change_pending: false,
    warnings: [],
  },
  applied_profiles: [],
  blocked_fields: [],
  error_code: null,
  updated_at: null,
  glare: {
    enabled: false,
    state: "disabled",
    last_frame_time: null,
    last_event_time: null,
    zones: [],
    error_code: null,
    lighting_transition: null,
  },
};
