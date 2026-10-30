import { Recording } from "@/types/record";

type TimelineRecording = Pick<
  Recording,
  "start_time" | "end_time" | "duration" | "lead_in"
>;

function recordingLeadIn(segment: TimelineRecording): number {
  return (
    segment.lead_in ??
    Math.max(0, segment.duration - (segment.end_time - segment.start_time))
  );
}

/** Map encoded media time to capture time without treating tail padding as lead-in. */
export function calculateRecordingTimestamp(
  playerTime: number,
  recordings: readonly TimelineRecording[],
): number {
  let elapsed = 0;
  for (const segment of recordings) {
    if (elapsed + segment.duration > playerTime) {
      return Math.min(
        segment.end_time,
        segment.start_time +
          Math.max(0, playerTime - elapsed - recordingLeadIn(segment)),
      );
    }
    elapsed += segment.duration;
  }
  return recordings.at(-1)?.end_time ?? 0;
}

/** the HLS endpoint returns the vod segments with the first
 * segment of the hour trimmed, meaning it will start at
 * the beginning of the hour, cutting off any difference
 * that the segment has.
 */
export function calculateInpointOffset(
  timeRangeStart: number | undefined,
  firstRecordingSegment: TimelineRecording | undefined,
): number {
  if (!timeRangeStart || !firstRecordingSegment) {
    return 0;
  }

  // if the first recording segment does not cross over
  // the beginning of the time range then there is no offset
  if (
    firstRecordingSegment.start_time < timeRangeStart &&
    firstRecordingSegment.end_time > timeRangeStart
  ) {
    return timeRangeStart - firstRecordingSegment.start_time;
  }

  return 0;
}

/**
 * Calculates the video player time (in seconds) for a given timestamp
 * by iterating through recording segments and summing their durations.
 * This accounts for the fact that the video is a concatenation of segments,
 * not a single continuous stream.
 *
 * @param timestamp - The target timestamp to seek to
 * @param recordings - Array of recording segments
 * @param inpointOffset - HLS inpoint offset to subtract from the result
 * @returns The calculated seek position in seconds, or undefined if timestamp is out of range
 */
export function calculateSeekPosition(
  timestamp: number,
  recordings: readonly TimelineRecording[],
  inpointOffset: number = 0,
): number | undefined {
  if (!recordings || recordings.length === 0) {
    return undefined;
  }

  // Check if timestamp is within the recordings range
  if (
    timestamp < recordings[0].start_time ||
    timestamp > recordings[recordings.length - 1].end_time
  ) {
    return undefined;
  }

  let seekSeconds = 0;

  (recordings || []).every((segment) => {
    // if the next segment is past the desired time, stop calculating
    if (segment.start_time > timestamp) {
      return false;
    }

    const leadIn = recordingLeadIn(segment);

    if (segment.end_time <= timestamp) {
      seekSeconds += segment.duration;
      return true;
    }

    // We're in this segment - calculate position within it
    seekSeconds += leadIn + (timestamp - segment.start_time);
    return true;
  });

  // Adjust for HLS inpoint offset
  seekSeconds -= inpointOffset;

  return seekSeconds >= 0 ? seekSeconds : undefined;
}

/**
 * Attempts to play the video, and if it fails due to a NotAllowedError (often caused by browser autoplay restrictions),
 * it temporarily mutes the video and tries to play again.
 * @param video - The HTMLVideoElement to play
 */
export function isHlsPlaylistUrl(value: string): boolean {
  try {
    const url = new URL(value, window.location.href);
    return (
      (url.protocol === "https:" || url.protocol === "http:") &&
      url.pathname.toLowerCase().endsWith(".m3u8")
    );
  } catch (error) {
    if (error instanceof TypeError) return false;
    throw error;
  }
}

export function playWithTemporaryMuteFallback(
  video: Pick<HTMLVideoElement, "src" | "isConnected" | "muted" | "play">,
  cancelled: () => boolean = () => false,
) {
  if (cancelled()) return Promise.resolve();
  const requestedSource = video.src;
  const superseded = (error: { name?: string }) =>
    error.name === "AbortError" &&
    (!video.isConnected || video.src !== requestedSource);
  return video.play().catch((error: { name?: string }) => {
    if (cancelled()) return;
    if (superseded(error)) return;
    if (error.name === "NotAllowedError" && !video.muted) {
      video.muted = true;

      return video.play().catch((retryError: { name?: string }) => {
        if (cancelled()) return;
        if (superseded(retryError)) return;
        throw retryError;
      });
    }

    throw error;
  });
}
