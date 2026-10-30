import type { CameraConfig } from "@/types/frigateConfig";
import { isTranscodeStreamName, transcodeStreamName } from "./liveTranscode";

/** Main-source renditions only, never camera substreams. */
export function gpu4kStreamName(camera: string): string {
  return `${camera}_live_gpu_4k`;
}

export function hasGpu4kLiveStream(camera: CameraConfig): boolean {
  return Object.values(camera.live.streams).includes(
    gpu4kStreamName(camera.name),
  );
}

export function dashboardLiveStreams(
  camera: CameraConfig,
): Record<string, string> {
  return Object.fromEntries(
    Object.entries(camera.live.streams).filter(
      ([, name]) => name !== gpu4kStreamName(camera.name),
    ),
  );
}

export function adaptiveLiveStreams(
  camera: CameraConfig,
  individual = false,
): string[] {
  const main =
    camera.live.transcode?.source ?? Object.values(camera.live.streams)[0];
  if (!main) return [];
  const generated = camera.live.transcode
    ? [...camera.live.transcode.qualities]
        .filter((quality) => quality.height >= 720)
        .sort((a, b) => b.height - a.height)
        .map((quality) => transcodeStreamName(camera.name, quality.height))
        .filter((name) => Object.values(camera.live.streams).includes(name))
    : [];
  if (individual && hasGpu4kLiveStream(camera)) {
    return [
      main,
      gpu4kStreamName(camera.name),
      ...generated.filter((name) => name !== main),
    ];
  }
  return [main, ...generated.filter((name) => name !== main)];
}

/** Choose the smallest rendition that covers the actual display pixels. */
export function viewportLiveRung(
  camera: CameraConfig,
  streams: string[],
  height: number,
  pixelRatio: number,
): number {
  if (height <= 0) return Math.max(0, streams.length - 1);
  const target = height * Math.min(2, Math.max(1, pixelRatio));
  let selected = 0;
  streams.forEach((name, index) => {
    const resolution = isTranscodeStreamName(camera.name, name)
      ? camera.live.transcode?.qualities.find(
          (quality) =>
            transcodeStreamName(camera.name, quality.height) === name,
        )?.height
      : camera.live.height;
    if (resolution !== undefined && resolution >= target) selected = index;
  });
  return selected;
}
