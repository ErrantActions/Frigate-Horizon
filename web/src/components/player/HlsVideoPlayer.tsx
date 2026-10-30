import {
  MutableRefObject,
  ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import Hls, { HlsConfig } from "hls.js";
import { isDesktop, isMobile } from "react-device-detect";
import { TransformComponent, TransformWrapper } from "react-zoom-pan-pinch";
import VideoControls from "./VideoControls";
import { VideoResolutionType } from "@/types/live";
import useSWR from "swr";
import { FrigateConfig } from "@/types/frigateConfig";
import { AxiosResponse } from "axios";
import { toast } from "sonner";
import { useOverlayState } from "@/hooks/use-overlay-state";
import { useUserPersistence } from "@/hooks/use-user-persistence";
import { cn } from "@/lib/utils";
import { ASPECT_VERTICAL_LAYOUT, RecordingPlayerError } from "@/types/record";
import { useTranslation } from "react-i18next";
import ObjectTrackOverlay from "@/components/overlay/ObjectTrackOverlay";
import { useIsAdmin } from "@/hooks/use-is-admin";
import {
  isHlsPlaylistUrl,
  playWithTemporaryMuteFallback,
} from "@/utils/videoUtil";
import {
  PlaybackPerformance,
  playbackBufferTarget,
  PLAYBACK_BUFFER_BYTES,
  PLAYBACK_BACK_BUFFER_SECONDS,
} from "./playbackPerformance";

// Android native hls does not seek correctly
const USE_NATIVE_HLS = false;
const HLS_MIME_TYPE = "application/vnd.apple.mpegurl" as const;
const unsupportedErrorCodes: number[] = [
  MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED,
  MediaError.MEDIA_ERR_DECODE,
];

export interface HlsSource {
  playlist: string;
  startPosition?: number;
}

type HlsVideoPlayerProps = {
  videoRef: MutableRefObject<HTMLVideoElement | null>;
  onVideoElementChange?: (element: HTMLVideoElement | null) => void;
  containerRef?: React.MutableRefObject<HTMLDivElement | null>;
  visible: boolean;
  currentSource: HlsSource;
  hotKeys: boolean;
  supportsFullscreen: boolean;
  fullscreen: boolean;
  frigateControls?: boolean;
  preview?: boolean;
  inpointOffset?: number;
  onClipEnded?: (currentTime: number) => void;
  onClipPrevious?: (diff: number) => void;
  onPlayerLoaded?: () => void;
  onSeeked?: () => void;
  onTimeUpdate?: (time: number) => void;
  onPlaying?: () => void;
  onSeekToTime?: (timestamp: number, play?: boolean) => void;
  setFullResolution?: React.Dispatch<React.SetStateAction<VideoResolutionType>>;
  onUploadFrame?: (playTime: number) => Promise<AxiosResponse> | undefined;
  getSnapshotUrl?: (playTime: number) => string | undefined;
  onSnapshot?: (playTime: number) => Promise<void> | void;
  toggleFullscreen?: () => void;
  onError?: (error: RecordingPlayerError) => void;
  onStallStart?: () => void;
  onStallEnd?: () => void;
  onSeekStart?: () => void;
  onBandwidthSample?: (estimateBps: number, levelBitrateBps?: number) => void;
  onFatalNetworkError?: () => boolean;
  onFatalCodecError?: () => boolean;
  onRequestCompatibility?: (reason?: "audio" | "video") => boolean;
  onAudioFallbackChange?: (sourcePlaylist: string | null) => void;
  onPlaybackFailure?: () => void;
  initialBandwidthEstimate?: number;
  bufferLength?: number;
  isDetailMode?: boolean;
  camera?: string;
  currentTimeOverride?: number;
  transformedOverlay?: ReactNode;
  suspendLoading?: boolean;
  onSuspensionChange?: (suspended: boolean) => void;
  playbackMode?: "original" | "compatibility";
  storageTier?: "local" | "archive";
  onPlayPauseIntent?: (play: boolean) => void;
  onRelativeSeek?: (diff: number) => void;
};

export default function HlsVideoPlayer({
  videoRef,
  onVideoElementChange,
  containerRef,
  visible,
  currentSource,
  hotKeys,
  supportsFullscreen,
  fullscreen,
  frigateControls = true,
  preview = false,
  inpointOffset = 0,
  onClipEnded,
  onClipPrevious,
  onPlayerLoaded,
  onSeeked,
  onTimeUpdate,
  onPlaying,
  onSeekToTime,
  setFullResolution,
  onUploadFrame,
  getSnapshotUrl,
  onSnapshot,
  toggleFullscreen,
  onError,
  onStallStart,
  onStallEnd,
  onSeekStart,
  onBandwidthSample,
  onFatalNetworkError,
  onFatalCodecError,
  onRequestCompatibility,
  onAudioFallbackChange,
  onPlaybackFailure,
  initialBandwidthEstimate,
  bufferLength,
  isDetailMode = false,
  camera,
  currentTimeOverride,
  transformedOverlay,
  suspendLoading = false,
  onSuspensionChange,
  playbackMode = "original",
  storageTier,
  onPlayPauseIntent,
  onRelativeSeek,
}: HlsVideoPlayerProps) {
  const { t } = useTranslation("components/player");
  const { data: config } = useSWR<FrigateConfig>("config");
  const isAdmin = useIsAdmin();

  // for detail stream context in History
  const currentTime = currentTimeOverride;

  // playback

  const hlsRef = useRef<Hls>(undefined);
  const measurements = useRef(new PlaybackPerformance());
  const stablePlaybackSince = useRef<number | undefined>(undefined);
  const frameCallback = useRef<number | undefined>(undefined);
  const dimensionFrame = useRef<number | undefined>(undefined);
  const [playInBackground, setPlayInBackground] = useUserPersistence(
    "recordingPlayInBackground",
    false,
  );
  const suspensionRef = useRef(false);
  const resumePlaybackRef = useRef(false);
  const suspensionCallback = useRef(onSuspensionChange);
  useEffect(() => {
    suspensionCallback.current = onSuspensionChange;
  }, [onSuspensionChange]);
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [diagnostics, setDiagnostics] = useState({
    ...measurements.current.snapshot(),
    bufferedSeconds: 0,
    targetSeconds: null as number | null,
    droppedFrames: null as number | null,
    totalFrames: null as number | null,
  });

  const queueFrameMeasurement = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    if (typeof video.requestVideoFrameCallback !== "function") {
      measurements.current.framePresented(video.currentTime);
      return;
    }
    if (frameCallback.current !== undefined) return;
    frameCallback.current = video.requestVideoFrameCallback(
      (_now, metadata) => {
        frameCallback.current = undefined;
        measurements.current.framePresented(metadata.mediaTime);
      },
    );
  }, [videoRef]);
  const [audioFallback, setAudioFallback] = useState<
    HlsSource & { sourcePlaylist: string }
  >();
  const audioFallbackAttemptRef = useRef<string>(undefined);
  const audioFallbackActive =
    audioFallback?.sourcePlaylist === currentSource.playlist;
  const audioFallbackChangeRef = useRef(onAudioFallbackChange);
  useEffect(() => {
    audioFallbackChangeRef.current = onAudioFallbackChange;
  }, [onAudioFallbackChange]);
  useEffect(() => {
    audioFallbackChangeRef.current?.(
      audioFallbackActive ? currentSource.playlist : null,
    );
  }, [audioFallbackActive, currentSource.playlist]);
  const activeSource =
    audioFallbackActive && audioFallback ? audioFallback : currentSource;
  const recoverUnsupportedAudioRef = useRef<
    (error: MediaError | null) => boolean
  >(() => false);
  const onPlaybackFailureRef = useRef(onPlaybackFailure);
  // kept in a ref so changing callback identities do not recreate the
  // Hls instance; the setup effect must only re-run on source changes
  const qualitySignalsRef = useRef({
    onStallStart,
    onStallEnd,
    onSeekStart,
    onBandwidthSample,
    onFatalNetworkError,
    onFatalCodecError,
    onRequestCompatibility,
    initialBandwidthEstimate,
  });
  // must resolve before the first render: a mount-effect flip would run
  // the first source effect in native mode, briefly handing iOS a native
  // HLS src that hls.js then tears away mid-load
  const [useHlsCompat, setUseHlsCompat] = useState(() => {
    if (
      USE_NATIVE_HLS &&
      document.createElement("video").canPlayType(HLS_MIME_TYPE)
    ) {
      return false;
    }
    return Hls.isSupported();
  });
  const [loadedMetadata, setLoadedMetadata] = useState(false);
  const bufferTimeout = useRef<ReturnType<typeof setTimeout> | undefined>(
    undefined,
  );
  // native HLS playback has no MSE, so it recovers from pipeline errors
  // by reloading the source; one attempt per source
  const nativeRetryRef = useRef(0);
  // a ref rather than an effect-scoped counter so the element error
  // handler can hold its toast while a recovery is still possible
  const mediaRecoveryBudgetRef = useRef(0);
  // hls.js and the element can both report one failure, so each source
  // toasts at most once
  const failureReportedRef = useRef(false);

  const reportPlaybackFailure = useCallback(
    (code: number, message: string) => {
      if (failureReportedRef.current) {
        return;
      }

      failureReportedRef.current = true;
      hlsRef.current?.stopLoad();
      onPlaybackFailureRef.current?.();
      toast.error(t("toast.error.playRecordingsFailed", { code, message }), {
        position: "top-center",
      });
    },
    [t],
  );
  const reportPlaybackFailureRef = useRef(reportPlaybackFailure);

  useEffect(() => {
    reportPlaybackFailureRef.current = reportPlaybackFailure;
    onPlaybackFailureRef.current = onPlaybackFailure;
  }, [reportPlaybackFailure, onPlaybackFailure]);

  const recoverUnsupportedAudio = useCallback(
    (error: MediaError | null) => {
      if (
        !error ||
        !unsupportedErrorCodes.includes(error.code) ||
        !/audio decoder.*(?:failed|unsupported|not supported)/i.test(
          error.message,
        ) ||
        audioFallbackActive ||
        !currentSource.playlist.includes("/vod/") ||
        !/\/master\.m3u8(?=[?#]|$)/.test(currentSource.playlist)
      ) {
        return false;
      }

      // The element and hls.js can report the same failure before React commits.
      if (audioFallbackAttemptRef.current === currentSource.playlist) {
        return true;
      }

      if (qualitySignalsRef.current.onRequestCompatibility?.("audio")) {
        hlsRef.current?.stopLoad();
        mediaRecoveryBudgetRef.current = 0;
        return true;
      }

      audioFallbackAttemptRef.current = currentSource.playlist;
      hlsRef.current?.stopLoad();
      mediaRecoveryBudgetRef.current = 0;
      const position = videoRef.current?.currentTime;
      setAudioFallback({
        sourcePlaylist: currentSource.playlist,
        playlist: currentSource.playlist.replace(
          /\/master\.m3u8(?=[?#]|$)/,
          "/index-v1.m3u8",
        ),
        startPosition:
          position !== undefined && Number.isFinite(position) && position > 0
            ? position
            : currentSource.startPosition,
      });
      toast.warning(t("audioFallback"), { position: "top-center" });
      return true;
    },
    [audioFallbackActive, currentSource, t, videoRef],
  );

  useEffect(() => {
    recoverUnsupportedAudioRef.current = recoverUnsupportedAudio;
  }, [recoverUnsupportedAudio]);

  // a quality switch changes the buffer length a commit before the new
  // source arrives, so it updates the live instance instead of rebuilding
  // it on the outgoing playlist
  const bufferLengthRef = useRef(bufferLength);

  const updateBufferPolicy = useCallback(() => {
    const hls = hlsRef.current,
      video = videoRef.current;
    if (!hls || !video) return;
    const saveData =
      (navigator as Navigator & { connection?: { saveData?: boolean } })
        .connection?.saveData === true;
    const target = playbackBufferTarget({
      stableSeconds:
        stablePlaybackSince.current === undefined
          ? 0
          : (performance.now() - stablePlaybackSince.current) / 1000,
      playbackRate: video.playbackRate,
      bitrate: hls.levels[0]?.bitrate,
      preferredLength: bufferLengthRef.current,
      saveData,
    });
    hls.config.maxBufferLength = target;
    hls.config.maxMaxBufferLength = target;
    video.dataset.playbackBufferTarget = target.toFixed(2);
  }, [videoRef]);

  useEffect(() => {
    bufferLengthRef.current = bufferLength;
    updateBufferPolicy();
  }, [bufferLength, updateBufferPolicy]);

  useEffect(() => {
    if (!diagnosticsOpen || !visible) return;
    const sample = () => {
      if (document.visibilityState === "hidden") return;
      const video = videoRef.current;
      let bufferedSeconds = 0;
      if (video) {
        for (let index = 0; index < video.buffered.length; index += 1) {
          if (
            video.buffered.start(index) <= video.currentTime &&
            video.currentTime <= video.buffered.end(index)
          ) {
            bufferedSeconds = video.buffered.end(index) - video.currentTime;
            break;
          }
        }
      }
      const quality = video?.getVideoPlaybackQuality?.();
      setDiagnostics({
        ...measurements.current.snapshot(),
        bufferedSeconds,
        targetSeconds: hlsRef.current?.config.maxBufferLength ?? null,
        droppedFrames: quality?.droppedVideoFrames ?? null,
        totalFrames: quality?.totalVideoFrames ?? null,
      });
    };
    sample();
    const timer = setInterval(sample, 1000);
    return () => clearInterval(timer);
  }, [diagnosticsOpen, videoRef, visible]);

  const applyVideoDimensions = useCallback(
    (width: number, height: number) => {
      if (setFullResolution) {
        setFullResolution({ width, height });
      }
      setVideoDimensions({ width, height });
      if (height > 0) {
        setTallCamera(width / height < ASPECT_VERTICAL_LAYOUT);
      }
    },
    [setFullResolution],
  );

  const handleLoadedMetadata = useCallback(() => {
    if (dimensionFrame.current !== undefined) {
      cancelAnimationFrame(dimensionFrame.current);
      dimensionFrame.current = undefined;
    }
    setLoadedMetadata(true);
    if (!videoRef.current) {
      return;
    }

    const video = videoRef.current;
    const width = video.videoWidth;
    const height = video.videoHeight;

    // iOS Safari occasionally reports 0x0 for videoWidth/videoHeight
    // Poll with requestAnimationFrame until dimensions become available (or timeout).
    if (width > 0 && height > 0) {
      applyVideoDimensions(width, height);
      return;
    }

    let attempts = 0;
    const maxAttempts = 120; // ~2 seconds at 60fps
    const tryGetDims = () => {
      dimensionFrame.current = undefined;
      if (videoRef.current !== video || !video.isConnected) return;
      const w = video.videoWidth;
      const h = video.videoHeight;
      if (w > 0 && h > 0) {
        applyVideoDimensions(w, h);
        return;
      }
      if (attempts < maxAttempts) {
        attempts += 1;
        dimensionFrame.current = requestAnimationFrame(tryGetDims);
      }
    };
    dimensionFrame.current = requestAnimationFrame(tryGetDims);
  }, [videoRef, applyVideoDimensions]);

  useEffect(
    () => () => {
      clearTimeout(bufferTimeout.current);
      bufferTimeout.current = undefined;
      if (dimensionFrame.current !== undefined) {
        cancelAnimationFrame(dimensionFrame.current);
        dimensionFrame.current = undefined;
      }
    },
    [activeSource],
  );

  useEffect(() => {
    qualitySignalsRef.current = {
      onStallStart,
      onStallEnd,
      onSeekStart,
      onBandwidthSample,
      onFatalNetworkError,
      onFatalCodecError,
      onRequestCompatibility,
      initialBandwidthEstimate,
    };
  }, [
    onStallStart,
    onStallEnd,
    onSeekStart,
    onBandwidthSample,
    onFatalNetworkError,
    onFatalCodecError,
    onRequestCompatibility,
    initialBandwidthEstimate,
  ]);

  useEffect(() => {
    if (!videoRef.current) {
      return;
    }

    if (!isHlsPlaylistUrl(activeSource.playlist)) {
      reportPlaybackFailureRef.current(
        MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED,
        t("hlsOnly"),
      );
      return;
    }

    // loadedMetadata is intentionally NOT reset here: on a source swap
    // the element already holds a decoded frame, and keeping it visible
    // bridges the gap while the new source loads
    const currentPlaybackRate = videoRef.current.playbackRate;
    const video = videoRef.current;
    const sourceMeasurements = measurements.current;
    measurements.current.sourceChanged();
    stablePlaybackSince.current = undefined;
    failureReportedRef.current = false;

    if (!useHlsCompat) {
      nativeRetryRef.current = 0;
      mediaRecoveryBudgetRef.current = 0;
      videoRef.current.src = activeSource.playlist;
      videoRef.current.load();
      return () => {
        if (frameCallback.current !== undefined) {
          video.cancelVideoFrameCallback?.(frameCallback.current);
          frameCallback.current = undefined;
        }
        sourceMeasurements.stallEnded();
      };
    }

    // Base HLS configuration
    const hlsConfig: Partial<HlsConfig> = {
      maxBufferLength: 6,
      maxMaxBufferLength: 6,
      backBufferLength: PLAYBACK_BACK_BUFFER_SECONDS,
      maxBufferSize: PLAYBACK_BUFFER_BYTES,
      autoStartLoad: !suspensionRef.current && !suspendLoading,
      startPosition: activeSource.startPosition,
    };

    // every quality switch and chunk change recreates the instance, so
    // seed it to keep measured throughput across source swaps
    const seedEstimate = qualitySignalsRef.current.initialBandwidthEstimate;
    if (seedEstimate !== undefined && seedEstimate > 0) {
      hlsConfig.abrEwmaDefaultEstimate = seedEstimate;
    }

    const hls = new Hls(hlsConfig);
    hlsRef.current = hls;
    let networkRecoveryAttempts = 0;
    mediaRecoveryBudgetRef.current = 1;
    hls.on(Hls.Events.ERROR, (_event, data) => {
      if (data.fatal) {
        if (
          recoverUnsupportedAudioRef.current(videoRef.current?.error ?? null)
        ) {
          return;
        }
        const isCodecError =
          data.details ===
            Hls.ErrorDetails.MANIFEST_INCOMPATIBLE_CODECS_ERROR ||
          data.details === Hls.ErrorDetails.BUFFER_INCOMPATIBLE_CODECS_ERROR ||
          data.details === Hls.ErrorDetails.BUFFER_ADD_CODEC_ERROR;
        if (isCodecError) {
          if (!qualitySignalsRef.current.onFatalCodecError?.()) {
            reportPlaybackFailureRef.current(
              MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED,
              data.details,
            );
          }
          return;
        }
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
          // prefer a quality downswitch; fall back to restarting loading
          const handled =
            qualitySignalsRef.current.onFatalNetworkError?.() ?? false;
          if (
            !handled &&
            hls.levels.length > 0 &&
            networkRecoveryAttempts < 2
          ) {
            networkRecoveryAttempts += 1;
            hls.startLoad();
          } else if (!handled) {
            reportPlaybackFailureRef.current(
              MediaError.MEDIA_ERR_NETWORK,
              data.details,
            );
          }
        } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
          if (mediaRecoveryBudgetRef.current > 0) {
            mediaRecoveryBudgetRef.current -= 1;
            hls.recoverMediaError();
          } else {
            reportPlaybackFailureRef.current(
              videoRef.current?.error?.code ?? MediaError.MEDIA_ERR_DECODE,
              videoRef.current?.error?.message || data.details,
            );
          }
        } else {
          reportPlaybackFailureRef.current(
            MediaError.MEDIA_ERR_DECODE,
            data.details,
          );
        }
        return;
      }

      // hls.js reports each stall episode only once, so STALL_RESOLVED
      // below is what closes it
      if (data.details === Hls.ErrorDetails.BUFFER_STALLED_ERROR) {
        if (!video.paused && !suspensionRef.current) {
          measurements.current.stallStarted();
          qualitySignalsRef.current.onStallStart?.();
        }
      }
    });
    hls.on(Hls.Events.STALL_RESOLVED, () => {
      measurements.current.stallEnded();
      qualitySignalsRef.current.onStallEnd?.();
    });
    hls.on(Hls.Events.FRAG_LOADED, () => {
      updateBufferPolicy();
      // manifests are single-variant, so the bitrate is always level 0
      qualitySignalsRef.current.onBandwidthSample?.(
        hls.bandwidthEstimate,
        hls.levels?.[0]?.bitrate || undefined,
      );
    });
    hls.attachMedia(videoRef.current);
    hls.loadSource(activeSource.playlist);
    videoRef.current.playbackRate = currentPlaybackRate;

    return () => {
      // we must destroy the hlsRef every time the source changes
      // so that we can create a new HLS instance with startPosition
      // set at the optimal point in time
      if (hlsRef.current) {
        hlsRef.current.destroy();
        hlsRef.current = undefined;
      }
      if (frameCallback.current !== undefined) {
        video.cancelVideoFrameCallback?.(frameCallback.current);
        frameCallback.current = undefined;
      }
    };
    // Suspend/resume is handled separately without recreating the media source.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [videoRef, hlsRef, useHlsCompat, activeSource]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const apply = () => {
      const pip =
        document.pictureInPictureElement === video ||
        ("webkitPresentationMode" in video &&
          video.webkitPresentationMode === "picture-in-picture");
      const hidden =
        document.visibilityState === "hidden" && !pip && !playInBackground;
      const suspended = suspendLoading || hidden;
      const wasSuspended = suspensionRef.current;
      suspensionRef.current = suspended;
      if (suspended) {
        if (!wasSuspended && hidden && !suspendLoading) {
          resumePlaybackRef.current = !video.paused;
        }
        if (suspendLoading) resumePlaybackRef.current = false;
        video.pause();
        hlsRef.current?.stopLoad();
        measurements.current.suspend();
        stablePlaybackSince.current = undefined;
        qualitySignalsRef.current.onStallEnd?.();
      } else {
        measurements.current.resume();
        if (wasSuspended) hlsRef.current?.startLoad();
        if (resumePlaybackRef.current) {
          resumePlaybackRef.current = false;
          void playWithTemporaryMuteFallback(
            video,
            () => !video.isConnected || suspensionRef.current,
          ).catch(() =>
            reportPlaybackFailureRef.current(
              MediaError.MEDIA_ERR_DECODE,
              t("playbackFailed"),
            ),
          );
        }
      }
      video.dataset.playbackSuspended = String(suspended);
      suspensionCallback.current?.(suspended);
    };
    apply();
    document.addEventListener("visibilitychange", apply);
    video.addEventListener("enterpictureinpicture", apply);
    video.addEventListener("leavepictureinpicture", apply);
    video.addEventListener("webkitpresentationmodechanged", apply);
    return () => {
      document.removeEventListener("visibilitychange", apply);
      video.removeEventListener("enterpictureinpicture", apply);
      video.removeEventListener("leavepictureinpicture", apply);
      video.removeEventListener("webkitpresentationmodechanged", apply);
    };
  }, [videoRef, suspendLoading, playInBackground, activeSource, t]);

  // state handling

  const onPlayPause = useCallback(
    (play: boolean) => {
      resumePlaybackRef.current = false;
      if (onPlayPauseIntent) {
        onPlayPauseIntent(play);
        return;
      }
      if (!videoRef.current) {
        return;
      }

      if (play) {
        const video = videoRef.current;
        void playWithTemporaryMuteFallback(
          video,
          () => !video.isConnected || suspensionRef.current,
        ).catch(() =>
          reportPlaybackFailureRef.current(
            MediaError.MEDIA_ERR_DECODE,
            t("playbackFailed"),
          ),
        );
      } else {
        videoRef.current.pause();
      }
    },
    [videoRef, t, onPlayPauseIntent],
  );

  // controls

  const [tallCamera, setTallCamera] = useState(false);
  const [isPlaying, setIsPlaying] = useState(true);
  const [persistedMuted, setPersistedMuted] = useUserPersistence(
    "hlsPlayerMuted",
    true,
  );
  const [temporaryMuted, setTemporaryMuted] = useState(false);
  const [volume, setVolume] = useOverlayState("playerVolume", 1.0);
  const [defaultPlaybackRate] = useUserPersistence("playbackRate", 1);
  const [playbackRate, setPlaybackRate] = useOverlayState(
    "playbackRate",
    defaultPlaybackRate ?? 1,
  );
  const [mobileCtrlTimeout, setMobileCtrlTimeout] = useState<NodeJS.Timeout>();
  const [controls, setControls] = useState(isMobile);
  const [controlsOpen, setControlsOpen] = useState(false);
  const [isSnapshotLoading, setIsSnapshotLoading] = useState(false);
  const [zoomScale, setZoomScale] = useState(1.0);
  const [videoDimensions, setVideoDimensions] = useState<{
    width: number;
    height: number;
  }>({ width: 0, height: 0 });

  const muted = preview || persistedMuted || temporaryMuted;

  const onSetMuted = useCallback(
    (muted: boolean) => {
      setTemporaryMuted(false);
      setPersistedMuted(muted);
    },
    [setPersistedMuted],
  );

  useEffect(() => {
    if (!isDesktop) {
      return;
    }

    const callback = (e: MouseEvent) => {
      if (!videoRef.current) {
        return;
      }

      const rect = videoRef.current.getBoundingClientRect();

      if (
        e.clientX > rect.left &&
        e.clientX < rect.right &&
        e.clientY > rect.top &&
        e.clientY < rect.bottom
      ) {
        setControls(true);
      } else {
        setControls(controlsOpen);
      }
    };
    window.addEventListener("mousemove", callback);
    return () => {
      window.removeEventListener("mousemove", callback);
    };
  }, [videoRef, controlsOpen]);

  const attachVideoRef = useCallback(
    (element: HTMLVideoElement | null) => {
      videoRef.current = element;
      onVideoElementChange?.(element);
    },
    [videoRef, onVideoElementChange],
  );

  const getVideoTime = useCallback(() => {
    const currentTime = videoRef.current?.currentTime;

    if (!currentTime) {
      return undefined;
    }

    return currentTime + inpointOffset;
  }, [videoRef, inpointOffset]);

  const handleSnapshot = useCallback(async () => {
    const frameTime = getVideoTime();

    if (!frameTime || !onSnapshot) {
      return;
    }

    setIsSnapshotLoading(true);
    try {
      await onSnapshot(frameTime);
    } finally {
      setIsSnapshotLoading(false);
    }
  }, [getVideoTime, onSnapshot]);

  return (
    <TransformWrapper
      minScale={1.0}
      wheel={{ smoothStep: 0.005 }}
      onZoom={(zoom) => setZoomScale(zoom.state.scale)}
      disabled={!frigateControls}
    >
      {frigateControls && (
        <VideoControls
          className={cn(
            "absolute left-1/2 z-50 -translate-x-1/2",
            tallCamera ? "bottom-12" : "bottom-5",
          )}
          video={videoRef.current}
          isPlaying={isPlaying}
          show={visible && (controls || controlsOpen)}
          muted={muted}
          volume={volume}
          features={{
            volume: !audioFallbackActive,
            seek: true,
            playbackRate: true,
            plusUpload: isAdmin && config?.plus?.enabled == true,
            snapshot: !!onSnapshot,
            fullscreen: supportsFullscreen,
          }}
          setControlsOpen={setControlsOpen}
          setMuted={onSetMuted}
          playbackRate={playbackRate ?? 1}
          hotKeys={hotKeys}
          onPlayPause={onPlayPause}
          onSeek={(diff) => {
            if (onRelativeSeek) {
              onRelativeSeek(diff);
              return;
            }
            const currentTime = videoRef.current?.currentTime;

            if (!videoRef.current || currentTime == undefined) {
              return;
            }

            const newTime = currentTime + diff;

            if (newTime < 0 && onClipPrevious) {
              onClipPrevious(diff);
            } else {
              videoRef.current.currentTime = Math.max(0, newTime);
            }
          }}
          onSetPlaybackRate={(rate) => {
            setPlaybackRate(rate, true);

            if (videoRef.current) {
              videoRef.current.playbackRate = rate;
            }
          }}
          getSnapshotUrl={() => {
            const frameTime = getVideoTime();
            if (!frameTime || !getSnapshotUrl) {
              return undefined;
            }
            return getSnapshotUrl(frameTime);
          }}
          onUploadFrame={async () => {
            const frameTime = getVideoTime();

            if (frameTime && onUploadFrame) {
              const resp = await onUploadFrame(frameTime);

              if (resp && resp.status == 200) {
                toast.success(t("toast.success.submittedFrigatePlus"), {
                  position: "top-center",
                });
              } else {
                toast.success(t("toast.error.submitFrigatePlusFailed"), {
                  position: "top-center",
                });
              }
            }
          }}
          onSnapshot={onSnapshot ? handleSnapshot : undefined}
          snapshotLoading={isSnapshotLoading}
          fullscreen={fullscreen}
          toggleFullscreen={toggleFullscreen}
          containerRef={containerRef}
        />
      )}
      {frigateControls && visible && (
        <details
          open={diagnosticsOpen}
          className="absolute left-2 top-2 z-50 max-h-[min(65%,calc(100%_-_5rem))] max-w-[90%] overflow-y-auto rounded bg-background/90 p-2 text-xs text-foreground"
          onClick={(event) => event.stopPropagation()}
          onKeyDown={(event) => {
            if (event.key === " " || event.key === "Enter")
              event.stopPropagation();
          }}
          onKeyUp={(event) => {
            if (event.key === " " || event.key === "Enter")
              event.stopPropagation();
          }}
          onToggle={(event) => setDiagnosticsOpen(event.currentTarget.open)}
        >
          <summary className="cursor-pointer">{t("diagnostics.title")}</summary>
          <div className="mt-2 space-y-1" role="status" aria-live="off">
            <p>
              {t("diagnostics.mode", {
                mode: t(`diagnostics.modes.${playbackMode}`),
              })}
            </p>
            <p>
              {t("diagnostics.tier", {
                tier: t(`diagnostics.tiers.${storageTier ?? "unknown"}`),
              })}
            </p>
            <p>
              {t("diagnostics.firstFrame", {
                value:
                  diagnostics.firstFrameMs == null
                    ? t("diagnostics.pending")
                    : t("diagnostics.ms", {
                        value: Math.round(diagnostics.firstFrameMs),
                      }),
              })}
            </p>
            <p>
              {t("diagnostics.seek", {
                value:
                  diagnostics.lastSeekMs == null
                    ? t("diagnostics.pending")
                    : t("diagnostics.ms", {
                        value: Math.round(diagnostics.lastSeekMs),
                      }),
              })}
            </p>
            <p>
              {t("diagnostics.stalls", {
                count: diagnostics.stalls,
                seconds: (diagnostics.stalledMs / 1000).toFixed(1),
              })}
            </p>
            <p>
              {t("diagnostics.buffer", {
                seconds: diagnostics.bufferedSeconds.toFixed(1),
                target:
                  diagnostics.targetSeconds == null
                    ? t("diagnostics.pending")
                    : t("diagnostics.seconds", {
                        value: diagnostics.targetSeconds.toFixed(1),
                      }),
              })}
            </p>
            <p>
              {t("diagnostics.frames", {
                dropped: diagnostics.droppedFrames ?? t("diagnostics.pending"),
                total: diagnostics.totalFrames ?? t("diagnostics.pending"),
              })}
            </p>
            <p>{t("diagnostics.loads", { count: diagnostics.sourceLoads })}</p>
            <p className="max-w-72 text-muted-foreground">
              {t("diagnostics.timingHint")}
            </p>
            <label className="flex items-center gap-2 pt-2">
              <input
                type="checkbox"
                checked={playInBackground ?? false}
                onChange={(event) => setPlayInBackground(event.target.checked)}
              />
              {t("diagnostics.background")}
            </label>
          </div>
        </details>
      )}
      <TransformComponent
        wrapperStyle={{
          display: visible ? undefined : "none",
          width: "100%",
          height: "100%",
        }}
        wrapperProps={{
          onClick: isDesktop ? undefined : () => setControls(!controls),
        }}
        contentStyle={{
          width: "100%",
          height: isMobile ? "100%" : undefined,
        }}
      >
        <div className="relative size-full">
          {audioFallbackActive && !onAudioFallbackChange && (
            <div
              role="status"
              className="absolute inset-x-2 top-2 z-50 rounded bg-background/90 p-2 text-xs"
            >
              {t("audioFallback")}
            </div>
          )}
          {transformedOverlay}
          {isDetailMode &&
            camera &&
            currentTime != null &&
            loadedMetadata &&
            videoDimensions.width > 0 &&
            videoDimensions.height > 0 && (
              <div
                className={cn(
                  "absolute inset-0 z-50",
                  isDesktop
                    ? "size-full"
                    : "mx-auto flex items-center justify-center portrait:max-h-[50dvh]",
                )}
                style={{
                  aspectRatio: `${videoDimensions.width} / ${videoDimensions.height}`,
                }}
              >
                <ObjectTrackOverlay
                  camera={camera}
                  showBoundingBoxes={!isPlaying}
                  currentTime={currentTime}
                  videoWidth={videoDimensions.width}
                  videoHeight={videoDimensions.height}
                  className="absolute inset-0 z-10"
                  onSeekToTime={(timestamp, play) => {
                    if (onSeekToTime) {
                      onSeekToTime(timestamp, play);
                    }
                  }}
                />
              </div>
            )}
          <video
            ref={attachVideoRef}
            className={`size-full rounded-lg bg-black md:rounded-2xl ${loadedMetadata ? "" : "invisible"} cursor-pointer`}
            preload="auto"
            autoPlay={!preview}
            controls={!frigateControls && !preview}
            playsInline
            muted={muted}
            onClick={
              isDesktop && !preview
                ? () => {
                    if (zoomScale == 1.0) onPlayPause(!isPlaying);
                  }
                : undefined
            }
            onVolumeChange={() => {
              if (!videoRef.current || preview) {
                return;
              }

              setVolume(videoRef.current.volume ?? 1.0, true);

              if (frigateControls) {
                if (videoRef.current.muted && !persistedMuted) {
                  setTemporaryMuted(true);
                } else if (!videoRef.current.muted && temporaryMuted) {
                  setTemporaryMuted(false);
                }
              } else {
                setPersistedMuted(videoRef.current.muted);
              }
            }}
            onPlay={() => {
              setIsPlaying(true);

              if (isMobile) {
                setControls(true);
                setMobileCtrlTimeout(
                  setTimeout(() => setControls(false), 4000),
                );
              }
            }}
            onPlaying={() => {
              if (suspensionRef.current) {
                videoRef.current?.pause();
                return;
              }
              stablePlaybackSince.current ??= performance.now();
              measurements.current.stallEnded();
              queueFrameMeasurement();
              updateBufferPolicy();
              qualitySignalsRef.current.onStallEnd?.();
              onPlaying?.();
            }}
            onPause={() => {
              stablePlaybackSince.current = undefined;
              measurements.current.stallEnded();
              setIsPlaying(false);
              clearTimeout(bufferTimeout.current);
              bufferTimeout.current = undefined;

              // paused time must never count as stall time
              qualitySignalsRef.current.onStallEnd?.();

              if (isMobile && mobileCtrlTimeout) {
                clearTimeout(mobileCtrlTimeout);
              }
            }}
            onSeeking={() => {
              measurements.current.seekStarted(
                videoRef.current?.currentTime ?? 0,
              );
              stablePlaybackSince.current = undefined;
              updateBufferPolicy();
              queueFrameMeasurement();
              // iOS ManagedMediaSource gates hls.js fragment loading off
              // while paused and never resumes it on seek, so a seek
              // into unbuffered media would never complete
              if (!suspensionRef.current) hlsRef.current?.resumeBuffering();
              qualitySignalsRef.current.onSeekStart?.();
            }}
            onWaiting={() => {
              if (videoRef.current?.paused || suspensionRef.current) {
                return;
              }

              // the only stall signal under native HLS playback, which
              // emits no hls.js events
              qualitySignalsRef.current.onStallStart?.();
              measurements.current.stallStarted();
              stablePlaybackSince.current = undefined;

              if (onError != undefined) {
                clearTimeout(bufferTimeout.current);
                bufferTimeout.current = setTimeout(() => {
                  if (
                    document.visibilityState === "visible" &&
                    videoRef.current
                  ) {
                    onError("stalled");
                  }
                }, 3000);
              }
            }}
            onProgress={() => {
              if (onError != undefined) {
                if (videoRef.current?.paused) {
                  return;
                }

                if (bufferTimeout.current) {
                  clearTimeout(bufferTimeout.current);
                  bufferTimeout.current = undefined;
                }
              }
            }}
            onTimeUpdate={() => {
              updateBufferPolicy();
              if (!onTimeUpdate) {
                return;
              }

              const frameTime = getVideoTime();

              if (frameTime) {
                onTimeUpdate(frameTime);
              }
            }}
            onLoadedData={() => {
              queueFrameMeasurement();
              onPlayerLoaded?.();
              handleLoadedMetadata();

              if (videoRef.current) {
                if (playbackRate) {
                  videoRef.current.playbackRate = playbackRate;
                }

                if (volume) {
                  videoRef.current.volume = volume;
                }
              }
            }}
            onSeeked={() => {
              queueFrameMeasurement();
              onSeeked?.();
            }}
            onRateChange={updateBufferPolicy}
            onEnded={() => {
              if (onClipEnded) {
                onClipEnded(getVideoTime() ?? 0);
              }
            }}
            onError={(e) => {
              const mediaError = (e.target as HTMLVideoElement).error;

              if (!mediaError) {
                return;
              }

              // an intentional source swap aborts the in-flight load;
              // that abort is not an error the user can act on
              if (mediaError.code === MediaError.MEDIA_ERR_ABORTED) {
                return;
              }

              if (recoverUnsupportedAudio(mediaError)) {
                return;
              }

              if (
                hlsRef.current &&
                mediaError.code === MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED
              ) {
                if (!qualitySignalsRef.current.onFatalCodecError?.()) {
                  reportPlaybackFailure(mediaError.code, mediaError.message);
                }
                return;
              }

              // hold the toast while the fatal handler still has a retry
              // left; a failed recovery raises a second element error
              if (hlsRef.current && mediaRecoveryBudgetRef.current > 0) {
                return;
              }

              if (!hlsRef.current && videoRef.current) {
                if (
                  unsupportedErrorCodes.includes(mediaError.code) &&
                  Hls.isSupported()
                ) {
                  setLoadedMetadata(false);
                  setUseHlsCompat(true);
                  return;
                }

                // native pipeline errors around source swaps are usually
                // transient, and hls.js is no fallback without MSE
                if (nativeRetryRef.current < 1) {
                  nativeRetryRef.current += 1;
                  videoRef.current.load();
                  return;
                }
              }

              reportPlaybackFailure(mediaError.code, mediaError.message);
            }}
          />
        </div>
      </TransformComponent>
    </TransformWrapper>
  );
}
