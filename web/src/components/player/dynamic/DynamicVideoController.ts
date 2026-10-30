import { Recording } from "@/types/record";
import { DynamicPlayback } from "@/types/playback";
import { PreviewController } from "../PreviewController";
import { TimeRange, TrackingDetailsSequence } from "@/types/timeline";
import {
  calculateInpointOffset,
  calculateSeekPosition,
  calculateRecordingTimestamp,
} from "@/utils/videoUtil";
import { playWithTemporaryMuteFallback } from "@/utils/videoUtil.ts";

type PlayerMode = "playback" | "scrubbing";

// how long a seek may wait for its `seeked` event before playback starts
// anyway; long enough that a normally completing seek always wins
const SEEK_PLAY_FALLBACK_MS = 1000;

type PlaybackElement = Pick<
  HTMLVideoElement,
  | "currentTime"
  | "src"
  | "isConnected"
  | "muted"
  | "play"
  | "pause"
  | "paused"
  | "ended"
  | "buffered"
  | "readyState"
  | "error"
  | "addEventListener"
  | "removeEventListener"
>;

export class DynamicVideoController {
  // main state
  public camera = "";
  private playerController: PlaybackElement;
  private previewController: Pick<
    PreviewController,
    "scrubToTimestamp" | "setNewPreviewStartTime"
  >;
  private setNoRecording: (noRecs: boolean) => void;
  private setFocusedItem: (timeline: TrackingDetailsSequence) => void;
  private playerMode: PlayerMode = "playback";

  // playback
  private recordings: Recording[] = [];
  private timeRange: TimeRange = { after: 0, before: 0 };
  private inpointOffset: number = 0;
  private annotationOffset: number;
  private playGeneration = 0;
  private playIntent = true;
  private cancelPendingSeek: (() => void) | undefined;
  private onPlaybackError: (error: unknown) => void;

  constructor(
    camera: string,
    playerController: PlaybackElement,
    previewController: Pick<
      PreviewController,
      "scrubToTimestamp" | "setNewPreviewStartTime"
    >,
    annotationOffset: number,
    defaultMode: PlayerMode,
    setNoRecording: (noRecs: boolean) => void,
    setFocusedItem: (timeline: TrackingDetailsSequence) => void,
    onPlaybackError: (error: unknown) => void,
  ) {
    this.camera = camera;
    this.playerController = playerController;
    this.previewController = previewController;
    this.annotationOffset = annotationOffset;
    this.playerMode = defaultMode;
    this.setNoRecording = setNoRecording;
    this.setFocusedItem = setFocusedItem;
    this.onPlaybackError = onPlaybackError;
  }

  private cancelPlayIntent() {
    this.playGeneration += 1;
    this.cancelPendingSeek?.();
    this.cancelPendingSeek = undefined;
  }

  dispose() {
    this.cancelPlayIntent();
  }

  private startPlayback() {
    const generation = this.playGeneration;
    const source = this.playerController.src;
    const cancelled = () =>
      generation !== this.playGeneration ||
      source !== this.playerController.src ||
      !this.playerController.isConnected;
    void playWithTemporaryMuteFallback(this.playerController, cancelled).catch(
      (error: unknown) => {
        if (!cancelled()) this.onPlaybackError(error);
      },
    );
  }

  newPlayback(newPlayback: DynamicPlayback) {
    if (
      newPlayback.timeRange.after !== this.timeRange.after ||
      newPlayback.timeRange.before !== this.timeRange.before ||
      newPlayback.recordings.length !== this.recordings.length ||
      newPlayback.recordings.some((segment, index) => {
        const current = this.recordings[index];
        return (
          segment.start_time !== current.start_time ||
          segment.end_time !== current.end_time ||
          segment.duration !== current.duration ||
          segment.lead_in !== current.lead_in
        );
      })
    )
      this.cancelPlayIntent();
    this.recordings = newPlayback.recordings;
    this.timeRange = newPlayback.timeRange;
    this.inpointOffset = calculateInpointOffset(
      this.timeRange.after,
      this.recordings[0],
    );
  }

  play() {
    this.playIntent = true;
    this.cancelPlayIntent();
    this.startPlayback();
  }

  pause() {
    this.playIntent = false;
    this.suspend();
  }

  suspend() {
    this.cancelPlayIntent();
    this.playerController.pause();
  }

  wantsPlayback(): boolean {
    return this.playIntent;
  }

  isPlaying(): boolean {
    return !this.playerController.paused && !this.playerController.ended;
  }

