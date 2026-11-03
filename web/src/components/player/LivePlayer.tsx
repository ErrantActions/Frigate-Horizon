import { CameraConfig, FrigateConfig } from "@/types/frigateConfig";
import AutoUpdatingCameraImage from "../camera/AutoUpdatingCameraImage";
import ActivityIndicator from "../indicators/activity-indicator";
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useResizeObserver } from "@/hooks/resize-observer";
import { MdCircle } from "react-icons/md";
import { Tooltip, TooltipContent, TooltipTrigger } from "../ui/tooltip";
import { useCameraActivity } from "@/hooks/use-camera-activity";
import { PlayerStatsType, VideoResolutionType } from "@/types/live";
import { getIconForLabel } from "@/utils/iconUtil";
import Chip from "../indicators/Chip";
import { cn } from "@/lib/utils";
import { TbExclamationCircle } from "react-icons/tb";
import { TooltipPortal } from "@radix-ui/react-tooltip";
import { baseUrl } from "@/api/baseUrl";
import { PlayerStats } from "./PlayerStats";
import { LuVideoOff } from "react-icons/lu";
import { Trans, useTranslation } from "react-i18next";
import { useCameraFriendlyName } from "@/hooks/use-camera-friendly-name";
import { ImageShadowOverlay } from "../overlay/ImageShadowOverlay";
import { getTranslatedLabel } from "@/utils/i18n";
import { formatList } from "@/utils/stringUtil";
import useSWR from "swr";
import { Button } from "../ui/button";
import type { LiveHlsStatus } from "@/types/liveHls";
import type { LiveWebRTCStatus } from "@/types/liveWebrtc";
import { toast } from "sonner";

const LiveHlsPlayer = lazy(() => import("./LiveHlsPlayer"));
const LiveWebRTCPlayer = lazy(() => import("./LiveWebRTCPlayer"));

type LivePlayerProps = {
  cameraRef?: (ref: HTMLDivElement | null) => void;
  containerRef?: React.MutableRefObject<HTMLDivElement | null>;
  className?: string;
  cameraConfig: CameraConfig;
  streamName: string;
  showStillWithoutActivity?: boolean;
  alwaysShowCameraName?: boolean;
  windowVisible?: boolean;
  playAudio?: boolean;
  volume?: number;
  playInBackground: boolean;
  pip?: boolean;
  autoLive?: boolean;
  showStats?: boolean;
  onClick?: () => void;
  setFullResolution?: React.Dispatch<React.SetStateAction<VideoResolutionType>>;
  streamAuto?: boolean;
  onLiveAspectChange?: (aspectRatio: number | undefined) => void;
  adaptive?: boolean;
  onStreamChange?: (camera: string, stream: string) => void;
  dataSaver?: boolean;
};

