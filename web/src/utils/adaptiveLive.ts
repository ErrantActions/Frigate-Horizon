import type { CameraConfig, FrigateConfig } from "@/types/frigateConfig";
import { transcodeStreamName, webRtcDetailHeights } from "./liveTranscode";

export function dashboardLiveStreams(
  camera: CameraConfig,
  config?: FrigateConfig,
): Record<string, string> {
  if (config?.live_webrtc?.enabled) {
    return Object.fromEntries(
      webRtcDetailHeights(config).map((height) => [
        `${height}p`,
        transcodeStreamName(camera.name, height),
      ]),
    );
  }
  const advertised = new Set(adaptiveLiveStreams(camera, config));
  return Object.fromEntries(
    Object.entries(camera.live.streams).filter(([, name]) =>
      advertised.has(name),
    ),
  );
}

export function adaptiveLiveStreams(
  camera: CameraConfig,
  config?: FrigateConfig,
): string[] {
  if (config?.live_webrtc?.enabled) {
    return webRtcDetailHeights(config).map((height) =>
      transcodeStreamName(camera.name, height),
    );
  }
  return camera.live.transcode
    ? [...camera.live.transcode.qualities]
        .filter((quality) => quality.height >= 720)
        .sort((a, b) => b.height - a.height)
        .map((quality) => transcodeStreamName(camera.name, quality.height))
        .filter((name) => Object.values(camera.live.streams).includes(name))
    : [];
}
