/**
 * Policy engine for auto live stream selection.
 *
 * Rungs index the camera's live streams in config order, followed by
 * the jsmpeg floor. Evidence is media delivery falling behind wall
 * time while bytes still arrive, which separates congestion from a
 * dead camera or a go2rtc restart. No browser API reports spare
 * bandwidth, so upswitching is try-and-see with backoff.
 *
 * Time comes from an injected clock and the owner calls tick() once a
 * second, so the class holds no timers.
 */

import type {
  LiveAutoReason,
  LiveHealthSample,
  LivePlayerError,
} from "@/types/live";

// go2rtc bursts its GOP cache when a consumer attaches
const WARMUP_MS = 3000;
// media per wall second below this is a low sample. Low samples reset
// the upswitch clock and feed the chronic window
const LOW_RATE = 0.85;
// net shortfall within the window that downswitches on sustained
// under-delivery. Keyframes make per-second delivery bursty, so the
// window lets a fast second offset a slow one
const SUSTAINED_DEFICIT_S = 2;
const SUSTAINED_WINDOW_MS = 10000;
// low-sample deficit within the window that downswitches on repeated
// short stalls
const CHRONIC_DEFICIT_S = 6;
const CHRONIC_WINDOW_MS = 60000;
// clean time before trying one rung up, doubled after each failed try
const UPSWITCH_WAIT_MS = 60000;
const STARTUP_UPSWITCH_WAIT_MS = 3000;
const UPSWITCH_WAIT_MAX_MS = 600000;
// a downswitch this soon after an upswitch counts as a failed try
const PROBATION_MS = 30000;

export type LiveGovernorSnapshot = {
  rung: number;
  reason: LiveAutoReason | undefined;
};

type Shortfall = {
  ts: number;
  seconds: number;
};

const total = (entries: Shortfall[]) =>
  entries.reduce((sum, entry) => sum + entry.seconds, 0);

export class LiveStreamGovernor {
  private readonly floor: number;
  private readonly now: () => number;
  private readonly listeners = new Set<() => void>();
  private readonly unplayable = new Set<number>();

  private snapshot: LiveGovernorSnapshot = { rung: 0, reason: undefined };
  private holdLow = false;
  private minimumRung = 0;
  private decodePressure: Shortfall[] = [];
  private downReason: "bandwidth" | "decoder" = "bandwidth";
  private latestSampleTs: number | undefined;

  private rungStartTs: number;
  private cleanSinceTs: number;
  // every sample, signed, for the sustained window
  private shortfalls: Shortfall[] = [];
  // low samples only, for the chronic window
  private deficits: Shortfall[] = [];
  private upswitchWaitMs = UPSWITCH_WAIT_MS;
  private probationUntil = 0;
  private startupProbing = false;

  /** rungCount includes the jsmpeg floor. */
  constructor(
    rungCount: number,
    now: () => number = Date.now,
    private readonly includeFloor = true,
    startLow = false,
  ) {
    this.floor = rungCount - 1;
    this.now = now;
    this.rungStartTs = now();
    this.cleanSinceTs = this.rungStartTs;
    if (startLow) {
      this.snapshot = { rung: this.lowestPlayable(), reason: undefined };
      this.startupProbing = this.snapshot.rung > 0;
      if (this.startupProbing) {
        this.upswitchWaitMs = STARTUP_UPSWITCH_WAIT_MS;
      }
    }
  }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  getSnapshot = (): LiveGovernorSnapshot => this.snapshot;

  /**
   * One health sample from the playing transport. Zero-byte samples
   * belong to the stall watchdog, and warmup samples only prove that
   * bytes are arriving.
   */
  sample({
    bytes,
    mediaSeconds,
    wallSeconds,
    totalFrames,
    droppedFrames,
  }: LiveHealthSample) {
    if (bytes <= 0 || wallSeconds <= 0) {
      return;
    }

    const now = this.now();

    if (now - this.rungStartTs < WARMUP_MS) {
      return;
    }
    if (this.latestSampleTs === undefined || now - this.latestSampleTs > 3000) {
      this.cleanSinceTs = now;
    }
    this.latestSampleTs = now;

    this.decodePressure = this.decodePressure.filter(
      (entry) => entry.ts > now - SUSTAINED_WINDOW_MS,
    );
    if (
      totalFrames !== undefined &&
      droppedFrames !== undefined &&
      Number.isSafeInteger(totalFrames) &&
      Number.isSafeInteger(droppedFrames) &&
      totalFrames >= 20 &&
      droppedFrames >= 0 &&
      droppedFrames <= totalFrames &&
      droppedFrames / totalFrames > 0.1
    ) {
      this.cleanSinceTs = now;
      this.decodePressure.push({ ts: now, seconds: wallSeconds });
      if (total(this.decodePressure) >= 3) {
        this.stepDown("decoder");
        return;
      }
    }

    // WebRTC reports a finished freeze in one sample, so media can be
    // negative and one sample can carry more shortfall than its wall time
    const shortfall = { ts: now, seconds: wallSeconds - mediaSeconds };
    this.shortfalls.push(shortfall);
    if (mediaSeconds / wallSeconds < LOW_RATE) {
      this.cleanSinceTs = now;
      this.deficits.push(shortfall);
    }

    this.shortfalls = this.shortfalls.filter(
      (entry) => entry.ts > now - SUSTAINED_WINDOW_MS,
    );
    this.deficits = this.deficits.filter(
      (entry) => entry.ts > now - CHRONIC_WINDOW_MS,
    );

    if (
      total(this.shortfalls) >= SUSTAINED_DEFICIT_S ||
      total(this.deficits) >= CHRONIC_DEFICIT_S
    ) {
      this.stepDown("bandwidth");
    }
  }