export default function LivePlayer({
  cameraRef = undefined,
  containerRef,
  className,
  cameraConfig,
  streamName: requestedStreamName,
  showStillWithoutActivity: requestedShowStillWithoutActivity = true,
  alwaysShowCameraName = false,
  windowVisible = true,
  playAudio = false,
  volume,
  playInBackground = false,
  pip,
  autoLive = true,
  showStats = false,
  onClick,
  setFullResolution,
  streamAuto = false,
  onLiveAspectChange,
  adaptive = false,
  onStreamChange,
  dataSaver = false,
}: LivePlayerProps) {
  const { t } = useTranslation(["components/player"]);
  const { data: config } = useSWR<FrigateConfig>("config");
  const webRtcEnabled = config?.live_webrtc?.enabled === true;
  const overlayRef = useRef<HTMLDivElement | null>(null);
  const [overlayDimensions] = useResizeObserver(overlayRef);
  const inDashboard = containerRef === undefined;
  const adaptiveSelected = adaptive || streamAuto;
  const [hlsFailed, setHlsFailed] = useState(false);
  const [hlsStreamName, setHlsStreamName] = useState<string>();
  const failHls = useCallback(() => {
    setHlsFailed(true);
    toast.error(
      webRtcEnabled
        ? t("adaptive.webrtcUnavailable")
        : t("adaptive.hlsUnavailable"),
      {
        id: `live-hls-${cameraConfig.name}`,
      },
    );
  }, [cameraConfig.name, t, webRtcEnabled]);
  const {
    data: hlsStatus,
    isLoading: hlsStatusLoading,
    mutate: reloadHlsStatus,
  } = useSWR<LiveHlsStatus | LiveWebRTCStatus>(
    config && !hlsFailed
      ? `${encodeURIComponent(cameraConfig.name)}/live/${webRtcEnabled ? "webrtc" : "hls"}/status`
      : null,
    {
      revalidateOnFocus: false,
      shouldRetryOnError: false,
      onError: failHls,
    },
  );
  const webRtcDetail = webRtcEnabled && (!inDashboard || pip === true);
  const advertisedRenditions = hlsStatus?.renditions ?? [];
  const renditions = webRtcDetail
    ? advertisedRenditions.filter((rendition) => rendition.height >= 720)
    : advertisedRenditions;
  const hlsActive =
    !hlsFailed && hlsStatus?.available === true && renditions.length > 0;
  const hlsPending = !hlsFailed && (!config || hlsStatusLoading);
  const hlsUnavailable =
    hlsFailed ||
    (!hlsStatusLoading && hlsStatus?.available === false) ||
    (hlsStatus?.available === true && renditions.length === 0);
  const preferredHeight =
    dataSaver || (inDashboard && pip !== true)
      ? renditions[0]?.height
      : adaptiveSelected
        ? undefined
        : Number(/_transcode_(\d+)p$/.exec(requestedStreamName)?.[1]) ||
          renditions.at(-1)?.height;
  const playingStreamName =
    hlsActive && hlsStatus
      ? (hlsStreamName ??
        `${cameraConfig.name}_transcode_${renditions[0].height}p`)
      : requestedStreamName;

  const internalContainerRef = useRef<HTMLDivElement | null>(null);

  const cameraName = useCameraFriendlyName(cameraConfig);

  const isCompact =
    overlayDimensions.width > 0 && overlayDimensions.width < 280;

  // stats

  const [stats, setStats] = useState<PlayerStatsType>({
    streamType: "-",
    bandwidth: 0, // in kBps
    bandwidthMeasured: false,
    totalFrames: 0,
    droppedFrames: undefined,
    decodedFrames: 0,
    droppedFrameRate: 0, // percentage
  });

  const streamLabel = useMemo(
    () =>
      Object.keys(cameraConfig.live.streams).find(
        (label) => cameraConfig.live.streams[label] === playingStreamName,
      ),
    [cameraConfig.live.streams, playingStreamName],
  );

  // camera activity

  const {
    enabled: cameraEnabled,
    activeMotion,
    activeTracking,
    objects,
    offline,
    recordingOnly,
  } = useCameraActivity(cameraConfig);

  const showStillWithoutActivity =
    requestedShowStillWithoutActivity && !recordingOnly;

  const cameraActive = useMemo(
    () =>
      windowVisible &&
      (!showStillWithoutActivity || activeMotion || activeTracking),
    [activeMotion, activeTracking, showStillWithoutActivity, windowVisible],
  );

  // camera live state

  const [key, setKey] = useState(0);
  const [liveReady, setLiveReady] = useState(false);
  const [liveAspect, setLiveAspect] = useState<number | undefined>();

  const handleFullResolution = useCallback(
    (value: React.SetStateAction<VideoResolutionType>) => {
      setFullResolution?.(value);

      if (typeof value === "function") {
        return;
      }

      setLiveAspect(
        value.width && value.height ? value.width / value.height : undefined,
      );
    },
    [setFullResolution],
  );

  useEffect(() => {
    onLiveAspectChange?.(liveReady ? liveAspect : undefined);
  }, [liveReady, liveAspect, onLiveAspectChange]);

  // The card can be a different shape than the picture (a bucketed tile, or a
  // still whose detect aspect differs from the stream), so overlays that are
  // meant to sit on the image have to be fitted to it rather than to the card.
  const pictureAspect = useMemo(() => {
    if (liveReady && liveAspect) {
      return liveAspect;
    }
    const { width, height } = cameraConfig.detect;
    return width && height ? width / height : 16 / 9;
  }, [liveReady, liveAspect, cameraConfig.detect]);

  const liveReadyRef = useRef(liveReady);
  const cameraActiveRef = useRef(cameraActive);

  useEffect(() => {
    liveReadyRef.current = liveReady;
    cameraActiveRef.current = cameraActive;
  }, [liveReady, cameraActive]);

  useEffect(() => {
    if (!autoLive || !liveReady) {
      return;
    }

    if (!cameraActive) {
      const timer = setTimeout(() => {
        if (liveReadyRef.current && !cameraActiveRef.current) {
          setLiveReady(false);
        }
      }, 500);

      return () => {
        clearTimeout(timer);
      };
    }
    // live mode won't change
  }, [autoLive, cameraActive, liveReady]);

  // camera still state

  const stillReloadInterval = useMemo(() => {
    if (!windowVisible || offline || !showStillWithoutActivity) {
      return -1; // no reason to update the image when the window is not visible
    }

    if (liveReady && !cameraActive) {
      return 300;
    }

    if (liveReady) {
      return 60000;
    }

    if (activeMotion || activeTracking) {
      if (autoLive) {
        return 200;
      } else {
        return 59000;
      }
    }

    return 30000;
  }, [
    autoLive,
    showStillWithoutActivity,
    liveReady,
    activeMotion,
    activeTracking,
    offline,
    windowVisible,
    cameraActive,
  ]);

  useEffect(() => {
    setLiveReady(false);
    setHlsStreamName(undefined);
  }, [hlsActive, hlsPending]);

  useEffect(() => {
    if (showStillWithoutActivity && !autoLive) {
      setLiveReady(false);
    }
  }, [showStillWithoutActivity, autoLive]);

  const playerIsPlaying = useCallback(() => {
    setLiveReady(true);
  }, []);

  // enabled states

  const [isReEnabling, setIsReEnabling] = useState(false);
  const prevCameraEnabledRef = useRef(cameraEnabled ?? true);

  useEffect(() => {
    if (cameraEnabled == undefined) {
      return;
    }
    if (!prevCameraEnabledRef.current && cameraEnabled) {
      // Camera enabled
      setLiveReady(false);
      setIsReEnabling(true);
      setKey((prevKey) => prevKey + 1);
    } else if (prevCameraEnabledRef.current && !cameraEnabled) {
      // Camera disabled
      setLiveReady(false);
      setKey((prevKey) => prevKey + 1);
    }
    prevCameraEnabledRef.current = cameraEnabled;
  }, [cameraEnabled]);

  useEffect(() => {
    if (liveReady && isReEnabling) {
      setIsReEnabling(false);
    }
  }, [liveReady, isReEnabling]);

  if (!cameraConfig) {
    return <ActivityIndicator />;
  }

  let player;
  const StreamingPlayer = webRtcEnabled ? LiveWebRTCPlayer : LiveHlsPlayer;
  if (!autoLive || !cameraEnabled) {
    player = null;
  } else if (hlsPending) {
    player = <ActivityIndicator className="w-full" />;
  } else if (hlsActive && hlsStatus) {
    player = (
      <Suspense fallback={<ActivityIndicator className="w-full" />}>
        <StreamingPlayer
          key={`hls_${cameraConfig.name}_${key}`}
          camera={cameraConfig.name}
          dashboard={inDashboard && pip !== true}
          renditions={renditions}
          preferredHeight={preferredHeight}
          maximumHeight={
            inDashboard && !webRtcDetail
              ? overlayDimensions.height *
                Math.min(2, Math.max(1, window.devicePixelRatio))
              : Math.max(...renditions.map((value) => value.height))
          }
          className={`size-full ${liveReady ? "" : "hidden"}`}
          playbackEnabled={
            (windowVisible || playInBackground || pip === true) &&
            (cameraActive || liveReady)
          }
          audioEnabled={playAudio}
          volume={volume}
          pip={pip}
          getStats={showStats}
          setStats={setStats}
          onPlaying={playerIsPlaying}
          onError={failHls}
          onResolution={handleFullResolution}
          onLevelChange={(height) => {
            const name = `${cameraConfig.name}_transcode_${height}p`;
            setHlsStreamName(name);
            onStreamChange?.(cameraConfig.name, name);
          }}
        />
      </Suspense>
    );
  } else {
    player = null;
  }

  return (
    <div
      ref={(node) => {
        overlayRef.current = node;
        if (cameraRef) {
          cameraRef(node);
        } else {
          internalContainerRef.current = node;
        }
      }}
      data-camera={cameraConfig.name}
      data-live-stream={playingStreamName}
      data-live-auto={streamAuto || adaptiveSelected}
      style={hlsUnavailable ? { aspectRatio: pictureAspect } : undefined}
      className={cn(
        // the card owns the corner: overflow-hidden clips the stream, the still
        // image, and every overlay to this one radius so they stay concentric
        "relative flex w-full cursor-pointer justify-center overflow-hidden rounded-lg outline md:rounded-2xl",
        activeTracking &&
          ((showStillWithoutActivity && !liveReady) || liveReady)
          ? "shadow-severity_alert outline-[3px] outline-severity_alert"
          : "outline-0 outline-background",
        "transition-all duration-500",
        className,
      )}
      onClick={onClick}
      onAuxClick={(e) => {
        if (e.button === 1) {
          window.open(`${baseUrl}#${cameraConfig.name}`, "_blank")?.focus();
        }
      }}
    >
      {cameraEnabled &&
        ((showStillWithoutActivity && !liveReady) || liveReady) && (
          <div
            className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center [container-type:size]"
            style={{ "--pic-ar": pictureAspect } as React.CSSProperties}
          >
            <div className="relative aspect-[var(--pic-ar)] h-auto w-[min(100%,calc(100cqh*var(--pic-ar)))]">
              <ImageShadowOverlay />
            </div>
          </div>
        )}
      {player}
      {hlsUnavailable && (
        <div
          role="alert"
          className="absolute inset-0 z-40 flex flex-col items-center justify-center gap-3 bg-background/90 p-4 text-center"
        >
          <p>
            {webRtcEnabled
              ? t("adaptive.webrtcUnavailable")
              : t("adaptive.hlsUnavailable")}
          </p>
          <Button
            onClick={(event) => {
              event.stopPropagation();
              setHlsFailed(false);
              setLiveReady(false);
              void reloadHlsStatus();
              setKey((value) => value + 1);
            }}
          >
            {t("compatibility.retry")}
          </Button>
        </div>
      )}
      {cameraEnabled &&
        !offline &&
        !hlsUnavailable &&
        (!showStillWithoutActivity || isReEnabling) &&
        !liveReady && (
          <div className="absolute inset-0 flex items-center justify-center">
            <ActivityIndicator className={"w-full [.bg-black_&]:text-white"} />
          </div>
        )}

      {((showStillWithoutActivity && !liveReady) || liveReady) &&
        objects.length > 0 && (
          <div className="absolute left-0 top-2 z-40">
            <Tooltip>
              <div className="flex">
                <TooltipTrigger asChild>
                  <div className="mx-3 pb-1 text-sm text-white">
                    <Chip
                      className={`z-0 flex items-start justify-between space-x-1 bg-gray-500 bg-gradient-to-br from-gray-400 to-gray-500`}
                    >
                      {[
                        ...new Set([
                          ...(objects || []).map(({ label }) => label),
                        ]),
                      ]
                        .map((label) => {
                          return getIconForLabel(
                            label,
                            "object",
                            "size-3 text-white",
                          );
                        })
                        .sort()}
                    </Chip>
                  </div>
                </TooltipTrigger>
              </div>
              <TooltipPortal>
                <TooltipContent>
                  {formatList(
                    [
                      ...new Set(
                        (objects || [])
                          .map(({ label, sub_label }) => {
                            const isManual = label.endsWith("verified");
                            const text = isManual ? sub_label : label;
                            const type = isManual ? "manual" : "object";
                            return getTranslatedLabel(text, type);
                          })
                          .filter(
                            (translated) =>
                              translated && !translated.includes("-verified"),
                          ),
                      ),
                    ].sort(),
                  )}
                </TooltipContent>
              </TooltipPortal>
            </Tooltip>
          </div>
        )}

      {!recordingOnly && (
        <div
          className={cn(
            "absolute inset-0 w-full",
            showStillWithoutActivity &&
              !liveReady &&
              !isReEnabling &&
              cameraEnabled
              ? "visible"
              : "invisible",
          )}
        >
          <AutoUpdatingCameraImage
            className="pointer-events-none size-full"
            cameraClasses="relative size-full flex justify-center"
            camera={cameraConfig.name}
            showFps={false}
            reloadInterval={stillReloadInterval}
            periodicCache
          />
        </div>
      )}

      {offline && inDashboard && (
        <>
          <div className="absolute inset-0 bg-black/50" />
          <div className="absolute inset-0 left-1/2 top-1/2 flex -translate-x-1/2 -translate-y-1/2 items-center justify-center">
            <div className="flex flex-col items-center justify-center gap-2 rounded-lg bg-background/50 p-3 text-center">
              <div>{t("streamOffline.title")}</div>
              <TbExclamationCircle className="size-6" />
              {!isCompact && (
                <p className="text-center text-sm">
                  <Trans
                    ns="components/player"
                    values={{
                      cameraName: cameraName,
                    }}
                  >
                    streamOffline.desc
                  </Trans>
                </p>
              )}
            </div>
          </div>
        </>
      )}

      {offline && !showStillWithoutActivity && cameraEnabled && (
        <div className="absolute inset-0 left-1/2 top-1/2 flex h-96 w-96 -translate-x-1/2 -translate-y-1/2">
          <div className="flex flex-col items-center justify-center rounded-lg bg-background/50 p-5">
            <p className="my-5 text-lg">{t("streamOffline.title")}</p>
            <TbExclamationCircle className="mb-3 size-10" />
            {!isCompact && (
              <p className="max-w-96 text-center">
                <Trans
                  ns="components/player"
                  values={{
                    cameraName: cameraName,
                  }}
                >
                  streamOffline.desc
                </Trans>
              </p>
            )}
          </div>
        </div>
      )}

      {!cameraEnabled && (
        <div className="relative flex h-full w-full items-center justify-center border border-secondary-foreground bg-background_alt">
          <div className="flex h-32 flex-col items-center justify-center rounded-lg p-4 md:h-48 md:w-48">
            <LuVideoOff className="mb-2 size-8 md:size-10" />
            <p className="max-w-32 text-center text-sm md:max-w-40 md:text-base">
              {t("cameraOff")}
            </p>
          </div>
        </div>
      )}

      <div className="absolute right-2 top-2 flex items-center gap-3">
        {(alwaysShowCameraName ||
          (offline && showStillWithoutActivity) ||
          !cameraEnabled) && (
          <Chip
            className={`z-0 flex items-start justify-between space-x-1 bg-gray-500 bg-gradient-to-br from-gray-400 to-gray-500 text-xs capitalize`}
          >
            {cameraName}
          </Chip>
        )}
        {autoLive &&
          !offline &&
          activeMotion &&
          ((showStillWithoutActivity && !liveReady) || liveReady) && (
            <MdCircle className="mr-2 size-2 animate-pulse text-danger shadow-danger drop-shadow-md" />
          )}
      </div>
      {showStats && (
        <PlayerStats
          stats={stats}
          minimal={cameraRef !== undefined}
          streamLabel={streamLabel}
          streamAuto={streamAuto || adaptiveSelected}
        />
      )}
    </div>
  );
}
