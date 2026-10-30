import { BirdseyeConfig } from "@/types/frigateConfig";
import type { LiveHlsStatus } from "@/types/liveHls";
import ActivityIndicator from "../indicators/activity-indicator";
import { cn } from "@/lib/utils";
import React, { useEffect, useState } from "react";
import { ImageShadowOverlay } from "../overlay/ImageShadowOverlay";
import LiveHlsPlayer from "./LiveHlsPlayer";
import useSWR from "swr";
import { useTranslation } from "react-i18next";
import { Button } from "../ui/button";

type Props = {
  className?: string;
  birdseyeConfig: BirdseyeConfig;
  pip?: boolean;
  containerRef: React.MutableRefObject<HTMLDivElement | null>;
  playerRef?: React.MutableRefObject<HTMLDivElement | null>;
  onClick?: () => void;
};

export default function BirdseyeHlsPlayer({
  className,
  birdseyeConfig,
  pip,
  containerRef,
  playerRef,
  onClick,
}: Props) {
  const { t } = useTranslation("components/player");
  const [failed, setFailed] = useState(false);
  const [ready, setReady] = useState(false);
  const [generation, setGeneration] = useState(0);
  const [visible, setVisible] = useState(
    document.visibilityState === "visible",
  );
  const enabled = birdseyeConfig.enabled && birdseyeConfig.restream;
  const {
    data: status,
    isLoading,
    mutate,
  } = useSWR<LiveHlsStatus>(enabled ? "birdseye/live/hls/status" : null, {
    revalidateOnFocus: false,
    shouldRetryOnError: false,
    onError: () => setFailed(true),
  });
  useEffect(() => {
    const update = () => setVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  const unavailable = !enabled || failed || status?.available === false;

  return (
    <div
      ref={containerRef}
      className={cn(
        "relative flex w-full cursor-pointer justify-center overflow-hidden rounded-lg md:rounded-2xl",
        className,
      )}
      onClick={onClick}
    >
      <div
        className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center [container-type:size]"
        style={
          {
            "--pic-ar": birdseyeConfig.width / birdseyeConfig.height,
          } as React.CSSProperties
        }
      >
        <div className="relative aspect-[var(--pic-ar)] h-auto w-[min(100%,calc(100cqh*var(--pic-ar)))]">
          <ImageShadowOverlay />
        </div>
      </div>
      <div className="size-full" ref={playerRef}>
        {!unavailable && status?.available && (
          <LiveHlsPlayer
            key={generation}
            camera="birdseye"
            renditions={status.renditions}
            maximumHeight={birdseyeConfig.height}
            className={cn("size-full", !ready && "invisible")}
            playbackEnabled={visible || pip === true}
            audioEnabled={false}
            pip={pip}
            setStats={() => {}}
            onPlaying={() => setReady(true)}
            onError={() => setFailed(true)}
            onResolution={() => {}}
          />
        )}
        {unavailable ? (
          <div
            role="alert"
            className="flex size-full flex-col items-center justify-center gap-3 p-4 text-center"
          >
            <p>{t("adaptive.birdseyeUnavailable")}</p>
            {enabled && (
              <Button
                onClick={(event) => {
                  event.stopPropagation();
                  setFailed(false);
                  setReady(false);
                  setGeneration((value) => value + 1);
                  void mutate();
                }}
              >
                {t("compatibility.retry")}
              </Button>
            )}
          </div>
        ) : (
          (isLoading || !ready) && (
            <ActivityIndicator className="absolute inset-0 m-auto" />
          )
        )}
      </div>
    </div>
  );
}
