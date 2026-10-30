import type { CameraConfig } from "@/types/frigateConfig";
import { transcodeStreamName } from "./liveTranscode";

export function dashboardLiveStreams(
  camera: CameraConfig,
): Record<string, string> {
  const advertised = new Set(adaptiveLiveStreams(camera));
  return Object.fromEntries(
    Object.entries(camera.live.streams).filter(([, name]) =>
      advertised.has(name),
    ),
  );
}

export function adaptiveLiveStreams(camera: CameraConfig): string[] {
  return camera.live.transcode
    ? [...camera.live.transcode.qualities]
        .filter((quality) => quality.height >= 720)
        .sort((a, b) => b.height - a.height)
        .map((quality) => transcodeStreamName(camera.name, quality.height))
        .filter((name) => Object.values(camera.live.streams).includes(name))
    : [];
}
