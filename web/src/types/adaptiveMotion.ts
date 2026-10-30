export const adaptiveMotionFields = [
  "adjustment",
  "activity",
  "effective",
  "p",
  "i",
  "d",
] as const;

export type AdaptiveMotionField = (typeof adaptiveMotionFields)[number];

export type AdaptiveMotionCell = {
  column: number;
  row: number;
  adjustment: number;
  applied_adjustment: number;
  effective_threshold: number;
  activity: number;
  episode_count: number;
  p: number;
  i: number;
  d: number;
  hold_remaining_seconds: number;
  quiet_seconds: number;
  decay_rate: number;
};

export type AdaptiveMotionSnapshot = {
  camera: string;
  mode: "disabled" | "shadow" | "active";
  status: "disabled" | "warming_up" | "learning" | "paused" | "stale" | "error";
  updated_at: number | null;
  baseline_threshold: number;
  max_adjustment: number;
  applied: boolean;
  positive_holds: number;
  warning: string | null;
  error: string | null;
  profile: {
    window_index: number;
    start_minute: number;
    end_minute: number;
    timezone: string;
    gust_band_mph: number | null;
    wind_direction_sector: number | null;
    weather_state: string;
    weather_observed_at: number | null;
  } | null;
  grid: {
    columns: number;
    rows: number;
    analysis_width: number;
    analysis_height: number;
    display_columns: number;
    display_rows: number;
    // Row-major area-averaged overview values, not exact native-cell samples.
    values: number[];
    field: AdaptiveMotionField;
    minimum?: number;
    maximum?: number;
  };
  cell?: AdaptiveMotionCell;
};
