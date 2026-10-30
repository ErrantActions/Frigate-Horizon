export type WeatherSettings = {
  enabled: boolean;
  zip_code?: string | null;
  latitude?: number | null;
  longitude?: number | null;
  refresh_seconds: number;
  stale_after_seconds: number;
};

export type WeatherCondition = "rain" | "snow" | "fog" | "cloud" | "wind";

export type CameraEnvironmentSettings = {
  enabled: boolean;
  timezone: string;
  manual_profile?: string | null;
  solar: {
    enabled: boolean;
    day?: string | null;
    night?: string | null;
    sunrise_profile?: string | null;
    sunrise_start_minutes?: number;
    sunrise_end_minutes?: number;
    sunrise_offset_minutes: number;
    sunset_offset_minutes: number;
  };
  schedules: {
    enabled: boolean;
    id: string;
    profile: string;
    start: string;
    end: string;
    weekdays: number[];
    priority: number;
  }[];
  weather_rules: {
    enabled: boolean;
    id: string;
    profile: string;
    condition: WeatherCondition;
    threshold?: number | null;
    priority: number;
  }[];
  weather_dwell_seconds: number;
  glare: {
    enabled: boolean;
    profile?: string | null;
    zones: string[];
    brightness_threshold: number;
    minimum_bright_fraction: number;
    minimum_fraction_increase: number;
    minimum_luminance_increase: number;
    maximum_frame_gap_seconds: number;
    baseline_seconds: number;
    restore_after_seconds: number;
    cooldown_seconds: number;
    global_change_fraction: number;
    transition_exclusion_minutes: number;
  };
  allow_device_changes: boolean;
};

export type WeatherSnapshot = {
  provider: "open-meteo";
  modeled: true;
  model_time: string;
  fetched_at: string;
  interval_seconds: number;
  temperature_c: number | null;
  rain_mm: number | null;
  showers_mm: number | null;
  snowfall_cm: number | null;
  weather_code: number | null;
  cloud_cover_percent: number | null;
  wind_speed_mps: number | null;
  wind_gusts_mps: number | null;
  is_day: boolean | null;
  visibility_m: number | null;
  visibility_model_time: string | null;
  solar_days: {
    day_start: string;
    sunrise: string | null;
    sunset: string | null;
  }[];
};

export type WeatherStatus = {
  enabled: boolean;
  state:
    "disabled" | "initializing" | "unavailable" | "stale" | "cached" | "fresh";
  data: WeatherSnapshot | null;
  error_code: string | null;
  next_refresh_seconds: number;
  service_running: boolean;
};

export type CameraEnvironmentStatus = {
  camera: string;
  phase: "disabled" | "initializing" | "applied" | "error";
  desired: {
    profiles: string[];
    reason: string;
    base_reason: string;
    schedule_id: string | null;
    weather_rule_id: string | null;
    pending_weather_profile: string | null;
    weather_change_pending: boolean;
    warnings: string[];
  };
  applied_profiles: string[];
  blocked_fields: string[];
  error_code: string | null;
  updated_at: string | null;
  glare: {
    enabled: boolean;
    state:
      | "disabled"
      | "initializing"
      | "paused_camera"
      | "ready"
      | "stale"
      | "unavailable";
    last_frame_time: number | null;
    last_event_time: number | null;
    zones: string[];
    error_code: string | null;
    lighting_transition: boolean | null;
  };
};
