export const PLAYBACK_BUFFER_BYTES = 20_000_000;
export const PLAYBACK_BACK_BUFFER_SECONDS = 15;

export function playbackBufferTarget({
  stableSeconds,
  playbackRate,
  bitrate,
  saveData,
  preferredLength,
}: {
  stableSeconds: number;
  playbackRate: number;
  bitrate?: number;
  saveData: boolean;
  preferredLength?: number;
}): number {
  const rate =
    Number.isFinite(playbackRate) && playbackRate > 0 ? playbackRate : 1;
  const horizon = stableSeconds >= 10 ? (preferredLength ?? 30) : 6;
  const seconds = Math.min(
    60,
    (saveData ? Math.min(horizon, 10) : horizon) * rate,
  );
  const byteBudget =
    bitrate != null && Number.isFinite(bitrate) && bitrate > 0
      ? (PLAYBACK_BUFFER_BYTES * 8) / bitrate
      : 60;
  return Math.min(Math.max(1, seconds), byteBudget);
}

export type PlaybackMeasurements = {
  sourceLoads: number;
  firstFrameMs: number | null;
  lastSeekMs: number | null;
  seeks: number;
  stalls: number;
  stalledMs: number;
};

export class PlaybackPerformance {
  private sourceStarted: number | undefined;
  private seekStartedAt: number | undefined;
  private seekTarget: number | undefined;
  private stallStartedAt: number | undefined;
  private suspendedAt: number | undefined;
  private measurements: PlaybackMeasurements = {
    sourceLoads: 0,
    firstFrameMs: null,
    lastSeekMs: null,
    seeks: 0,
    stalls: 0,
    stalledMs: 0,
  };

  constructor(private now: () => number = () => performance.now()) {}

  sourceChanged() {
    this.stallEnded();
    this.sourceStarted = this.now();
    if (this.suspendedAt !== undefined) this.suspendedAt = this.now();
    this.seekStartedAt = undefined;
    this.seekTarget = undefined;
    this.measurements.sourceLoads += 1;
    this.measurements.firstFrameMs = null;
    this.measurements.lastSeekMs = null;
  }

  seekStarted(target: number) {
    this.stallEnded();
    if (this.measurements.firstFrameMs === null) return;
    this.seekStartedAt = this.now();
    this.seekTarget = target;
    this.measurements.seeks += 1;
  }

  framePresented(mediaTime: number) {
    if (this.suspendedAt !== undefined) return;
    if (
      this.sourceStarted !== undefined &&
      this.measurements.firstFrameMs === null
    ) {
      this.measurements.firstFrameMs = Math.max(
        0,
        this.now() - this.sourceStarted,
      );
    }
    if (
      this.seekStartedAt !== undefined &&
      this.seekTarget !== undefined &&
      Math.abs(mediaTime - this.seekTarget) <= 0.25
    ) {
      this.measurements.lastSeekMs = Math.max(
        0,
        this.now() - this.seekStartedAt,
      );
      this.seekStartedAt = undefined;
    }
    this.stallEnded();
  }

  stallStarted() {
    if (
      this.stallStartedAt !== undefined ||
      this.suspendedAt !== undefined ||
      this.measurements.firstFrameMs === null ||
      this.seekStartedAt !== undefined
    )
      return;
    this.stallStartedAt = this.now();
    this.measurements.stalls += 1;
  }

  stallEnded() {
    if (this.stallStartedAt === undefined) return;
    this.measurements.stalledMs += Math.max(
      0,
      this.now() - this.stallStartedAt,
    );
    this.stallStartedAt = undefined;
  }

  suspend() {
    this.stallEnded();
    this.suspendedAt ??= this.now();
  }

  resume() {
    if (this.suspendedAt === undefined) return;
    const elapsed = this.now() - this.suspendedAt;
    if (this.sourceStarted !== undefined) this.sourceStarted += elapsed;
    if (this.seekStartedAt !== undefined) this.seekStartedAt += elapsed;
    this.suspendedAt = undefined;
  }

  snapshot(): PlaybackMeasurements {
    return {
      ...this.measurements,
      stalledMs:
        this.measurements.stalledMs +
        (this.stallStartedAt === undefined
          ? 0
          : Math.max(0, this.now() - this.stallStartedAt)),
    };
  }
}
