import { CameraConfig, FrigateConfig } from "@/types/frigateConfig";
import { useCallback, useEffect, useState, useMemo } from "react";
import useSWR from "swr";
import { LivePlayerMode } from "@/types/live";
import useDeferredStreamMetadata from "./use-deferred-stream-metadata";
import { detectCameraAudioFeatures } from "@/utils/cameraUtil";
import { isRestreamedStream } from "@/utils/liveTranscode";
import { resolveLivePlayerMode } from "@/utils/liveMode";
import { adaptiveLiveStreams } from "@/utils/adaptiveLive";
import {
  evaluateStreamWebRTCAvailability,
  useWebRTCGloballyAvailable,
  type StreamAvailability,
} from "./use-webrtc-availability";

export default function useCameraLiveMode(
  cameras: CameraConfig[],
  windowVisible: boolean,
  activeStreams?: { [cameraName: string]: string },
  preferredModes?: { [cameraName: string]: LivePlayerMode | undefined },
) {
  const { data: config } = useSWR<FrigateConfig>("config");

  // Compute which streams need metadata (restreamed streams only)
  const restreamedStreamNames = useMemo(() => {
    if (!cameras || !config) return [];

    const streamNames = new Set<string>();
    cameras.forEach((camera) => {
      if (activeStreams && activeStreams[camera.name]) {
        const selectedStreamName = activeStreams[camera.name];
        const isRestreamed = isRestreamedStream(config, selectedStreamName);

        if (isRestreamed) {
          streamNames.add(selectedStreamName);
        }
      } else {
        Object.values(camera.live.streams).forEach((streamName) => {
          const isRestreamed = isRestreamedStream(config, streamName);

          if (isRestreamed) {
            streamNames.add(streamName);
          }
        });
      }
    });

    return Array.from(streamNames);
  }, [cameras, config, activeStreams]);

  // Fetch stream metadata with deferred loading (doesn't block initial render)
  const streamMetadata = useDeferredStreamMetadata(restreamedStreamNames);

  const { globallyAvailable, globalReason } = useWebRTCGloballyAvailable();

  // "checking" counts as usable because the probe re-enters it on every mount,
  // and treating it as unavailable would downgrade a saved WebRTC choice.
  const { webRTCUsableStates, webRTCAvailabilityStates } = useMemo(() => {
    const states: { [cameraName: string]: boolean } = {};
    const availability: Record<string, StreamAvailability> = {};
    cameras.forEach((camera) => {
      const streamName =
        activeStreams?.[camera.name] ?? Object.values(camera.live.streams)[0];
      const { available, reason } = evaluateStreamWebRTCAvailability({
        globallyAvailable,
        globalReason,
        metadata: streamMetadata[streamName],
      });
      states[camera.name] = available || reason === "checking";
      availability[camera.name] = { available, reason };
    });
    return {
      webRTCUsableStates: states,
      webRTCAvailabilityStates: availability,
    };
  }, [cameras, activeStreams, globallyAvailable, globalReason, streamMetadata]);

  // Compute live mode states
  const [preferredLiveModes, setPreferredLiveModes] = useState<{
    [key: string]: LivePlayerMode;
  }>({});
  const [isRestreamedStates, setIsRestreamedStates] = useState<{
    [key: string]: boolean;
  }>({});
  const [supportsAudioOutputStates, setSupportsAudioOutputStates] = useState<{
    [key: string]: {
      supportsAudio: boolean;
      cameraName: string;
    };
  }>({});

  useEffect(() => {
    if (!cameras || cameras.length === 0) return;

    const mseSupported =
      "MediaSource" in window || "ManagedMediaSource" in window;

    const newPreferredLiveModes: { [key: string]: LivePlayerMode } = {};
    const newIsRestreamedStates: { [key: string]: boolean } = {};
    const newSupportsAudioOutputStates: {
      [key: string]: { supportsAudio: boolean; cameraName: string };
    } = {};

    cameras.forEach((camera) => {
      const selectedStreamName =
        activeStreams?.[camera.name] ?? Object.values(camera.live.streams)[0];
      const isRestreamed = isRestreamedStream(config, selectedStreamName);

      newIsRestreamedStates[camera.name] = isRestreamed ?? false;

      // Auto-selected default, overridden by the user's per-camera choice when viable
      // Runtime fallback (player errors) still adjusts the mode on top of this base
      newPreferredLiveModes[camera.name] = resolveLivePlayerMode({
        isRestreamed: !!isRestreamed,
        mseSupported,
        webRTCAvailable:
          webRTCAvailabilityStates[camera.name]?.available ?? false,
        webRTCChecking:
          webRTCAvailabilityStates[camera.name]?.reason === "checking",
        requested: preferredModes?.[camera.name],
        preferMse: adaptiveLiveStreams(camera).length > 1,
      });

      // Check each stream for audio support
      if (isRestreamed) {
        Object.values(camera.live.streams).forEach((streamName) => {
          const metadata = streamMetadata[streamName];
          const audioFeatures = detectCameraAudioFeatures(metadata);
          newSupportsAudioOutputStates[streamName] = {
            supportsAudio: audioFeatures.audioOutput,
            cameraName: camera.name,
          };
        });
      } else {
        newSupportsAudioOutputStates[camera.name] = {
          supportsAudio: false,
          cameraName: camera.name,
        };
      }
    });

    setPreferredLiveModes(newPreferredLiveModes);
    setIsRestreamedStates(newIsRestreamedStates);
    setSupportsAudioOutputStates(newSupportsAudioOutputStates);
  }, [
    activeStreams,
    cameras,
    config,
    windowVisible,
    streamMetadata,
    webRTCAvailabilityStates,
    preferredModes,
  ]);

  const resetPreferredLiveMode = useCallback(
    (cameraName: string) => {
      const mseSupported =
        "MediaSource" in window || "ManagedMediaSource" in window;
      const cameraConfig = cameras.find((camera) => camera.name === cameraName);
      const selectedStreamName =
        activeStreams?.[cameraName] ??
        (cameraConfig
          ? Object.values(cameraConfig.live.streams)[0]
          : cameraName);
      const isRestreamed = isRestreamedStream(config, selectedStreamName);

      setPreferredLiveModes((prevModes) => ({
        ...prevModes,
        [cameraName]: resolveLivePlayerMode({
          isRestreamed: !!isRestreamed,
          mseSupported,
          webRTCAvailable:
            webRTCAvailabilityStates[cameraName]?.available ?? false,
          webRTCChecking:
            webRTCAvailabilityStates[cameraName]?.reason === "checking",
          requested: preferredModes?.[cameraName],
          preferMse: cameraConfig
            ? adaptiveLiveStreams(cameraConfig).length > 1
            : false,
        }),
      }));
    },
    [activeStreams, cameras, config, webRTCAvailabilityStates, preferredModes],
  );

  return {
    preferredLiveModes,
    setPreferredLiveModes,
    resetPreferredLiveMode,
    isRestreamedStates,
    supportsAudioOutputStates,
    streamMetadata,
    webRTCUsableStates,
  };
}
