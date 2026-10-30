import type { TimeRange } from "@/types/timeline";

export function mergePlaybackIntervals(
  intervals: readonly TimeRange[],
  bounds: TimeRange,
): TimeRange[] {
  if (
    !Number.isFinite(bounds.after) ||
    !Number.isFinite(bounds.before) ||
    bounds.after >= bounds.before
  )
    throw new RangeError("Invalid playback bounds");
  const ordered = intervals
    .map((interval) => {
      if (
        !Number.isFinite(interval.after) ||
        !Number.isFinite(interval.before) ||
        interval.after >= interval.before
      )
        throw new RangeError("Invalid playback interval");
      return {
        after: Math.max(bounds.after, interval.after),
        before: Math.min(bounds.before, interval.before),
      };
    })
    .filter((interval) => interval.after < interval.before)
    .sort((a, b) => a.after - b.after);
  const merged: TimeRange[] = [];
  for (const interval of ordered) {
    const last = merged.at(-1);
    if (last && interval.after <= last.before)
      last.before = Math.max(last.before, interval.before);
    else merged.push({ ...interval });
  }
  return merged;
}

export function adjacentPlaybackEvent(
  events: readonly { start_time: number }[],
  timestamp: number,
  direction: "previous" | "next",
): number | undefined {
  if (!Number.isFinite(timestamp))
    throw new RangeError("Invalid playback timestamp");
  const starts = events
    .map((event) => {
      if (!Number.isFinite(event.start_time))
        throw new RangeError("Invalid event timestamp");
      return event.start_time;
    })
    .filter((start) =>
      direction === "previous"
        ? start < timestamp - 0.25
        : start > timestamp + 0.25,
    );
  return starts.length
    ? direction === "previous"
      ? Math.max(...starts)
      : Math.min(...starts)
    : undefined;
}

export function nextActivePlaybackTime(
  timestamp: number,
  intervals: readonly TimeRange[],
): number | undefined {
  if (!Number.isFinite(timestamp))
    throw new RangeError("Invalid playback timestamp");
  for (const interval of intervals) {
    if (timestamp < interval.after) return interval.after;
    if (timestamp < interval.before) return timestamp;
  }

  return undefined;
}

export function parseCaptureTimestamp(value: string): number | undefined {
  if (
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,3})?)?(?:Z|[+-]\d{2}:\d{2})$/.test(
      value,
    )
  )
    return undefined;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed / 1000 : undefined;
}
