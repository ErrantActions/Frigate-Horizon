import ActivityIndicator from "@/components/indicators/activity-indicator";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useIsAdmin } from "@/hooks/use-is-admin";
import { cn } from "@/lib/utils";
import type { FrigateConfig } from "@/types/frigateConfig";
import type { RecordingCoverage } from "@/types/record";
import type {
  RecordedHTTPDiscovery,
  RecordedHTTPQuality,
  RecordedHTTPResolution,
  RecordedHTTPSession,
} from "@/types/recordedHttp";
import {
  recordedHttpEndpoint as endpoint,
  recordingCatalogTime,
  recordingMediaTime,
} from "@/utils/recordedHttp";
import {
  downloadSnapshot,
  generateSnapshotFilename,
  grabVideoSnapshot,
} from "@/utils/snapshotUtil";
import { playWithTemporaryMuteFallback } from "@/utils/videoUtil";
import axios from "axios";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { TransformComponent, TransformWrapper } from "react-zoom-pan-pinch";
import { toast } from "sonner";
import useSWR from "swr";
import PreviewPlayer from "../PreviewPlayer";
import type { PreviewController } from "../PreviewController";
import VideoControls from "../VideoControls";
import {
  DynamicVideoController,
  type RemoteRecordingPlayback,
} from "./DynamicVideoController";
import type { DynamicVideoPlayerProps } from "./DynamicVideoPlayer";

