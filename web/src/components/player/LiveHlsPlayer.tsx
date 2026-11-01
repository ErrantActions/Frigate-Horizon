import { baseUrl } from "@/api/baseUrl";
import type { LiveHlsRendition } from "@/types/liveHls";
import type { PlayerStatsType, VideoResolutionType } from "@/types/live";
import { playWithTemporaryMuteFallback } from "@/utils/videoUtil";
import { isMseMimeTypeSupported } from "@/utils/codecSupport";
import Hls from "hls.js";
import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";

type Props = {
  camera: string;
  renditions: LiveHlsRendition[];
  maximumHeight: number;
  preferredHeight?: number;
  className?: string;
  playbackEnabled: boolean;
  audioEnabled: boolean;
  volume?: number;
  pip?: boolean;
  getStats?: boolean;
  setStats: (stats: PlayerStatsType) => void;
  onPlaying: () => void;
  onError: () => void;
  onResolution: (resolution: VideoResolutionType) => void;
  onLevelChange?: (height: number) => void;
};

export default function LiveHlsPlayer({
  camera,
  renditions,
  maximumHeight,
  preferredHeight,
  className,
  playbackEnabled,
  audioEnabled,
  volume,
  pip = false,
  getStats = false,
  setStats,
  onPlaying,
  onError,
  onResolution,
  onLevelChange,
}: Props) {
  const { t } = useTranslation("components/player");
  const videoRef = useRef<HTMLVideoElement>(null);
  const hlsRef = useRef<Hls | undefined>(undefined);
  const callbacks = useRef({ onPlaying, onError, onResolution, onLevelChange });
  const failed = useRef(false);
  const maximumRendition =
    renditions.find(
      (rendition) => rendition.height >= Math.max(720, maximumHeight),
    ) ?? renditions.at(-1);
  const saveData = (
    navigator as Navigator & { connection?: { saveData?: boolean } }
  ).connection?.saveData;
  const selectedHeight = saveData ? renditions[0]?.height : preferredHeight;
  const heightLimit = maximumRendition?.height;
  const audioSupported =
    !Hls.isSupported() ||
    isMseMimeTypeSupported('audio/mp4; codecs="mp4a.40.2"');
  const requestAudio =
    audioSupported && (audioEnabled || preferredHeight === undefined);

  useEffect(() => {
    if (audioEnabled && !audioSupported) {
      toast.warning(t("adaptive.audioUnsupported"), {
        id: `live-hls-audio-${camera}`,
      });
    }
  }, [audioEnabled, audioSupported, camera, t]);

  useEffect(() => {
    callbacks.current = { onPlaying, onError, onResolution, onLevelChange };
  }, [onPlaying, onError, onResolution, onLevelChange]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !playbackEnabled) return;
    failed.current = false;
    let alive = true;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let networkRetries = 0;
    const url = new URL(
      `api/${encodeURIComponent(camera)}/live/hls/master.m3u8`,
      baseUrl,
    );
    url.searchParams.set("audio", String(requestAudio));
    if (selectedHeight !== undefined) {
      url.searchParams.set("height", String(selectedHeight));
    } else if (heightLimit !== undefined) {
      url.searchParams.set("max_height", String(heightLimit));
    }
    const fail = () => {
      if (!alive || failed.current) return;
      failed.current = true;
      callbacks.current.onError();
    };
    const play = () => {
      void playWithTemporaryMuteFallback(video).catch(fail);
    };
    const updateResolution = () => {
      if (video.videoWidth > 0 && video.videoHeight > 0) {
        callbacks.current.onResolution({
          width: video.videoWidth,
          height: video.videoHeight,
        });
        if (!hlsRef.current) {
          callbacks.current.onLevelChange?.(video.videoHeight);
        }
      }
    };
    const ready = () => {
      if (!alive) return;
      updateResolution();
      callbacks.current.onPlaying();
    };
    video.addEventListener("loadeddata", ready);
    video.addEventListener("resize", updateResolution);
    video.addEventListener("error", fail);
    if (Hls.isSupported()) {
      const hls = new Hls({
        lowLatencyMode: true,
        startLevel: 0,
        abrEwmaDefaultEstimate: 500_000,
        // Forget LAN throughput before a slower connection drains the live buffer.
        abrEwmaFastLive: 0.5,
        abrEwmaSlowLive: 2,
        maxBufferLength: 6,
        maxMaxBufferLength: 12,
        backBufferLength: 0,
        maxBufferSize: 12_000_000,
        manifestLoadingTimeOut: 30_000,
        levelLoadingTimeOut: 30_000,
        fragLoadingTimeOut: 30_000,
        xhrSetup: (xhr) => {
          xhr.withCredentials = true;
        },
      });
      hlsRef.current = hls;
      hls.on(Hls.Events.MEDIA_ATTACHED, () => hls.loadSource(url.href));
      hls.on(Hls.Events.MANIFEST_PARSED, play);
      hls.on(Hls.Events.LEVEL_SWITCHED, (_event, data) => {
        const height = hls.levels[data.level]?.height;
        if (height !== undefined) callbacks.current.onLevelChange?.(height);
      });
      hls.on(Hls.Events.ERROR, (_event, data) => {
        if (!data.fatal) return;
        if (
          data.type === Hls.ErrorTypes.NETWORK_ERROR &&
          data.response?.code !== 401 &&
          data.response?.code !== 403 &&
          networkRetries < 2
        ) {
          hls.stopLoad();
          networkRetries += 1;
          clearTimeout(retryTimer);
          retryTimer = setTimeout(() => {
            if (alive) hls.loadSource(url.href);
          }, networkRetries * 1000);
          return;
        }
        fail();
      });
      hls.attachMedia(video);
    } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = url.href;
      video.addEventListener("loadedmetadata", play);
    } else {
      fail();
    }
    return () => {
      alive = false;
      clearTimeout(retryTimer);
      video.removeEventListener("loadeddata", ready);
      video.removeEventListener("resize", updateResolution);
      video.removeEventListener("error", fail);
      video.removeEventListener("loadedmetadata", play);
      hlsRef.current?.destroy();
      hlsRef.current = undefined;
      video.pause();
      video.removeAttribute("src");
      video.load();
    };
  }, [camera, playbackEnabled, selectedHeight, heightLimit, requestAudio]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    video.muted = !audioEnabled;
    video.volume = volume ?? 1;
  }, [audioEnabled, volume]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !pip || !video.requestPictureInPicture) return;
    const enter = () => {
      if (document.pictureInPictureElement === video) return;
      void video.requestPictureInPicture().catch(() => {
        toast.error(t("adaptive.pictureInPictureUnavailable"));
      });
    };
    if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) enter();
    else video.addEventListener("loadeddata", enter, { once: true });
    return () => video.removeEventListener("loadeddata", enter);
  }, [pip, t]);

  useEffect(() => {
    if (!getStats || !playbackEnabled) return;
    const timer = window.setInterval(() => {
      const quality = videoRef.current?.getVideoPlaybackQuality?.();
      const total = quality?.totalVideoFrames ?? 0;
      const dropped = quality?.droppedVideoFrames ?? 0;
      setStats({
        streamType: "LL-HLS",
        bandwidth: (hlsRef.current?.bandwidthEstimate ?? 0) / 8000,
        totalFrames: total,
        droppedFrames: dropped,
        decodedFrames: total - dropped,
        droppedFrameRate: total > 0 ? (dropped / total) * 100 : 0,
      });
    }, 1000);
    return () => window.clearInterval(timer);
  }, [getStats, playbackEnabled, setStats]);

  return (
    <video
      ref={videoRef}
      className={className}
      playsInline
      muted={!audioEnabled}
      data-live-transport="hls"
    />
  );
}
