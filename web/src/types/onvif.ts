export type DeviceValue = number | string;
export type OnvifProfile = {
  token: string;
  name: string;
  encoder_token?: string | null;
  video_source_token?: string | null;
};
export type NumberRange = { minimum: number; maximum: number };
export type ImageControl = {
  key: string;
  value: DeviceValue | null;
  limits: NumberRange | null;
  choices: string[];
  available: boolean;
  limits_source?: "camera" | "onvif_standard";
};
export type ImageResponse = {
  profiles: OnvifProfile[];
  profile_token: string | null;
  state: "ready" | "choose_profile" | "read_only";
  imaging: {
    video_source_token: string;
    controls: ImageControl[];
  } | null;
};
export type EncoderState = {
  token: string;
  encoding: string;
  revision: string;
  editable: boolean;
  service: "media" | "media2";
  persistence_required: boolean;
  codec_profile?: string | null;
  codec_options?: Record<string, EncoderState["options"]>;
  codec_frame_rates?: Record<string, number[]>;
  codec_profiles?: Record<string, string[]>;
  frame_rates: number[];
  shared_profiles: string[];
  resolution: { width: number; height: number } | null;
  frame_rate: number | null;
  bitrate_kbps: number | null;
  quality: number | null;
  gop_length: number | null;
  encoding_interval: number | null;
  options: {
    resolutions: { width: number; height: number }[];
    frame_rate: NumberRange | null;
    bitrate_kbps: NumberRange | null;
    quality: NumberRange | null;
    gop_length: NumberRange | null;
    encoding_interval: NumberRange | null;
  };
};
export type AudioEncoderResponse = {
  profiles: {
    token: string;
    name: string;
    audio_source_token: string | null;
    encoder_token: string | null;
  }[];
  profile_token: string | null;
  state: "ready" | "choose_profile" | "unsupported" | "read_only";
  encoder: {
    token: string;
    audio_source_token: string;
    encoding: string;
    bitrate_kbps: number | null;
    sample_rate_khz: number | null;
    options: {
      encoding: string;
      bitrates_kbps: number[];
      sample_rates_khz: number[];
    }[];
    shared_profiles: string[];
    editable: boolean;
    revision: string;
    persistence_required?: boolean;
  } | null;
};
export type FocusResponse = {
  profiles: OnvifProfile[];
  profile_token: string | null;
  state: "ready" | "choose_profile" | "unsupported" | "read_only";
  focus: {
    video_source_token: string;
    position: number | null;
    position_range: NumberRange;
    speed_range: NumberRange | null;
    autofocus_mode: string | null;
    editable: boolean;
    revision: string;
  } | null;
};
export type MediaCatalog = {
  state: "ready" | "unsupported";
  service: "media" | "media2";
  profiles: {
    token: string;
    name: string;
    video_source_token: string | null;
    audio_source_token: string | null;
    video_encoder_token: string | null;
    audio_encoder_token: string | null;
    ptz_configuration_token: string | null;
  }[];
};
export type EncoderResponse = {
  profiles: OnvifProfile[];
  profile_token: string | null;
  encoder: EncoderState | null;
};
export type DeviceStatus = {
  connected: boolean;
  services: Record<string, boolean>;
  noise_reduction: string;
};
export type OwnershipStatus = {
  phase: string;
  blocked: boolean;
  manual_remaining_seconds: number;
  owned_fields: string[];
  profile_token: string | null;
};
export type EventStatus = {
  state: string;
  failures: number;
  events: { topic: string; received_at: number }[];
  integration?: "status_history_only" | "manual_events";
  signals?: {
    active: number;
    starts: number;
    ends: number;
    expiry_seconds: number;
    error_code: string | null;
  } | null;
};
export type PresetResponse = {
  profiles: OnvifProfile[];
  presets: { token: string; name: string }[];
};
export type RecordingResponse = {
  recordings: { token: string; start: string; end: string }[];
  search_state: string;
  limit: number;
};
export type ReplaySession = {
  id: string;
  state: "ready" | "starting" | "streaming" | "ended" | "expired";
  start: string;
  end: string;
  expires_in: number;
  max_duration_seconds: number;
  audio: boolean | null;
  audio_status:
    "pending" | "available" | "not_recorded" | "unavailable" | "not_present";
  audio_codec?: "aac_lc" | null;
  audio_sample_rate?: number | null;
};
