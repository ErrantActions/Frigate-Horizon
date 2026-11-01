import { baseUrl } from "@/api/baseUrl";
import type { Preview } from "@/types/preview";
import {
  isHlsPlaylistUrl,
  playWithTemporaryMuteFallback,
} from "@/utils/videoUtil";
import Hls from "hls.js";
import { MutableRefObject, useEffect, useState } from "react";
import useSWR from "swr";
import type { FrigateConfig } from "@/types/frigateConfig";

export function previewHlsUrl(preview: Preview): string {
  return new URL(
    `vod/preview/${encodeURIComponent(preview.camera)}/start/${preview.start}/end/${preview.end}/index.m3u8`,
    baseUrl,
  ).href;
}

export function useHlsPreview(
  videoRef: MutableRefObject<HTMLVideoElement | null>,
  playlist: string | undefined,
  enabled: boolean,
  startPosition?: number,
  previewFile?: string,
) {
  const { data: config } = useSWR<FrigateConfig>("config");
  const direct = config?.live_webrtc?.enabled === true;
  const configLoaded = config !== undefined;
  const source = direct
    ? previewFile
      ? new URL(previewFile.replace(/^\/+/, ""), baseUrl).href
      : undefined
    : playlist;
  const [failedPlaylist, setFailedPlaylist] = useState<string>();
  useEffect(() => {
    const video = videoRef.current;
    if (!configLoaded || !video || !source || !enabled) return;
    let active = true;
    let hls: Hls | undefined;
    const fail = () => {
      if (active) {
        hls?.stopLoad();
        video.pause();
        setFailedPlaylist(source);
      }
    };
    const play = () => {
      if (startPosition !== undefined) {
        video.currentTime = startPosition;
      }
      if (video.autoplay) {
        void playWithTemporaryMuteFallback(video, () => !active).catch(fail);
      }
    };
    setFailedPlaylist(undefined);
    if (!direct && !isHlsPlaylistUrl(source)) {
      fail();
      return;
    }
    video.addEventListener("error", fail);
    video.addEventListener("loadedmetadata", play);
    if (direct) {
      video.src = source;
      video.load();
    } else if (Hls.isSupported()) {
      hls = new Hls({
        startPosition,
        maxBufferLength: 6,
        maxMaxBufferLength: 12,
        backBufferLength: 0,
        xhrSetup: (xhr) => {
          xhr.withCredentials = true;
        },
      });
      hls.on(Hls.Events.MEDIA_ATTACHED, () => hls?.loadSource(source));
      hls.on(Hls.Events.MANIFEST_PARSED, play);
      hls.on(Hls.Events.ERROR, (_event, error) => {
        if (error.fatal) fail();
      });
      hls.attachMedia(video);
    } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = source;
      video.load();
    } else {
      fail();
    }
    return () => {
      active = false;
      video.removeEventListener("error", fail);
      video.removeEventListener("loadedmetadata", play);
      hls?.destroy();
      video.pause();
      video.removeAttribute("src");
      video.load();
    };
  }, [enabled, source, startPosition, videoRef, direct, configLoaded]);

  return source !== undefined && failedPlaylist === source;
}
