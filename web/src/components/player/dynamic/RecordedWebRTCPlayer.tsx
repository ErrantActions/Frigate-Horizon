import ActivityIndicator from "@/components/indicators/activity-indicator";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";
import { useIsAdmin } from "@/hooks/use-is-admin";
import type { FrigateConfig } from "@/types/frigateConfig";
import type { RecordingCoverage } from "@/types/record";
import type {
  RecordedWebRTCSession,
  RecordedWebRTCWindow,
} from "@/types/recordedWebrtc";
import {
  downloadSnapshot,
  generateSnapshotFilename,
  grabVideoSnapshot,
} from "@/utils/snapshotUtil";
import { playWithTemporaryMuteFallback } from "@/utils/videoUtil";
import { webrtcEndpoint as endpoint } from "@/utils/webrtc";
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

export default function RecordedWebRTCPlayer(props: DynamicVideoPlayerProps) {
  const { t } = useTranslation("components/player");
  const isAdmin = useIsAdmin();
  const { data: config } = useSWR<FrigateConfig>("config");
  const { data: coverage } = useSWR<RecordingCoverage>([
    `${props.camera}/recordings/coverage`,
    { after: props.timeRange.after, before: props.timeRange.before },
  ]);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [preview, setPreview] = useState<PreviewController | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(true);
  const [volume, setVolume] = useState(1);
  const [retry, setRetry] = useState(0);
  const [selectedHeight, setSelectedHeight] = useState<number | null>(null);
  const selectedHeightRef = useRef(selectedHeight);
  selectedHeightRef.current = selectedHeight;
  const changeQuality = useRef<((height: number | null) => void) | undefined>(
    undefined,
  );
  const callbacks = useRef(props);
  callbacks.current = props;
  const coverageRef = useRef(coverage);
  coverageRef.current = coverage;
  const sessionRef = useRef<RecordedWebRTCSession | undefined>(undefined);
  const timestamp = useRef(props.startTimestamp ?? props.timeRange.after);
  const requestedTimestamp = useRef(timestamp.current);
  const wanted = useRef(true);
  const wasScrubbing = useRef(props.isScrubbing);
  const mutedRef = useRef(muted);
  mutedRef.current = muted;
  const controllerRef = useRef<DynamicVideoController | undefined>(undefined);
  const transport = useRef<RemoteRecordingPlayback | undefined>(undefined);
  const maximumHeight = config?.live_webrtc?.max_height ?? 1080;
  const saveData =
    (navigator as Navigator & { connection?: { saveData?: boolean } })
      .connection?.saveData ?? false;

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    let active = true;
    let broken = false;
    let peer: RTCPeerConnection | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let deadline: ReturnType<typeof setTimeout> | undefined;
    let work = Promise.resolve();
    let answered = 0;
    let decoded = 0;
    let activationQueued = 0;
    let seekRevision = 0;
    let inViewport = false;
    let deferredSeek: number | undefined;
    let decodedFrame: number | undefined;
    let decodedListener: (() => void) | undefined;
    const abort = new AbortController();
    const request = {
      withCredentials: true,
      signal: abort.signal,
      timeout: 10000,
      headers: { "X-CSRF-TOKEN": "1" },
    };
    const visible = () =>
      (document.visibilityState === "visible" && inViewport) ||
      document.pictureInPictureElement === video;
    const stopPeer = () => {
      clearTimeout(deadline);
      if (decodedFrame !== undefined)
        video.cancelVideoFrameCallback(decodedFrame);
      decodedFrame = undefined;
      if (decodedListener)
        video.removeEventListener("loadeddata", decodedListener);
      decodedListener = undefined;
      peer?.close();
      peer = undefined;
      video.pause();
      setPlaying(false);
    };
    const release = (session: RecordedWebRTCSession) => {
      void fetch(endpoint(session.close_url), {
        method: "DELETE",
        credentials: "include",
        keepalive: true,
        headers: { "X-CSRF-TOKEN": "1" },
      })
        .then((response) => {
          if (
            !response.ok &&
            response.status !== 404 &&
            response.status !== 410
          )
            toast.warning(t("adaptive.webrtcCleanupPending"));
        })
        .catch(() => toast.warning(t("adaptive.webrtcCleanupPending")));
    };
    const fail = () => {
      if (!active || broken) return;
      broken = true;
      clearTimeout(timer);
      stopPeer();
      setLoading(false);
      setFailed(true);
      if (sessionRef.current) release(sessionRef.current);
      sessionRef.current = undefined;
    };
    const queue = (operation: () => Promise<void>) => {
      work = work
        .then(async () => {
          if (active && !broken) await operation();
        })
        .catch((error: unknown) => {
          if (!axios.isCancel(error)) fail();
        });
      return work;
    };
    const remember = (
      session: RecordedWebRTCSession,
      updatePosition = true,
    ) => {
      sessionRef.current = session;
      if (updatePosition && session.position_timestamp !== null) {
        timestamp.current = session.position_timestamp;
        callbacks.current.onTimestampUpdate?.(timestamp.current);
      }
      video.muted = session.state === "warming" || mutedRef.current;
      if (session.state === "cleanup_pending") {
        throw new Error("Recorded source cleanup is pending");
      }
    };
    const control = async (
      action: "pause" | "resume" | "seek" | "activate",
      window?: RecordedWebRTCWindow,
    ) => {
      const current = sessionRef.current;
      if (!current) throw new Error("Recorded session is not ready");
      const revision = seekRevision;
      const response = await axios.post<RecordedWebRTCSession>(
        endpoint(current.control_url),
        {
          version: 1,
          generation: current.generation,
          action,
          ...(window ? { window } : {}),
          ...(action === "activate" ? { decoded_ready: true } : {}),
        },
        request,
      );
      if (!active) {
        release(response.data);
        return;
      }
      const obsolete =
        action === "seek" &&
        revision !== seekRevision &&
        response.data.window.start_timestamp !== requestedTimestamp.current;
      remember(response.data, !obsolete);
      if (obsolete) return response.data;
      if (response.data.renegotiate) await connect(response.data);
      if (response.data.state === "paused" || response.data.state === "ended")
        stopPeer();
      return response.data;
    };
    const activate = () => {
      const current = sessionRef.current;
      if (
        !current ||
        current.state !== "warming" ||
        answered !== current.generation ||
        decoded !== current.generation ||
        activationQueued === current.generation
      )
        return;
      activationQueued = current.generation;
      void queue(async () => {
        if (
          sessionRef.current?.generation !== current.generation ||
          sessionRef.current.state !== "warming"
        )
          return;
        const result = await control(
          wanted.current && visible() && !callbacks.current.isScrubbing
            ? "activate"
            : "pause",
        );
        if (result?.state === "playing") {
          clearTimeout(deadline);
          setLoading(false);
          setPlaying(true);
        }
      });
    };
    const onDecoded = () => {
      if (
        !active ||
        video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA ||
        video.videoWidth <= 0 ||
        video.videoHeight <= 0
      )
        return;
      callbacks.current.setFullResolution({
        width: video.videoWidth,
        height: video.videoHeight,
      });
    };
    const connect = async (session: RecordedWebRTCSession) => {
      stopPeer();
      answered = 0;
      decoded = 0;
      activationQueued = 0;
      if (!session.offer) throw new Error("Recorded peer has no offer");
      setLoading(true);
      const connection = new RTCPeerConnection({
        iceServers: session.ice_servers,
        iceTransportPolicy: session.ice_transport_policy,
      });
      peer = connection;
      const stream = new MediaStream();
      video.srcObject = stream;
      video.muted = true;
      const acknowledgeFrame = () => {
        if (!active || peer !== connection || video.srcObject !== stream)
          return;
        decoded = session.generation;
        setLoading(false);
        onDecoded();
        activate();
      };
      if (typeof video.requestVideoFrameCallback === "function") {
        decodedFrame = video.requestVideoFrameCallback(acknowledgeFrame);
      } else {
        decodedListener = acknowledgeFrame;
        video.addEventListener("loadeddata", decodedListener, { once: true });
      }
      const candidates: RTCIceCandidateInit[] = [];
      let candidateWork = Promise.resolve();
      const flush = () => {
        if (answered !== session.generation || !candidates.length) return;
        const batch = candidates.splice(0, 32);
        candidateWork = candidateWork
          .then(async () => {
            if (
              !active ||
              peer !== connection ||
              sessionRef.current?.generation !== session.generation
            )
              return;
            await axios.post(
              endpoint(session.candidates_url),
              {
                version: 1,
                generation: session.generation,
                candidates: batch,
              },
              request,
            );
          })
          .catch((error: unknown) => {
            if (
              !axios.isCancel(error) &&
              peer === connection &&
              sessionRef.current?.generation === session.generation
            )
              fail();
          });
      };
      connection.onicecandidate = (event) => {
        if (event.candidate && active && peer === connection) {
          candidates.push(event.candidate.toJSON());
          flush();
        }
      };
      connection.ontrack = (event) => {
        if (!active || peer !== connection) return;
        stream.addTrack(event.track);
        void playWithTemporaryMuteFallback(
          video,
          () => !active || peer !== connection,
        ).catch(fail);
      };
      connection.onconnectionstatechange = () => {
        if (peer === connection && connection.connectionState === "failed")
          fail();
      };
      deadline = setTimeout(fail, 15000);
      await connection.setRemoteDescription(session.offer);
      await Promise.all(
        session.ice_candidates.map((candidate) =>
          connection.addIceCandidate(candidate),
        ),
      );
      const answer = await connection.createAnswer();
      await connection.setLocalDescription(answer);
      await axios.post(
        endpoint(session.answer_url),
        {
          version: 1,
          generation: session.generation,
          type: "answer",
          sdp: answer.sdp,
        },
        request,
      );
      if (!active || peer !== connection) return;
      answered = session.generation;
      flush();
      activate();
    };
    const windowAt = (target: number): RecordedWebRTCWindow => {
      const range = callbacks.current.timeRange;
      if (
        !Number.isFinite(target) ||
        target < range.after ||
        target >= range.before
      ) {
        throw new Error("Recorded seek is outside the current catalog range");
      }
      const after = Math.max(range.after, Math.min(target, range.before - 0.1));
      return {
        after,
        before: Math.min(range.before, after + 30),
        start_timestamp: target,
        stream: "auto",
      };
    };
    const create = async () => {
      const revision = seekRevision;
      const response = await axios.post<RecordedWebRTCSession>(
        endpoint(
          `${encodeURIComponent(props.camera)}/recordings/webrtc/sessions`,
        ),
        {
          ...windowAt(timestamp.current),
          version: 1,
          max_height: saveData ? 720 : maximumHeight,
          height: saveData ? 720 : selectedHeightRef.current,
          audio: true,
        },
        request,
      );
      if (!active) {
        release(response.data);
        return;
      }
      const obsolete =
        revision !== seekRevision &&
        response.data.window.start_timestamp !== requestedTimestamp.current;
      remember(response.data, !obsolete);
      if (!obsolete) await connect(response.data);
      if (active && !broken && timer === undefined)
        timer = setTimeout(() => void renew(), 1000);
    };
    const renew = async () => {
      await queue(async () => {
        const current = sessionRef.current;
        if (!current || !visible()) return;
        const response = await axios.patch<RecordedWebRTCSession>(
          endpoint(current.heartbeat_url),
          undefined,
          request,
        );
        if (!active) return;
        remember(response.data);
        if (response.data.state === "ended" && wanted.current) {
          if (!coverageRef.current)
            throw new Error("Recording coverage is unavailable");
          const next = coverageRef.current?.spans.find(
            (span) =>
              span.end_time > response.data.window.before &&
              span.start_time < callbacks.current.timeRange.before,
          );
          if (next) {
            const target = Math.max(
              response.data.window.before,
              next.start_time,
            );
            requestedTimestamp.current = target;
            await control("seek", windowAt(target));
          } else {
            wanted.current = false;
            stopPeer();
            callbacks.current.onClipEnded?.();
          }
        }
      });
      if (active && !broken) timer = setTimeout(() => void renew(), 1000);
    };
    transport.current = {
      play: () => {
        wanted.current = true;
        if (!visible() || callbacks.current.isScrubbing) return;
        if (deferredSeek !== undefined) {
          transport.current?.seek(deferredSeek, true);
          return;
        }
        void queue(async () => {
          const current = sessionRef.current;
          if (!current) await create();
          else if (current.state === "paused" || current.state === "ended")
            await control("resume");
        });
      },
      pause: () => {
        wanted.current = controllerRef.current?.wantsPlayback() ?? false;
        video.pause();
        void queue(async () => {
          if (
            sessionRef.current?.state === "playing" ||
            sessionRef.current?.state === "warming"
          )
            await control("pause");
        });
      },
      seek: (target, play) => {
        const revision = ++seekRevision;
        wanted.current = play;
        requestedTimestamp.current = target;
        timestamp.current = target;
        if (!visible() || callbacks.current.isScrubbing) {
          deferredSeek = target;
          return;
        }
        deferredSeek = undefined;
        void queue(async () => {
          if (revision !== seekRevision) return;
          if (!sessionRef.current) await create();
          else if (
            sessionRef.current.state === "warming" &&
            sessionRef.current.window.start_timestamp === target &&
            sessionRef.current.window.stream === "auto"
          )
            activate();
          else await control("seek", windowAt(target));
        });
      },
      timestamp: () => timestamp.current,
      isPlaying: () => sessionRef.current?.state === "playing" && !video.paused,
      hasRecording: (target) =>
        coverageRef.current?.spans.some(
          (span) => span.start_time <= target && target < span.end_time,
        ) ?? false,
    };
    changeQuality.current = (height) => {
      void queue(async () => {
        const current = sessionRef.current;
        if (!current) throw new Error("Recorded session is not ready");
        await axios.patch(
          endpoint(current.quality_url),
          {
            version: 1,
            generation: current.generation,
            height,
          },
          request,
        );
      });
    };
    const visibility = () => {
      if (!visible()) {
        video.pause();
        void queue(async () => {
          if (
            sessionRef.current?.state === "playing" ||
            sessionRef.current?.state === "warming"
          )
            await control("pause");
        });
      } else if (!callbacks.current.isScrubbing) {
        if (deferredSeek !== undefined)
          transport.current?.seek(deferredSeek, wanted.current);
        else if (wanted.current) transport.current?.play();
      }
    };
    const pagehide = () => {
      active = false;
      clearTimeout(timer);
      clearTimeout(deadline);
      abort.abort();
      stopPeer();
      const current = sessionRef.current;
      sessionRef.current = undefined;
      if (current) release(current);
    };
    const pageshow = (event: PageTransitionEvent) => {
      if (event.persisted) setRetry((value) => value + 1);
    };
    const observer = new IntersectionObserver(([entry]) => {
      if (!active || !entry || inViewport === entry.isIntersecting) return;
      inViewport = entry.isIntersecting;
      visibility();
    });
    observer.observe(video);
    video.addEventListener("loadeddata", onDecoded);
    video.addEventListener("resize", onDecoded);
    video.addEventListener("playing", onDecoded);
    video.addEventListener("error", fail);
    video.addEventListener("enterpictureinpicture", visibility);
    video.addEventListener("leavepictureinpicture", visibility);
    document.addEventListener("visibilitychange", visibility);
    window.addEventListener("pagehide", pagehide);
    window.addEventListener("pageshow", pageshow);
    setFailed(false);
    setLoading(true);
    if (visible() && !callbacks.current.isScrubbing) {
      void queue(create);
    }
    return () => {
      active = false;
      clearTimeout(timer);
      clearTimeout(deadline);
      abort.abort();
      stopPeer();
      const current = sessionRef.current;
      sessionRef.current = undefined;
      if (current) release(current);
      video.srcObject = null;
      video.removeEventListener("loadeddata", onDecoded);
      video.removeEventListener("resize", onDecoded);
      video.removeEventListener("playing", onDecoded);
      video.removeEventListener("error", fail);
      video.removeEventListener("enterpictureinpicture", visibility);
      video.removeEventListener("leavepictureinpicture", visibility);
      document.removeEventListener("visibilitychange", visibility);
      observer.disconnect();
      window.removeEventListener("pagehide", pagehide);
      window.removeEventListener("pageshow", pageshow);
      transport.current = undefined;
      changeQuality.current = undefined;
    };
  }, [props.camera, maximumHeight, saveData, retry, t]);

  const controller = useMemo(() => {
    const video = videoRef.current;
    if (!video || !preview) return undefined;
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
      preview,
      0,
      "playback",
      () => {},
      () => {},
      () => toast.error(t("playbackFailed")),
      bridge,
    );
  }, [props.camera, preview, t]);
  controllerRef.current = controller;
  useEffect(() => {
    if (controller) callbacks.current.onControllerReady(controller);
    return () => controller?.dispose();
  }, [controller]);
  useEffect(() => {
    if (!controller) return;
    const scrubEnded = wasScrubbing.current && !props.isScrubbing;
    wasScrubbing.current = props.isScrubbing;
    if (props.isScrubbing) {
      controller.scrubToTimestamp(
        props.startTimestamp ?? timestamp.current,
        true,
      );
    } else if (
      props.startTimestamp !== undefined &&
      props.startTimestamp !== requestedTimestamp.current
    ) {
      controller.seekToTimestamp(
        props.startTimestamp,
        controller.wantsPlayback(),
      );
    } else if (scrubEnded) {
      controller.seekToTimestamp(
        props.startTimestamp ?? requestedTimestamp.current,
        controller.wantsPlayback(),
      );
    }
  }, [controller, props.isScrubbing, props.startTimestamp]);

  return (
    <div className="relative size-full" data-recorded-transport="webrtc">
      {!props.isScrubbing && !failed && (
        <Select
          value={selectedHeight === null ? "auto" : String(selectedHeight)}
          disabled={loading || sessionRef.current?.state !== "playing"}
          onValueChange={(value) => {
            const height = value === "auto" ? null : Number(value);
            if (
              height !== null &&
              !sessionRef.current?.renditions.some(
                (rung) => rung.height === height,
              )
            ) {
              toast.error(t("playbackFailed"));
              return;
            }
            selectedHeightRef.current = height;
            setSelectedHeight(height);
            changeQuality.current?.(height);
          }}
        >
          <SelectTrigger
            aria-label={t("recordedWebrtc.qualityLabel")}
            className="absolute right-2 top-2 z-50 w-auto bg-background/80"
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="auto">{t("quality.auto")}</SelectItem>
            {sessionRef.current?.renditions.map((rung) => (
              <SelectItem key={rung.height} value={String(rung.height)}>
                {t("recordedWebrtc.height", { height: rung.height })}
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
          playbackRate={1}
          hotKeys={props.hotKeys}
          features={{
            volume: true,
            seek: true,
            playbackRate: false,
            snapshot: true,
            plusUpload: isAdmin && config?.plus?.enabled === true,
            fullscreen: props.supportsFullscreen,
          }}
          setMuted={(value) => {
            setMuted(value);
            if (videoRef.current)
              videoRef.current.muted =
                sessionRef.current?.state === "warming" || value;
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
          onSetPlaybackRate={() =>
            toast.error(t("recordedWebrtc.speedUnavailable"))
          }
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
                {
                  withCredentials: true,
                  headers: { "X-CSRF-TOKEN": "1" },
                },
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
                toast.error(t("playbackFailed"));
                return;
              }
              downloadSnapshot(
                result.data.dataUrl,
                generateSnapshotFilename(props.camera, timestamp.current),
              );
            } catch {
              toast.error(t("playbackFailed"));
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
              autoPlay
              playsInline
              className={cn("size-full object-contain", props.className)}
              onVolumeChange={(event) => setVolume(event.currentTarget.volume)}
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
          <p>{t("playbackFailed")}</p>
          <Button onClick={() => setRetry((value) => value + 1)}>
            {t("compatibility.retry")}
          </Button>
        </div>
      )}
      {!props.isScrubbing && !failed && (
        <div
          role="status"
          className="absolute left-2 top-2 rounded bg-background/80 px-2 py-1 text-xs"
        >
          {t("recordedWebrtc.positionEstimate")}
        </div>
      )}
    </div>
  );
}