  /**
   * A player error. Returns true when auto handled it by stepping down,
   * false to hand it to the error fallback.
   */
  playerError(error: LivePlayerError, sourceOnline: boolean): boolean {
    const { rung } = this.snapshot;
    if (rung === this.floor) {
      return false;
    }

    // go2rtc's codec reply is proof, so the rung is not retried this session
    if (error === "mse-codec") {
      this.unplayable.add(rung);
      return this.stepDown();
    }

    // the rung below played a moment ago, so a failed upswitch returns to it
    if (this.now() < this.probationUntil) {
      return this.stepDown();
    }

    // an offline camera fails every stream alike; a stall on a live camera
    // is the viewer's link, even when congestion stops delivery outright
    if (!sourceOnline) {
      return false;
    }

    return this.stepDown();
  }

  /** Called once a second while auto is active and the page is visible. */
  tick() {
    const now = this.now();

    // the higher rung survived probation, so backoff starts over
    if (this.probationUntil > 0 && now >= this.probationUntil) {
      this.probationUntil = 0;
      this.upswitchWaitMs = UPSWITCH_WAIT_MS;
    }

    if (now - this.cleanSinceTs < this.upswitchWaitMs) {
      return;
    }
    if (this.latestSampleTs === undefined || now - this.latestSampleTs > 3000) {
      return;
    }

    // data saver may climb out of the floor, but never past its stream
    const target = this.nextPlayableAbove(this.snapshot.rung);
    if (
      target === undefined ||
      (this.holdLow && target < this.lowestPlayable())
    ) {
      return;
    }

    this.probationUntil = now + PROBATION_MS;
    this.moveTo(target);
    if (target === this.minimumRung) {
      this.startupProbing = false;
      this.upswitchWaitMs = UPSWITCH_WAIT_MS;
    }
  }

  /**
   * Data saver holds the lowest playable stream and never upswitches past
   * it, whatever the connection could carry.
   */
  setHoldLow(hold: boolean) {
    this.holdLow = hold;
    this.moveTo(
      hold
        ? Math.max(this.snapshot.rung, this.lowestPlayable())
        : this.snapshot.rung,
    );
  }

  /** Do not spend pixels above the displayed tile's resolution. */
  setMinimumRung(value: number) {
    const previous = this.minimumRung;
    this.minimumRung = Math.max(
      0,
      Math.min(value, this.floor - (this.includeFloor ? 1 : 0)),
    );
    if (this.holdLow) {
      this.moveTo(this.lowestPlayable());
      return;
    }
    if (
      this.snapshot.rung < this.minimumRung ||
      (this.minimumRung < previous &&
        this.snapshot.rung === previous &&
        !this.startupProbing)
    ) {
      this.moveTo(this.minimumRung);
    }
  }

  /** A remount or unpause on the same rung starts with clean evidence. */
  resetHistory() {
    const now = this.now();
    this.rungStartTs = now;
    this.cleanSinceTs = now;
    this.shortfalls = [];
    this.deficits = [];
    this.decodePressure = [];
    this.latestSampleTs = undefined;
  }

  /** Returns to the top rung and clears every session fact. */
  reset() {
    this.unplayable.clear();
    this.startupProbing = false;
    this.upswitchWaitMs = UPSWITCH_WAIT_MS;
    this.probationUntil = 0;
    this.moveTo(this.holdLow ? this.lowestPlayable() : this.minimumRung);
  }

  private stepDown(reason: "bandwidth" | "decoder" = "bandwidth"): boolean {
    const target = this.nextPlayableBelow(this.snapshot.rung);
    if (target === undefined) {
      return false;
    }

    this.startupProbing = false;
    this.upswitchWaitMs = Math.max(this.upswitchWaitMs, UPSWITCH_WAIT_MS);
    if (this.now() < this.probationUntil) {
      this.upswitchWaitMs = Math.min(
        this.upswitchWaitMs * 2,
        UPSWITCH_WAIT_MAX_MS,
      );
    }
    this.probationUntil = 0;
    this.downReason = reason;

    this.moveTo(target);
    return true;
  }

  private moveTo(rung: number) {
    this.resetHistory();

    const reason = this.reasonAt(rung);
    if (rung === this.snapshot.rung && reason === this.snapshot.reason) {
      return;
    }

    this.snapshot = { rung, reason };
    this.listeners.forEach((listener) => listener());
  }

  private reasonAt(rung: number): LiveAutoReason | undefined {
    if (this.includeFloor && rung === this.floor) {
      return "floor";
    }

    if (this.nextPlayableAbove(rung) === undefined) {
      return [...this.unplayable].some((unplayable) => unplayable < rung)
        ? "codec"
        : this.minimumRung > 0
          ? "viewport"
          : undefined;
    }

    return this.holdLow
      ? "saveData"
      : this.startupProbing
        ? undefined
        : this.downReason;
  }

  private nextPlayableAbove(rung: number): number | undefined {
    for (let candidate = rung - 1; candidate >= this.minimumRung; candidate--) {
      if (!this.unplayable.has(candidate)) {
        return candidate;
      }
    }
    return undefined;
  }

  // the floor is always playable
  private nextPlayableBelow(rung: number): number | undefined {
    for (let candidate = rung + 1; candidate <= this.floor; candidate++) {
      if (
        (this.includeFloor && candidate === this.floor) ||
        !this.unplayable.has(candidate)
      ) {
        return candidate;
      }
    }
    return undefined;
  }

  private lowestPlayable(): number {
    for (
      let candidate = this.floor - (this.includeFloor ? 1 : 0);
      candidate >= this.minimumRung;
      candidate--
    ) {
      if (!this.unplayable.has(candidate)) {
        return candidate;
      }
    }
    return this.floor;
  }
}
