import { CameraConfig, FrigateConfig } from "@/types/frigateConfig";
import { useMemo } from "react";
import useSWR from "swr";
import useDeferredStreamMetadata from "./use-deferred-stream-metadata";
import { detectCameraAudioFeatures } from "@/utils/cameraUtil";
import { isRestreamedStream } from "@/utils/liveTranscode";
import { adaptiveLiveStreams } from "@/utils/adaptiveLive";

export default function useCameraStreamMetadata(
  cameras: CameraConfig[],
  activeStreams?: Record<string, string>,
) {
  const { data: config } = useSWR<FrigateConfig>("config");
  const restreamedStreamNames = useMemo(() => {
    const names = new Set<string>();
    for (const camera of cameras) {
      const active = activeStreams?.[camera.name];
      const selected = config?.live_webrtc?.enabled
        ? [camera.live.transcode.source || camera.name]
        : active
          ? [active]
          : Object.values(camera.live.streams);
      for (const name of selected) {
        if (isRestreamedStream(config, name)) names.add(name);
      }
    }
    return [...names];
  }, [cameras, config, activeStreams]);
  const streamMetadata = useDeferredStreamMetadata(restreamedStreamNames);

  const states = useMemo(() => {
    const isRestreamedStates: Record<string, boolean> = {};
    const supportsAudioOutputStates: Record<
      string,
      { supportsAudio: boolean; cameraName: string }
    > = {};
    for (const camera of cameras) {
      const selected =
        activeStreams?.[camera.name] ?? Object.values(camera.live.streams)[0];
      isRestreamedStates[camera.name] =
        isRestreamedStream(config, selected) ?? false;
      for (const stream of new Set([
        ...Object.values(camera.live.streams),
        ...adaptiveLiveStreams(camera, config),
      ])) {
        const source = config?.live_webrtc?.enabled
          ? camera.live.transcode.source || camera.name
          : stream;
        supportsAudioOutputStates[stream] = {
          supportsAudio: detectCameraAudioFeatures(streamMetadata[source])
            .audioOutput,
          cameraName: camera.name,
        };
      }
    }
    return { isRestreamedStates, supportsAudioOutputStates };
  }, [activeStreams, cameras, config, streamMetadata]);

  return { ...states, streamMetadata };
}