  seekToTimestamp(
    time: number,
    play: boolean = false,
    preservePlayIntent: boolean = false,
  ) {
    if (!preservePlayIntent) this.playIntent = play;
    this.cancelPlayIntent();
    // a seek outside the current playback window is a no-op: the view
    // moves its anchor and chunk on such seeks, and the rebuilt source
    // resumes at the anchor (startPosition plus the post-load seek).
    // Seeking here would only reposition the outgoing source's media
    if (time < this.timeRange.after || time > this.timeRange.before) {
      return;
    }

    if (this.playerMode != "playback") {
      this.playerMode = "playback";
    }

    const seekSeconds = calculateSeekPosition(
      time,
      this.recordings,
      this.inpointOffset,
    );

    if (seekSeconds === undefined) {
      this.setNoRecording(true);
      return;
    }

    if (this.playerController.currentTime === seekSeconds) {
      // seeking to the current position fires no seeked event, so apply
      // the play intent directly (this includes position 0, which the
      // player sits at before its first seek)
      if (play) {
        this.startPlayback();
      } else {
        this.playerController.pause();
      }
      return;
    }

    this.playerController.currentTime = seekSeconds;

    if (play) {
      this.waitAndPlay();
    } else {
      this.playerController.pause();
    }
  }

  waitAndPlay() {
    this.cancelPendingSeek?.();
    return new Promise<boolean>((resolve) => {
      let fallback: NodeJS.Timeout | undefined;
      const generation = this.playGeneration;
      const source = this.playerController.src;
      const cleanup = () => {
        clearTimeout(fallback);
        this.playerController.removeEventListener("seeked", onSeekedHandler);
        this.cancelPendingSeek = undefined;
      };

      const onSeekedHandler = () => {
        const current =
          generation === this.playGeneration &&
          source === this.playerController.src &&
          this.playerController.isConnected;
        cleanup();
        if (current) this.startPlayback();
        resolve(current);
      };
      this.cancelPendingSeek = () => {
        cleanup();
        resolve(false);
      };

      this.playerController.addEventListener("seeked", onSeekedHandler, {
        once: true,
      });

      // iOS ManagedMediaSource pauses hls.js buffering, so `seeked` may
      // never fire; playing is what prompts WebKit to resume streaming
      if (typeof window !== "undefined" && "ManagedMediaSource" in window) {
        fallback = setTimeout(onSeekedHandler, SEEK_PLAY_FALLBACK_MS);
      }
    });
  }

  canSeekBuffered(timestamp: number): boolean {
    if (
      !this.playerController.isConnected ||
      this.playerController.readyState < 2 ||
      this.playerController.error ||
      timestamp < this.timeRange.after ||
      timestamp >= this.timeRange.before ||
      !this.recordings.some(
        (segment) =>
          segment.start_time <= timestamp && timestamp < segment.end_time,
      )
    )
      return false;
    const position = calculateSeekPosition(
      timestamp,
      this.recordings,
      this.inpointOffset,
    );
    if (position === undefined) return false;
    const buffered = this.playerController.buffered;
    for (let index = 0; index < buffered.length; index += 1) {
      if (
        buffered.start(index) <= position &&
        position + 0.05 < buffered.end(index)
      )
        return true;
    }
    return false;
  }

  seekToTimelineItem(timeline: TrackingDetailsSequence) {
    this.playerController.pause();
    this.seekToTimestamp(timeline.timestamp + this.annotationOffset);
    this.setFocusedItem(timeline);
  }

  getProgress(playerTime: number): number {
    return calculateRecordingTimestamp(playerTime, this.recordings);
  }

  relativeSeekTimestamp(diff: number): number | undefined {
    const first = this.recordings[0];
    if (!first) {
      this.setNoRecording(true);
      return undefined;
    }
    const target =
      this.playerController.currentTime + diff + this.inpointOffset;
    if (target < 0) return first.start_time + target;
    const duration = this.recordings.reduce(
      (sum, segment) => sum + segment.duration,
      0,
    );
    if (target >= duration && diff > 0)
      return (
        (this.recordings.at(-1)?.end_time ?? this.timeRange.before) +
        Math.max(0.001, target - duration)
      );
    return calculateRecordingTimestamp(target, this.recordings);
  }

  scrubToTimestamp(time: number, saveIfNotReady: boolean = false) {
    this.cancelPlayIntent();
    const scrubResult = this.previewController.scrubToTimestamp(time);

    if (!scrubResult && saveIfNotReady) {
      this.previewController.setNewPreviewStartTime(time);
    }

    // pause even when no preview can render this range: a hidden player
    // left running reports stale times once the drag releases, bouncing
    // the handlebar back and sometimes swallowing the release seek
    if (this.playerMode != "scrubbing") {
      this.playerMode = "scrubbing";
      this.playerController.pause();
    }
  }

  hasRecordingAtTime(time: number): boolean {
    if (!this.recordings || this.recordings.length == 0) {
      return false;
    }

    return (
      this.recordings.find(
        (segment) => segment.start_time <= time && segment.end_time >= time,
      ) != undefined
    );
  }
}

export default typeof DynamicVideoController;
