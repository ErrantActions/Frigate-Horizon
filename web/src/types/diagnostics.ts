export type DiagnosticFinding = {
  code: string;
  severity: "info" | "warning" | "error";
  confidence: "observed" | "possible";
  camera: string | null;
  evidence: Record<string, number>;
};

export type DiagnosticsReport = {
  sampled_at: number;
  stats_time: number | null;
  age_seconds: number | null;
  stale: boolean;
  findings: DiagnosticFinding[];
  cameras: {
    camera: string;
    capture_fps: number | null;
    process_fps: number | null;
    skipped_fps: number | null;
    skipped_percent: number | null;
    detection_fps: number | null;
    tracker_fps: number | null;
    output_dropped_fps: number | null;
    source_height: number | null;
    analysis_height: number | null;
    detection_enabled: boolean;
  }[];
  gpus: {
    gpu: string;
    decoder_percent: number | null;
    compute_percent: number | null;
    temperature: number | null;
  }[];
  detector_inference_ms: number[];
  total_cameras: number;
  truncated: boolean;
  ai_available: boolean;
};

export type DiagnosticsExplanation = {
  text: string;
  based_on_stats_time: number;
};
