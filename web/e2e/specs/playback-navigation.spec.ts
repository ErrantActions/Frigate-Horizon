import { test, expect } from "@playwright/test";
import {
  adjacentPlaybackEvent,
  mergePlaybackIntervals,
  nextActivePlaybackTime,
  parseCaptureTimestamp,
} from "../../src/utils/playbackNavigation";

test.describe("Capture-time navigation @critical @mobile", () => {
  test("exact-time navigation requires an explicit timezone", () => {
    expect(parseCaptureTimestamp("2026-10-20T12:30:00-05:00")).toBe(
      Date.parse("2026-10-20T17:30:00Z") / 1000,
    );
    expect(parseCaptureTimestamp("2026-10-20T12:30:00")).toBeUndefined();
    expect(parseCaptureTimestamp("not a timestamp")).toBeUndefined();
  });
  test("merges padded activity without filling real gaps", () => {
    const intervals = [
      { after: 120, before: 130 },
      { after: 98, before: 110 },
      { after: 108, before: 115 },
      { after: 130, before: 140 },
    ];
    expect(
      mergePlaybackIntervals(intervals, { after: 100, before: 135 }),
    ).toEqual([
      { after: 100, before: 115 },
      { after: 120, before: 135 },
    ]);
    expect(intervals[1].after).toBe(98);
  });
  test("event jumps ignore order, duplicates and the current event boundary", () => {
    const events = [120, 100, 110, 110].map((start_time) => ({ start_time }));
    expect(adjacentPlaybackEvent(events, 110, "previous")).toBe(100);
    expect(adjacentPlaybackEvent(events, 110, "next")).toBe(120);
    expect(adjacentPlaybackEvent(events, 100, "previous")).toBeUndefined();
  });
  test("activity navigation stops explicitly at the end", () => {
    const intervals = [
      { after: 100, before: 110 },
      { after: 120, before: 130 },
    ];
    expect(nextActivePlaybackTime(105, intervals)).toBe(105);
    expect(nextActivePlaybackTime(110, intervals)).toBe(120);
    expect(nextActivePlaybackTime(130, intervals)).toBeUndefined();
  });
  test("invalid timestamps are explicit failures", () => {
    expect(() =>
      mergePlaybackIntervals([{ after: 10, before: 5 }], {
        after: 0,
        before: 20,
      }),
    ).toThrow(RangeError);
    expect(() =>
      adjacentPlaybackEvent([{ start_time: NaN }], 100, "next"),
    ).toThrow(RangeError);
    expect(() => nextActivePlaybackTime(Infinity, [])).toThrow(RangeError);
  });
});
