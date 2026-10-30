import { test, expect } from "@playwright/test";
import { DynamicVideoController } from "../../src/components/player/dynamic/DynamicVideoController";
import {
  PlaybackPerformance,
  playbackBufferTarget,
  PLAYBACK_BUFFER_BYTES,
} from "../../src/components/player/playbackPerformance";
import type { Recording } from "../../src/types/record";

function controllerFixture() {
  const state = { plays: 0, paused: true, errors: [] as unknown[] };
  const video = Object.assign(new EventTarget(), {
    src: "blob:fixture",
    currentTime: 0,
    isConnected: true,
    muted: false,
    error: null,
    ended: false,
    readyState: 4,
    buffered: { length: 1, start: () => 0, end: () => 20 },
    get paused() {
      return state.paused;
    },
    play: async () => {
      state.plays += 1;
      state.paused = false;
    },
    pause: () => {
      state.paused = true;
    },
  });
  const controller = new DynamicVideoController(
    "fixture",
    video,
    { scrubToTimestamp: () => true, setNewPreviewStartTime: () => {} },
    0,
    "playback",
    () => {},
    () => {},
    (error) => state.errors.push(error),
  );
  Object.defineProperty(video, "paused", { get: () => state.paused });
  const recordings: Recording[] = [
    [100, 110],
    [120, 130],
  ].map(([start, end]) => ({
    id: String(start),
    camera: "fixture",
    start_time: start,
    end_time: end,
    duration: end - start,
    path: "/fixture",
    segment_size: 1,
    motion: 0,
    objects: 0,
    dBFS: 0,
  }));
  controller.newPlayback({
    recordings,
    timeRange: { after: 100, before: 130 },
  });
  return { controller, video, state, recordings };
}

