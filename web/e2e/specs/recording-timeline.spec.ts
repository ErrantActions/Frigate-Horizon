import { test, expect } from "@playwright/test";
import type { Recording } from "../../src/types/record";
import {
  calculateRecordingTimestamp,
  calculateSeekPosition,
  playWithTemporaryMuteFallback,
} from "../../src/utils/videoUtil";

function segment(
  start: number,
  end: number,
  duration: number,
  leadIn?: number,
): Recording {
  return {
    id: `${start}`,
    camera: "fixture",
    start_time: start,
    end_time: end,
    duration,
    lead_in: leadIn,
    path: "/fixture",
    segment_size: 0,
    motion: 0,
    objects: 0,
    dBFS: 0,
  };
}

test.describe("Recording timeline conversion @critical @mobile", () => {
  test("preserves legacy nginx keyframe lead-in inference", () => {
    const recordings = [segment(100, 110, 10.5), segment(120, 130, 10)];
    expect(calculateRecordingTimestamp(0.25, recordings)).toBe(100);
    expect(calculateRecordingTimestamp(5.5, recordings)).toBe(105);
    expect(calculateSeekPosition(105, recordings)).toBe(5.5);
  });

  test.describe("Recording play intent @critical @mobile", () => {
    test("retains the existing muted autoplay retry", async () => {
      const denied = Object.assign(new Error("gesture required"), {
        name: "NotAllowedError",
      });
      let calls = 0;
      const video = {
        src: "blob:same",
        isConnected: true,
        muted: false,
        play: () =>
          ++calls === 1 ? Promise.reject(denied) : Promise.resolve(),
      };
      await expect(
        playWithTemporaryMuteFallback(video),
      ).resolves.toBeUndefined();
      expect(video.muted).toBe(true);
      expect(calls).toBe(2);
    });
    test("a replaced source cancels an outgoing play promise without hiding codec errors", async () => {
      const cancelled = Object.assign(new Error("source replaced"), {
        name: "AbortError",
      });
      const video = {
        src: "blob:old",
        isConnected: true,
        muted: false,
        play: () => Promise.reject(cancelled),
      };
      const outgoing = playWithTemporaryMuteFallback(video);
      video.src = "blob:new";
      await expect(outgoing).resolves.toBeUndefined();
      const unsupported = Object.assign(new Error("unsupported codec"), {
        name: "NotSupportedError",
      });
      video.play = () => Promise.reject(unsupported);
      await expect(playWithTemporaryMuteFallback(video)).rejects.toBe(
        unsupported,
      );
    });

    test("a disconnected player cancels its outgoing play promise", async () => {
      const cancelled = Object.assign(new Error("player removed"), {
        name: "AbortError",
      });
      const video = {
        src: "blob:old",
        isConnected: true,
        muted: false,
        play: () => Promise.reject(cancelled),
      };
      const outgoing = playWithTemporaryMuteFallback(video);
      video.isConnected = false;
      await expect(outgoing).resolves.toBeUndefined();
    });

    test("an unexplained abort on the same connected source is still reported", async () => {
      const cancelled = Object.assign(new Error("unexpected abort"), {
        name: "AbortError",
      });
      const video = {
        src: "blob:same",
        isConnected: true,
        muted: false,
        play: () => Promise.reject(cancelled),
      };
      await expect(playWithTemporaryMuteFallback(video)).rejects.toBe(
        cancelled,
      );
    });

    test("autoplay mute retry does not silently swallow a decoder failure", async () => {
      const denied = Object.assign(new Error("gesture required"), {
        name: "NotAllowedError",
      });
      const unsupported = Object.assign(new Error("unsupported codec"), {
        name: "NotSupportedError",
      });
      let calls = 0;
      const video = {
        src: "blob:same",
        isConnected: true,
        muted: false,
        play: () => Promise.reject(++calls === 1 ? denied : unsupported),
      };
      await expect(playWithTemporaryMuteFallback(video)).rejects.toBe(
        unsupported,
      );
      expect(video.muted).toBe(true);
      expect(calls).toBe(2);
    });
  });

  test("does not mistake measured encoder tail padding for earlier capture time", () => {
    const recordings = [
      segment(100, 110, 10.05, 0),
      segment(120, 130, 10.1, 0),
    ];
    expect(calculateRecordingTimestamp(5, recordings)).toBe(105);
    expect(calculateRecordingTimestamp(10.03, recordings)).toBe(110);
    expect(calculateRecordingTimestamp(10.05, recordings)).toBe(120);
    expect(calculateSeekPosition(125, recordings)).toBeCloseTo(15.05, 8);
    expect(calculateRecordingTimestamp(15.05, recordings)).toBe(125);
  });

  test("supports a real initial media offset independently of tail padding", () => {
    const recordings = [segment(100, 110, 10.04, 0.021)];
    expect(calculateSeekPosition(100, recordings)).toBeCloseTo(0.021, 8);
    expect(calculateRecordingTimestamp(0, recordings)).toBe(100);
    expect(calculateRecordingTimestamp(5.021, recordings)).toBe(105);
    expect(calculateRecordingTimestamp(10.035, recordings)).toBe(110);
  });

  test("seeks contiguous boundaries after the preceding encoded tail", () => {
    const recordings = [segment(100, 110, 10.05, 0), segment(110, 120, 10, 0)];
    expect(calculateSeekPosition(110, recordings)).toBeCloseTo(10.05, 8);
    expect(calculateRecordingTimestamp(10.05, recordings)).toBe(110);
    expect(calculateSeekPosition(120, recordings)).toBeCloseTo(20.05, 8);
  });

  test("keeps gaps, out-of-range seeks and end clamping explicit", () => {
    const recordings = [segment(100, 110, 10.05, 0), segment(120, 130, 10, 0)];
    expect(calculateSeekPosition(99, recordings)).toBeUndefined();
    expect(calculateSeekPosition(131, recordings)).toBeUndefined();
    expect(calculateSeekPosition(115, recordings)).toBeCloseTo(10.05, 8);
    expect(calculateRecordingTimestamp(100, recordings)).toBe(130);
    expect(calculateRecordingTimestamp(1, [])).toBe(0);
  });

  test("round-trips many differently padded spans without cumulative clock drift", () => {
    const recordings = Array.from({ length: 100 }, (_, index) =>
      segment(1000 + index * 2, 1001 + index * 2, 1.04, 0),
    );
    for (let index = 0; index < recordings.length; index++) {
      const timestamp = 1000.5 + index * 2;
      const seek = calculateSeekPosition(timestamp, recordings);
      expect(seek).toBeDefined();
      expect(calculateRecordingTimestamp(seek!, recordings)).toBeCloseTo(
        timestamp,
        8,
      );
    }
  });
});
