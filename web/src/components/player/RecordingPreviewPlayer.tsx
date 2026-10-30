import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useApiHost } from "@/api";
import { useRecordingCoverage } from "@/hooks/use-recording-coverage";
import { TimeRange } from "@/types/timeline";
import {
  calculateInpointOffset,
  calculateSeekPosition,
} from "@/utils/videoUtil";
import { cn } from "@/lib/utils";
import { Skeleton } from "../ui/skeleton";
import HlsVideoPlayer from "./HlsVideoPlayer";
import { PreviewController } from "./PreviewController";

type RecordingPreviewPlayerProps = {
  previewRef?: (ref: HTMLDivElement | null) => void;
  className?: string;
  camera: string;
  timeRange: TimeRange;
  startTime?: number;
  isVisible: boolean;
  onControllerReady: (controller: PreviewController) => void;
  onClick?: () => void;
};

export default function RecordingPreviewPlayer({
  previewRef,
  className,
  camera,
  timeRange,
  startTime,
  isVisible,
  onControllerReady,
  onClick,
}: RecordingPreviewPlayerProps) {
  const { t } = useTranslation("components/player");
  const apiHost = useApiHost();
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [timestamp, setTimestamp] = useState(startTime ?? timeRange.after);
  const [windowStart, setWindowStart] = useState(() =>
    Math.max(
      timeRange.after,
      Math.floor((startTime ?? timeRange.after) / 2) * 2,
    ),
  );
  const windowEnd = Math.min(timeRange.before, windowStart + 60);
  const sourceAfter =
    timestamp < windowStart ||
    timestamp >= windowEnd ||
    windowStart < timeRange.after ||
    windowStart >= timeRange.before
      ? Math.max(timeRange.after, Math.floor(timestamp / 2) * 2)
      : windowStart;
  if (sourceAfter !== windowStart) setWindowStart(sourceAfter);
  const sourceBefore = Math.min(timeRange.before, sourceAfter + 60);
  const targetRef = useRef(timestamp);
  targetRef.current = timestamp;
  const [loadedPlaylist, setLoadedPlaylist] = useState<string>();
  const [failedPlaylist, setFailedPlaylist] = useState<string>();
  const { data: coverage, error } = useRecordingCoverage(
    camera,
    sourceAfter,
    sourceBefore,
    isVisible,
  );
  const stream = coverage?.timelines?.sub.some(
    (span) => span.start_time <= timestamp && timestamp < span.end_time,
  )
    ? "sub"
    : "auto";
  const recordings = useMemo(
    () =>
      coverage?.timelines?.[stream].map((span) => ({
        start_time: span.start_time,
        end_time: span.end_time,
        duration: span.duration / 1000,
        lead_in:
          span.lead_in_ms === undefined ? undefined : span.lead_in_ms / 1000,
      })),
    [coverage, stream],
  );
  const playlist =
    `${apiHost}vod/${camera}${stream === "sub" ? "/sub" : ""}` +
    `/start/${sourceAfter}/end/${sourceBefore}/index-v1.m3u8`;
  const offset = calculateInpointOffset(sourceAfter, recordings?.[0]);
  const hasRecording = recordings?.some(
    (recording) =>
      recording.start_time <= timestamp && timestamp < recording.end_time,
  );
  const source = useMemo(
    () => ({
      playlist,
      startPosition: recordings
        ? calculateSeekPosition(targetRef.current, recordings, offset)
        : undefined,
    }),
    [playlist, recordings, offset],
  );
  const synchronize = useCallback(() => {
    const video = videoRef.current;
    if (!video || !recordings || video.readyState < 1) return;
    video.pause();
    if (
      video.seeking ||
      !recordings.some(
        (span) =>
          span.start_time <= targetRef.current &&
          targetRef.current < span.end_time,
      )
    )
      return;
    const position = calculateSeekPosition(
      targetRef.current,
      recordings,
      offset,
    );
    if (position !== undefined && Math.abs(video.currentTime - position) > 0.05)
      video.currentTime = position;
  }, [recordings, offset]);
  const controller = useMemo(
    () => new RecordingPreviewController(camera, setTimestamp),
    [camera],
  );
  const readyRef = useRef(onControllerReady);
  readyRef.current = onControllerReady;
  useEffect(() => {
    readyRef.current(controller);
  }, [controller]);
  useEffect(() => {
    synchronize();
  }, [timestamp, synchronize, isVisible]);

  return (
    <div
      ref={previewRef}
      data-camera={camera}
      data-testid="recording-preview"
      data-timestamp={timestamp}
      className={cn(
        "relative flex size-full justify-center overflow-hidden rounded-lg bg-black md:rounded-2xl",
        onClick && "cursor-pointer",
        className,
      )}
      onClick={onClick}
    >
      {isVisible && recordings && recordings.length > 0 && (
        <HlsVideoPlayer
          videoRef={videoRef}
          currentSource={source}
          visible={!!hasRecording}
          frigateControls={false}
          preview
          hotKeys={false}
          supportsFullscreen={false}
          fullscreen={false}
          bufferLength={2}
          onPlayerLoaded={() => {
            setLoadedPlaylist(playlist);
            synchronize();
          }}
          onSeeked={synchronize}
          onPlaybackFailure={() => setFailedPlaylist(playlist)}
        />
      )}
      {(error || failedPlaylist === playlist) && (
        <div
          role="alert"
          className="absolute inset-0 bg-background_alt p-2 text-center text-xs"
        >
          {t("playbackFailed")}
        </div>
      )}
      {!error && recordings && !hasRecording && (
        <div
          role="status"
          className="absolute inset-0 flex items-center justify-center bg-background_alt p-2 text-center text-xs"
        >
          {t("noRecordingsFoundForThisTime")}
        </div>
      )}
      {!error &&
        failedPlaylist !== playlist &&
        (!recordings || (hasRecording && loadedPlaylist !== playlist)) && (
          <Skeleton className="absolute inset-0" />
        )}
    </div>
  );
}

class RecordingPreviewController extends PreviewController {
  constructor(
    camera: string,
    private seek: (timestamp: number) => void,
  ) {
    super(camera);
  }

  scrubToTimestamp(timestamp: number) {
    this.seek(timestamp);
    return true;
  }

  finishedSeeking() {}

  setNewPreviewStartTime(timestamp: number) {
    this.seek(timestamp);
  }
}