test.describe("Player lifecycle and measurements @critical @mobile", () => {
  test("only the latest seek may resume playback", () => {
    const { controller, video, state } = controllerFixture();
    controller.seekToTimestamp(105, true);
    controller.seekToTimestamp(125, true);
    video.dispatchEvent(new Event("seeked"));
    expect(state.plays).toBe(1);
    expect(video.currentTime).toBe(15);
    controller.dispose();
  });

  test("a later paused seek cancels an older play request", () => {
    const { controller, video, state } = controllerFixture();
    controller.seekToTimestamp(105, true);
    controller.seekToTimestamp(125, false);
    video.dispatchEvent(new Event("seeked"));
    expect(state.plays).toBe(0);
    expect(state.paused).toBe(true);
  });

  test("pause, disposal, new timelines and scrubbing settle pending waits", async () => {
    for (const action of ["pause", "dispose", "timeline", "scrub"] as const) {
      const { controller, video, state, recordings } = controllerFixture();
      const waiting = controller.waitAndPlay();
      if (action === "timeline")
        controller.newPlayback({
          recordings,
          timeRange: { after: 99, before: 130 },
        });
      else if (action === "scrub") controller.scrubToTimestamp(105);
      else controller[action]();
      await expect(waiting).resolves.toBe(false);
      video.dispatchEvent(new Event("seeked"));
      expect(state.plays).toBe(0);
    }
  });

  test("source replacement prevents a delayed seek event from starting video", async () => {
    const { controller, video, state } = controllerFixture();
    const waiting = controller.waitAndPlay();
    video.src = "blob:replacement";
    video.dispatchEvent(new Event("seeked"));
    await expect(waiting).resolves.toBe(false);
    expect(state.plays).toBe(0);
  });

  test("pause suppresses a delayed autoplay retry", async () => {
    const { controller, video, state } = controllerFixture();
    video.play = async () => {
      state.plays += 1;
      throw Object.assign(new Error("Autoplay denied"), {
        name: "NotAllowedError",
      });
    };
    controller.play();
    controller.pause();
    await Promise.resolve();
    await Promise.resolve();
    expect(state.plays).toBe(1);
    expect(video.muted).toBe(false);
    expect(state.errors).toEqual([]);
  });

  test("genuine playback errors are surfaced", async () => {
    const { controller, video, state } = controllerFixture();
    const failure = new Error("synthetic decoder failure");
    video.play = () => Promise.reject(failure);
    controller.play();
    await expect.poll(() => state.errors.length).toBe(1);
    expect(state.errors[0]).toBe(failure);
  });

  test("buffer reuse rejects gaps, unbuffered times and unstable decoders", () => {
    const { controller, video } = controllerFixture();
    expect(controller.canSeekBuffered(105)).toBe(true);
    expect(controller.canSeekBuffered(125)).toBe(true);
    expect(controller.canSeekBuffered(115)).toBe(false);
    expect(controller.canSeekBuffered(130)).toBe(false);
    expect(controller.canSeekBuffered(99)).toBe(false);
    video.buffered.end = () => 8;
    expect(controller.canSeekBuffered(125)).toBe(false);
    video.readyState = 1;
    expect(controller.canSeekBuffered(105)).toBe(false);
  });

  test("equivalent metadata refreshes do not cancel an in-flight seek", () => {
    const { controller, video, state, recordings } = controllerFixture();
    controller.seekToTimestamp(105, true);
    controller.newPlayback({
      recordings: recordings.map((segment) => ({ ...segment })),
      timeRange: { after: 100, before: 130 },
    });

    video.dispatchEvent(new Event("seeked"));
    expect(state.plays).toBe(1);
  });

  test("relative seeks preserve recording gaps and cross source boundaries", () => {
    const { controller, video } = controllerFixture();
    expect(controller.relativeSeekTimestamp(5)).toBe(105);
    video.currentTime = 10;
    expect(controller.relativeSeekTimestamp(5)).toBe(125);
    video.currentTime = 18;
    expect(controller.relativeSeekTimestamp(10)).toBe(138);
    video.currentTime = 2;
    expect(controller.relativeSeekTimestamp(-10)).toBe(92);
  });

  test("temporary suspension and model positioning preserve explicit play intent", () => {
    const { controller } = controllerFixture();
    controller.play();
    controller.suspend();
    expect(controller.wantsPlayback()).toBe(true);
    controller.seekToTimestamp(105, false, true);
    expect(controller.wantsPlayback()).toBe(true);
    controller.pause();
    controller.seekToTimestamp(120, false, true);
    expect(controller.wantsPlayback()).toBe(false);
  });

  test("buffer budgets start small, grow after stable playback, and respect bytes", () => {
    const base = { stableSeconds: 0, playbackRate: 1, saveData: false };
    expect(playbackBufferTarget(base)).toBe(6);
    expect(playbackBufferTarget({ ...base, stableSeconds: 10 })).toBe(30);
    expect(
      playbackBufferTarget({ ...base, stableSeconds: 10, saveData: true }),
    ).toBe(10);
    expect(
      playbackBufferTarget({ ...base, stableSeconds: 10, playbackRate: 16 }),
    ).toBe(60);
    const bitrate = 80_000_000;
    expect(
      (playbackBufferTarget({ ...base, stableSeconds: 10, bitrate }) *
        bitrate) /
        8,
    ).toBeLessThanOrEqual(PLAYBACK_BUFFER_BYTES);
    expect(
      playbackBufferTarget({ ...base, stableSeconds: 10, preferredLength: 20 }),
    ).toBe(20);
  });

  test("first-frame and latest-seek latency use presented frames", () => {
    let now = 0;
    const metrics = new PlaybackPerformance(() => now);
    metrics.sourceChanged();
    now = 250;
    metrics.framePresented(0);
    expect(metrics.snapshot().firstFrameMs).toBe(250);
    now = 300;
    metrics.seekStarted(10);
    now = 400;
    metrics.seekStarted(20);
    now = 450;
    metrics.framePresented(10);
    expect(metrics.snapshot().lastSeekMs).toBeNull();
    now = 600;
    metrics.framePresented(20);
    expect(metrics.snapshot().lastSeekMs).toBe(200);
    expect(metrics.snapshot().seeks).toBe(2);
  });

  test("suspension is excluded from startup and duplicated stalls are counted once", () => {
    let now = 0;
    const metrics = new PlaybackPerformance(() => now);
    metrics.sourceChanged();
    now = 100;
    metrics.suspend();
    now = 1100;
    metrics.resume();
    now = 1200;
    metrics.framePresented(0);
    expect(metrics.snapshot().firstFrameMs).toBe(200);
    metrics.stallStarted();
    metrics.stallStarted();
    now = 1500;
    metrics.suspend();
    now = 5000;
    expect(metrics.snapshot().stalls).toBe(1);
    expect(metrics.snapshot().stalledMs).toBe(300);
    metrics.resume();
    metrics.sourceChanged();
    expect(metrics.snapshot().sourceLoads).toBe(2);
    expect(metrics.snapshot().firstFrameMs).toBeNull();
  });
});