export default function RecordedHTTPPlayer(props: DynamicVideoPlayerProps) {
  const { t } = useTranslation("components/player");
  const isAdmin = useIsAdmin();
  const { data: config } = useSWR<FrigateConfig>("config");
  const { data: coverage } = useSWR<RecordingCoverage>([
    `${props.camera}/recordings/coverage`,
    { after: props.timeRange.after, before: props.timeRange.before },
  ]);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [preview, setPreview] = useState<PreviewController | null>(null);
  const previewRef = useRef(preview);
  previewRef.current = preview;
  const hasPreview = preview !== null;
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [empty, setEmpty] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(true);
  const [volume, setVolume] = useState(1);
  const [rate, setRate] = useState(1);
  const [retry, setRetry] = useState(0);
  const [quality, setQuality] = useState<RecordedHTTPQuality>("auto");
  const [qualities, setQualities] = useState<RecordedHTTPQuality[]>([]);
  const callbacks = useRef(props);
  callbacks.current = props;
  const coverageRef = useRef(coverage);
  coverageRef.current = coverage;
  const selected = useRef(quality);
  selected.current = quality;
  const session = useRef<RecordedHTTPSession | undefined>(undefined);
  const timestamp = useRef(props.startTimestamp ?? props.timeRange.after);
  const requested = useRef(timestamp.current);
  const anchor = useRef(timestamp.current);
  const wanted = useRef(true);
  const wasScrubbing = useRef(props.isScrubbing);
  const priorRange = useRef(props.timeRange);
  const transport = useRef<RemoteRecordingPlayback | undefined>(undefined);
  const changeQuality = useRef<(() => void) | undefined>(undefined);
  const mutedRef = useRef(muted);
  mutedRef.current = muted;

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    let active = true;
    let broken = false;
    let inViewport = false;
    let revision = 0;
    let preparing = false;
    let discover: RecordedHTTPDiscovery | undefined;
    const saveData =
      (navigator as Navigator & { connection?: { saveData?: boolean } })
        .connection?.saveData ?? false;
    let expiry: ReturnType<typeof setTimeout> | undefined;
    let pollAbort: AbortController | undefined;
    let nextAbort: AbortController | undefined;
    let nextSession: RecordedHTTPSession | undefined;
    let nextRevision = 0;
    let nextAttempted = false;
    let nextExhausted = false;
    let nextPromise: Promise<RecordedHTTPSession | undefined> | undefined;
    let prepareSeconds = 6;
    let releasing = Promise.resolve(true);
    const visible = () =>
      (document.visibilityState === "visible" && inViewport) ||
      document.pictureInPictureElement === video;
    const request = {
      withCredentials: true,
      timeout: 15000,
      headers: { "X-CSRF-TOKEN": "1" },
    };
    const release = (current: RecordedHTTPSession) => {
      const abort = new AbortController();
      const timeout = setTimeout(() => abort.abort(), 10000);
      return fetch(endpoint(current.close_url), {
        method: "DELETE",
        credentials: "include",
        keepalive: true,
        headers: { "X-CSRF-TOKEN": "1" },
        signal: abort.signal,
      })
        .then((response) => {
          if (
            !response.ok &&
            response.status !== 404 &&
            response.status !== 410
          )
            toast.warning(t("recordedHttp.cleanupPending"));
          return (
            response.ok || response.status === 404 || response.status === 410
          );
        })
        .catch(() => {
          toast.warning(t("recordedHttp.cleanupPending"));
          return false;
        })
        .finally(() => clearTimeout(timeout));
    };
    const scheduleRelease = (current: RecordedHTTPSession) => {
      releasing = Promise.all([releasing, release(current)]).then((results) =>
        results.every(Boolean),
      );
    };
    const cancelNext = () => {
      nextRevision += 1;
      nextAbort?.abort();
      nextPromise = undefined;
      nextAttempted = false;
      nextExhausted = false;
      if (nextSession) scheduleRelease(nextSession);
      nextSession = undefined;
    };
    const nativePause = () => {
      if (!video.ended) cancelNext();
    };
    const stop = () => {
      revision += 1;
      preparing = false;
      clearTimeout(expiry);
      pollAbort?.abort();
      cancelNext();
      video.pause();
      video.removeAttribute("src");
      video.load();
      setPlaying(false);
      const current = session.current;
      session.current = undefined;
      if (current) scheduleRelease(current);
      return releasing;
    };
    const fail = () => {
      if (!active || broken) return;
      broken = true;
      stop();
      setLoading(false);
      setFailed(true);
    };
    const play = () => {
      wanted.current = true;
      if (!session.current || session.current.state !== "ready") return;
      const currentRevision = revision;
      void playWithTemporaryMuteFallback(
        video,
        () => !active || currentRevision !== revision || !wanted.current,
      ).catch(() => {
        if (active && currentRevision === revision) fail();
      });
    };
    const nextTarget = (current: RecordedHTTPSession) => {
      return current.next_timestamp < callbacks.current.timeRange.before
        ? current.next_timestamp
        : undefined;
    };
    const activate = (current: RecordedHTTPSession, target: number) => {
      if (current.hard_expires_at * 1000 <= Date.now()) {
        scheduleRelease(current);
        fail();
        return;
      }
      session.current = current;
      requested.current = target;
      timestamp.current = target;
      video.muted = mutedRef.current;
      video.src = endpoint(current.media_url);
      video.load();
      clearTimeout(expiry);
      expiry = setTimeout(fail, current.hard_expires_at * 1000 - Date.now());
    };
    const position = () => {
      const current = session.current;
      if (!current || video.readyState < 2 || video.seeking) return;
      const value = recordingCatalogTime(current.timeline, video.currentTime);
      if (value !== undefined) {
        timestamp.current = value;
        callbacks.current.onTimestampUpdate?.(value);
      }
      const last = current.timeline.at(-1);
      if (
        last &&
        wanted.current &&
        !video.paused &&
        visible() &&
        !callbacks.current.isScrubbing &&
        !nextAttempted &&
        (last.media_end - video.currentTime) / video.playbackRate <=
          Math.max(6, prepareSeconds + 3)
      ) {
        const target = nextTarget(current);
        if (target !== undefined) {
          nextAttempted = true;
          nextPromise = load(target, true);
        }
      }
    };
    const hasRecording = (target: number) =>
      coverageRef.current?.spans.some(
        (span) => span.start_time <= target && target < span.end_time,
      ) ?? false;
    const load = async (
      target: number,
      upcoming = false,
    ): Promise<RecordedHTTPSession | undefined> => {
      if (!active || broken || !visible() || callbacks.current.isScrubbing)
        return;
      const bounds = callbacks.current.timeRange;
      if (target < bounds.after || target >= bounds.before) {
        video.pause();
        setLoading(true);
        return;
      }
      const released = upcoming ? releasing : stop();
      const currentRevision = revision;
      const upcomingRevision = nextRevision;
      const started = performance.now();
      if (!upcoming) {
        preparing = true;
        setLoading(true);
        setFailed(false);
        setEmpty(false);
      }
      const abort = new AbortController();
      if (upcoming) nextAbort = abort;
      else pollAbort = abort;
      const obsolete = () =>
        !active ||
        broken ||
        currentRevision !== revision ||
        !visible() ||
        (upcoming && (upcomingRevision !== nextRevision || !wanted.current));
      const remember = (current: RecordedHTTPSession) => {
        if (upcoming) nextSession = current;
        else session.current = current;
      };
      try {
        if (!(await released))
          throw new Error("Previous recording cleanup is pending");
        if (obsolete()) return;
        if (!discover) {
          const response = await axios.get<RecordedHTTPDiscovery>(
            endpoint(
              `${encodeURIComponent(props.camera)}/recordings/http/status`,
            ),
            request,
          );
          if (obsolete()) return;
          discover = response.data;
          if (
            discover.version !== 1 ||
            !discover.enabled ||
            !discover.available ||
            discover.transport !== "https_mp4" ||
            discover.seek !== "http_byte_range" ||
            typeof discover.resolve_url !== "string" ||
            !discover.qualities.includes(selected.current)
          )
            throw new Error("HTTPS recorded viewing is unavailable");
          setQualities(
            saveData
              ? discover.qualities.filter(
                  (item) => item === "auto" || item === "360" || item === "720",
                )
              : discover.qualities,
          );
        }
        const range = callbacks.current.timeRange;
        const searchAfter = Math.max(range.after, target);
        const searchBefore = Math.min(range.before, searchAfter + 3600);
        if (!(searchAfter <= target && target < searchBefore))
          throw new Error("Recording target is outside the selected range");
        let cursor = searchAfter;
        let resolution: RecordedHTTPResolution | undefined;
        for (let attempt = 0; attempt < 4; attempt += 1) {
          const response = await axios.post<RecordedHTTPResolution>(
            endpoint(discover.resolve_url),
            {
              version: 1,
              after: cursor,
              before: searchBefore,
              start_timestamp: cursor,
            },
            { ...request, signal: abort.signal },
          );
          if (obsolete()) return;
          resolution = response.data;
          if (
            resolution.effective_start_timestamp !== null ||
            resolution.exhausted
          )
            break;
          if (
            !Number.isFinite(resolution.searched_before) ||
            resolution.searched_before <= cursor ||
            resolution.searched_before >= searchBefore
          )
            throw new Error("Recording target search made no progress");
          cursor = resolution.searched_before;
        }
        if (!resolution)
          throw new Error("Recording target resolution is unavailable");
        if (resolution.effective_start_timestamp === null) {
          if (!resolution.exhausted)
            throw new Error("Recording target resolution reached its limit");
          if (upcoming) nextExhausted = true;
          else {
            setEmpty(true);
            fail();
          }
          return;
        }
        const resolvedTarget = resolution.effective_start_timestamp;
        if (
          !Number.isFinite(resolvedTarget) ||
          resolvedTarget < cursor ||
          resolvedTarget >= searchBefore
        )
          throw new Error("Resolved recording target is outside its search");
        target = resolvedTarget;
        const windowSeconds = Math.min(discover.max_window_seconds, 20);
        const after = Math.max(range.after, upcoming ? target : target - 5);
        const before = Math.min(range.before, after + windowSeconds);
        if (!(after <= target && target < before))
          throw new Error("Recording target is outside the viewing window");
        const requestedQuality =
          selected.current === "auto" && saveData
            ? discover.qualities.includes("720")
              ? "720"
              : discover.qualities.includes("360")
                ? "360"
                : undefined
            : selected.current;
        if (!requestedQuality)
          throw new Error("Data Saver recording quality is unavailable");
        const created = await axios.post<RecordedHTTPSession>(
          endpoint(discover.session_url),
          {
            version: 1,
            after,
            before,
            start_timestamp: target,
            start_policy: "next",
            quality: requestedQuality,
          },
          request,
        );
        if (obsolete()) {
          release(created.data);
          return;
        }
        remember(created.data);
        const deadline = Date.now() + 60000;
        let current = created.data;
        while (current.state === "preparing") {
          if (Date.now() >= deadline)
            throw new Error("MP4 preparation timed out");
          await new Promise<void>((resolve) => {
            const signal = abort.signal;
            const finish = () => {
              clearTimeout(timer);
              signal.removeEventListener("abort", finish);
              resolve();
            };
            const timer = setTimeout(finish, 250);
            signal.addEventListener("abort", finish, { once: true });
          });
          if (obsolete()) return;
          const status = await axios.get<RecordedHTTPSession>(
            endpoint(current.status_url),
            { ...request, signal: abort.signal },
          );
          if (obsolete()) return;
          current = status.data;
          remember(current);
        }
        if (current.state === "empty") {
          if (upcoming) {
            scheduleRelease(current);
            nextSession = undefined;
            nextExhausted = true;
          } else {
            setEmpty(true);
            fail();
          }
          return;
        }
        if (current.state !== "ready")
          throw new Error("MP4 preparation failed");
        if (
          current.effective_start_timestamp === null ||
          !Number.isFinite(current.effective_start_timestamp)
        )
          throw new Error("Prepared recording has no effective frame target");
        target = current.effective_start_timestamp;
        const mediaTime = recordingMediaTime(current.timeline, target);
        if (
          mediaTime === undefined ||
          current.hard_expires_at * 1000 <= Date.now()
        )
          throw new Error(
            "Prepared recording cannot serve the requested position",
          );
        prepareSeconds = (performance.now() - started) / 1000;
        if (!upcoming) activate(current, target);
        return current;
      } catch (error: unknown) {
        if (!obsolete() && !axios.isCancel(error)) {
          if (upcoming) {
            if (nextSession) scheduleRelease(nextSession);
            nextSession = undefined;
            toast.warning(t("recordedHttp.continuationFailed"));
          } else fail();
        }
      } finally {
        if (!upcoming && currentRevision === revision) preparing = false;
      }
    };
    const seek = (target: number, shouldPlay: boolean) => {
      cancelNext();
      wanted.current = shouldPlay;
      anchor.current = target;
      requested.current = target;
      const mediaTime =
        session.current?.state === "ready"
          ? recordingMediaTime(session.current.timeline, target)
          : undefined;
      if (mediaTime !== undefined && video.readyState >= 1) {
        video.pause();
        video.currentTime = mediaTime;
        timestamp.current = target;
        callbacks.current.onTimestampUpdate?.(target);
        if (shouldPlay) play();
      } else {
        void load(target);
      }
    };
    const loaded = () => {
      const current = session.current;
      if (!current || current.state !== "ready") return;
      const mediaTime = recordingMediaTime(current.timeline, requested.current);
      if (mediaTime === undefined) {
        fail();
        return;
      }
      video.currentTime = mediaTime;
      callbacks.current.setFullResolution({
        width: video.videoWidth,
        height: video.videoHeight,
      });
      if (wanted.current) play();
    };
    const decoded = () => {
      setLoading(false);
      position();
    };
    const ended = async () => {
      const current = session.current;
      if (!current || !wanted.current) return;
      const target = nextTarget(current);
      if (target !== undefined) {
        const currentRevision = revision;
        setLoading(true);
        const prepared =
          nextSession?.state === "ready" ? nextSession : await nextPromise;
        if (
          !active ||
          revision !== currentRevision ||
          !wanted.current ||
          !visible()
        )
          return;
        if (nextExhausted) {
          wanted.current = false;
          setLoading(false);
          callbacks.current.onClipEnded?.();
          return;
        }
        if (prepared) {
          nextSession = undefined;
          nextPromise = undefined;
          nextAttempted = false;
          revision += 1;
          scheduleRelease(current);
          if (prepared.effective_start_timestamp === null) {
            scheduleRelease(prepared);
            fail();
            return;
          }
          activate(prepared, prepared.effective_start_timestamp);
        } else {
          requested.current = target;
          void load(target);
        }
      } else {
        wanted.current = false;
        callbacks.current.onClipEnded?.();
      }
    };
    const visibility = () => {
      if (!active) return;
      if (!visible()) {
        requested.current = timestamp.current;
        stop();
      } else if (
        !callbacks.current.isScrubbing &&
        !session.current &&
        !preparing
      ) {
        void load(requested.current);
      }
    };
    transport.current = {
      play: () => {
        wanted.current = true;
        if (session.current) play();
        else if (!preparing) void load(requested.current);
      },
      pause: () => {
        wanted.current = false;
        cancelNext();
        video.pause();
      },
      seek,
      timestamp: () => timestamp.current,
      isPlaying: () => !video.paused && !video.ended,
      hasRecording,
    };
    changeQuality.current = () => {
      requested.current = timestamp.current;
      void load(requested.current);
    };
    const observer = new IntersectionObserver(([entry]) => {
      if (!entry || inViewport === entry.isIntersecting) return;
      inViewport = entry.isIntersecting;
      visibility();
    });
    const pagehide = () => stop();
    const pageshow = (event: PageTransitionEvent) => {
      if (event.persisted) setRetry((value) => value + 1);
    };
    observer.observe(video);
    video.addEventListener("loadedmetadata", loaded);
    video.addEventListener("loadeddata", decoded);
    video.addEventListener("seeked", decoded);
    video.addEventListener("timeupdate", position);
    video.addEventListener("ended", ended);
    video.addEventListener("pause", nativePause);
    video.addEventListener("error", fail);
    video.addEventListener("enterpictureinpicture", visibility);
    video.addEventListener("leavepictureinpicture", visibility);
    document.addEventListener("visibilitychange", visibility);
    window.addEventListener("pagehide", pagehide);
    window.addEventListener("pageshow", pageshow);
    setFailed(false);
    setLoading(true);
    return () => {
      active = false;
      stop();
      observer.disconnect();
      video.removeEventListener("loadedmetadata", loaded);
      video.removeEventListener("loadeddata", decoded);
      video.removeEventListener("seeked", decoded);
      video.removeEventListener("timeupdate", position);
      video.removeEventListener("ended", ended);
      video.removeEventListener("pause", nativePause);
      video.removeEventListener("error", fail);
      video.removeEventListener("enterpictureinpicture", visibility);
      video.removeEventListener("leavepictureinpicture", visibility);
      document.removeEventListener("visibilitychange", visibility);
      window.removeEventListener("pagehide", pagehide);
      window.removeEventListener("pageshow", pageshow);
      transport.current = undefined;
      changeQuality.current = undefined;
    };
  }, [props.camera, retry, t]);

  const controller = useMemo(() => {
    const video = videoRef.current;
    if (!video || !hasPreview) return undefined;
    const bridge: RemoteRecordingPlayback = {
      play: () => transport.current?.play(),
      pause: () => transport.current?.pause(),
      seek: (target, play) => transport.current?.seek(target, play),
      timestamp: () => timestamp.current,
      isPlaying: () => transport.current?.isPlaying() ?? false,
      hasRecording: (target) =>
        transport.current?.hasRecording(target) ?? false,
    };
    return new DynamicVideoController(
      props.camera,
      video,
      {
        scrubToTimestamp: (target) =>
          previewRef.current?.scrubToTimestamp(target) ?? false,
        setNewPreviewStartTime: (target) =>
          previewRef.current?.setNewPreviewStartTime(target),
      },
      0,
      "playback",
      () => {},
      () => {},
      () => toast.error(t("recordedHttp.failed")),
      bridge,
    );
  }, [props.camera, hasPreview, t]);
  useEffect(() => {
    if (controller) callbacks.current.onControllerReady(controller);
    return () => controller?.dispose();
  }, [controller]);
  useEffect(() => {
    if (!controller) return;
    const rangeChanged =
      priorRange.current.after !== props.timeRange.after ||
      priorRange.current.before !== props.timeRange.before;
    priorRange.current = {
      after: props.timeRange.after,
      before: props.timeRange.before,
    };
    const scrubEnded = wasScrubbing.current && !props.isScrubbing;
    wasScrubbing.current = props.isScrubbing;
    if (props.isScrubbing) {
      controller.scrubToTimestamp(
        props.startTimestamp ?? timestamp.current,
        true,
      );
    } else if (
      scrubEnded ||
      rangeChanged ||
      (props.startTimestamp !== undefined &&
        props.startTimestamp !== anchor.current)
    ) {
      controller.seekToTimestamp(
        props.startTimestamp ?? requested.current,
        controller.wantsPlayback(),
      );
    }
  }, [
    controller,
    props.isScrubbing,
    props.startTimestamp,
    props.timeRange.after,
    props.timeRange.before,
  ]);

  return (
    <div className="relative size-full" data-recorded-transport="https_mp4">
      {!props.isScrubbing && !failed && (
        <Select
          value={quality}
          disabled={loading}
          onValueChange={(value) => {
            const option = qualities.find((item) => item === value);
            if (!option) {
              toast.error(t("recordedHttp.failed"));
              return;
            }
            selected.current = option;
            setQuality(option);
            changeQuality.current?.();
          }}
        >
          <SelectTrigger
            aria-label={t("recordedHttp.qualityLabel")}
            className="absolute right-2 top-2 z-50 w-auto bg-background/80"
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {qualities.map((item) => (
              <SelectItem key={item} value={item}>
                {item === "auto"
                  ? t("quality.auto")
                  : item === "original"
                    ? t("recordedHttp.original")
                    : t("recordedWebrtc.height", { height: item })}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
      <TransformWrapper minScale={1}>
        <VideoControls
          className="absolute bottom-5 left-1/2 z-50 -translate-x-1/2"
          video={videoRef.current}
          isPlaying={playing}
          show={!props.isScrubbing && !failed}
          muted={muted}
          volume={volume}
          playbackRate={rate}
          hotKeys={props.hotKeys}
          features={{
            volume: true,
            seek: true,
            playbackRate: true,
            snapshot: true,
            plusUpload: isAdmin && config?.plus?.enabled === true,
            fullscreen: props.supportsFullscreen,
          }}
          setMuted={(value) => {
            setMuted(value);
            if (videoRef.current) videoRef.current.muted = value;
          }}
          onPlayPause={(play) =>
            play ? controller?.play() : controller?.pause()
          }
          onSeek={(difference) => {
            const target = controller?.relativeSeekTimestamp(difference);
            if (target !== undefined)
              callbacks.current.onSeekToTime(
                target,
                controller?.wantsPlayback(),
              );
          }}
          onSetPlaybackRate={(value) => {
            setRate(value);
            if (videoRef.current) {
              videoRef.current.defaultPlaybackRate = value;
              videoRef.current.playbackRate = value;
            }
          }}
          getSnapshotUrl={() =>
            endpoint(
              `${encodeURIComponent(props.camera)}/recordings/${timestamp.current}/snapshot.jpg?height=500`,
            )
          }
          onUploadFrame={async () => {
            try {
              await axios.post(
                endpoint(
                  `${encodeURIComponent(props.camera)}/plus/${timestamp.current}`,
                ),
                undefined,
                { withCredentials: true, headers: { "X-CSRF-TOKEN": "1" } },
              );
              toast.success(t("toast.success.submittedFrigatePlus"));
            } catch {
              toast.error(t("toast.error.submitFrigatePlusFailed"));
            }
          }}
          onSnapshot={async () => {
            if (!videoRef.current) return;
            try {
              const result = await grabVideoSnapshot(videoRef.current);
              if (!result.success) {
                toast.error(t("recordedHttp.failed"));
                return;
              }
              downloadSnapshot(
                result.data.dataUrl,
                generateSnapshotFilename(props.camera, timestamp.current),
              );
            } catch {
              toast.error(t("recordedHttp.failed"));
            }
          }}
          fullscreen={props.fullscreen}
          toggleFullscreen={props.toggleFullscreen}
          containerRef={props.containerRef}
        />
        <TransformComponent
          wrapperStyle={{
            width: "100%",
            height: "100%",
            display: props.isScrubbing ? "none" : undefined,
          }}
          contentStyle={{ width: "100%", height: "100%" }}
        >
          <div className="relative size-full">
            <video
              ref={videoRef}
              playsInline
              className={cn("size-full object-contain", props.className)}
              onPlay={() => setPlaying(true)}
              onPause={() => setPlaying(false)}
              onVolumeChange={(event) => setVolume(event.currentTarget.volume)}
              onWaiting={() => setLoading(true)}
              onPlaying={() => setLoading(false)}
            />
            {props.transformedOverlay}
          </div>
        </TransformComponent>
      </TransformWrapper>
      <PreviewPlayer
        className={cn(
          props.className,
          props.isScrubbing ? "visible" : "hidden",
        )}
        camera={props.camera}
        timeRange={props.timeRange}
        cameraPreviews={props.cameraPreviews}
        startTime={props.startTimestamp}
        recordingFallbackVisible={props.isScrubbing}
        isVisible={props.isScrubbing}
        isScrubbing={props.isScrubbing}
        onControllerReady={setPreview}
      />
      {!props.isScrubbing && loading && !failed && (
        <ActivityIndicator className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2" />
      )}
      {!props.isScrubbing && failed && (
        <div
          role="alert"
          className="absolute inset-0 z-50 flex flex-col items-center justify-center gap-3 bg-background/90 p-4 text-center"
        >
          <p>{t(empty ? "recordedHttp.noFootage" : "recordedHttp.failed")}</p>
          <Button onClick={() => setRetry((value) => value + 1)}>
            {t("compatibility.retry")}
          </Button>
        </div>
      )}
    </div>
  );
}
