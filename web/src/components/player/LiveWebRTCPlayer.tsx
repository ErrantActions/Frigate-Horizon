import type { LiveWebRTCLease, LiveWebRTCSession } from "@/types/liveWebrtc";
import { playWithTemporaryMuteFallback } from "@/utils/videoUtil";
import { webrtcEndpoint as endpoint } from "@/utils/webrtc";
import axios from "axios";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import type { LiveHlsPlayerProps } from "./LiveHlsPlayer";

export default function LiveWebRTCPlayer({
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
  dashboard = false,
}: LiveHlsPlayerProps) {
  const { t } = useTranslation("components/player");
  const videoRef = useRef<HTMLVideoElement>(null);
  const peerRef = useRef<RTCPeerConnection | undefined>(undefined);
  const sessionRef = useRef<LiveWebRTCSession | undefined>(undefined);
  const answeredRef = useRef(false);
  const [visible, setVisible] = useState(
    document.visibilityState === "visible",
  );
  const [inPip, setInPip] = useState(false);
  const activePlayback = playbackEnabled && (visible || pip || inPip);
  const saveData = (
    navigator as Navigator & { connection?: { saveData?: boolean } }
  ).connection?.saveData;
  const detailCap =
    dashboard || saveData
      ? 720
      : maximumHeight >= 2160
        ? 2160
        : maximumHeight >= 1080
          ? 1080
          : 720;
  const selectedHeight = saveData ? renditions[0]?.height : preferredHeight;
  const selection = useRef(selectedHeight);
  selection.current = selectedHeight;
  const callbacks = useRef({ onPlaying, onError, onResolution, onLevelChange });
  callbacks.current = { onPlaying, onError, onResolution, onLevelChange };

  useEffect(() => {
    const video = videoRef.current;
    const update = () => {
      setVisible(document.visibilityState === "visible");
      setInPip(document.pictureInPictureElement === video);
    };
    document.addEventListener("visibilitychange", update);
    video?.addEventListener("enterpictureinpicture", update);
    video?.addEventListener("leavepictureinpicture", update);
    return () => {
      document.removeEventListener("visibilitychange", update);
      video?.removeEventListener("enterpictureinpicture", update);
      video?.removeEventListener("leavepictureinpicture", update);
    };
  }, []);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !activePlayback) return;
    let active = true;
    let failed = false;
    let answered = false;
    let heartbeat: ReturnType<typeof setTimeout> | undefined;
    let connectionDeadline: ReturnType<typeof setTimeout> | undefined;
    let session: LiveWebRTCSession | undefined;
    let peer: RTCPeerConnection | undefined;
    let candidateWork: Promise<void> = Promise.resolve();
    const candidates: RTCIceCandidateInit[] = [];
    const controller = new AbortController();
    const release = (lease: LiveWebRTCSession) => {
      void fetch(endpoint(lease.close_url), {
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
          ) {
            toast.warning(t("adaptive.webrtcCleanupPending"));
          }
        })
        .catch(() => toast.warning(t("adaptive.webrtcCleanupPending")));
    };
    const request = {
      withCredentials: true,
      signal: controller.signal,
      timeout: 8000,
      headers: { "X-CSRF-TOKEN": "1" },
    };
    const fail = () => {
      if (!active || failed) return;
      failed = true;
      clearTimeout(heartbeat);
      clearTimeout(connectionDeadline);
      peer?.close();
      callbacks.current.onError();
    };
    const resolution = () => {
      if (!active || video.videoWidth <= 0 || video.videoHeight <= 0) return;
      callbacks.current.onResolution({
        width: video.videoWidth,
        height: video.videoHeight,
      });
      callbacks.current.onLevelChange?.(video.videoHeight);
    };
    const playing = () => {
      if (!active) return;
      clearTimeout(connectionDeadline);
      resolution();
      callbacks.current.onPlaying();
    };
    const flushCandidates = () => {
      if (!answered || !session || !candidates.length) return;
      const batch = candidates.splice(0, 32);
      const url = session.candidates_url;
      candidateWork = candidateWork
        .then(async () => {
          if (!active) return;
          await axios.post(endpoint(url), { candidates: batch }, request);
        })
        .catch((error: unknown) => {
          if (!axios.isCancel(error)) fail();
        });
    };
    const renew = async () => {
      if (!active || !session || failed) return;
      try {
        const response = await axios.patch<LiveWebRTCLease>(
          endpoint(session.heartbeat_url),
          undefined,
          request,
        );
        if (active && !failed) {
          heartbeat = setTimeout(
            () => void renew(),
            response.data.heartbeat_seconds * 1000,
          );
        }
      } catch (error: unknown) {
        if (!axios.isCancel(error)) fail();
      }
    };
    video.addEventListener("loadeddata", playing);
    video.addEventListener("resize", resolution);
    video.addEventListener("error", fail);
    const connect = async () => {
      try {
        const result = await axios.post<LiveWebRTCSession>(
          endpoint(`${encodeURIComponent(camera)}/live/webrtc/sessions`),
          {
            view: dashboard ? "grid" : "detail",
            height: selection.current ?? null,
            max_height: detailCap,
            audio: audioEnabled,
          },
          request,
        );
        session = result.data;
        if (!active) {
          release(session);
          return;
        }
        sessionRef.current = session;
        peer = new RTCPeerConnection({
          iceServers: session.ice_servers,
          iceTransportPolicy: session.ice_transport_policy,
        });
        peerRef.current = peer;
        connectionDeadline = setTimeout(fail, 15000);
        const stream = new MediaStream();
        video.srcObject = stream;
        peer.ontrack = (event) => {
          stream.addTrack(event.track);
          void playWithTemporaryMuteFallback(video, () => !active).catch(fail);
        };
        peer.onicecandidate = (event) => {
          if (!event.candidate || !active) return;
          candidates.push(event.candidate.toJSON());
          flushCandidates();
        };
        peer.onconnectionstatechange = () => {
          if (peer?.connectionState === "failed") fail();
        };
        await peer.setRemoteDescription(session.offer);
        await Promise.all(
          session.ice_candidates.map((candidate) =>
            peer?.addIceCandidate(candidate),
          ),
        );
        const answer = await peer.createAnswer();
        await peer.setLocalDescription(answer);
        await axios.post(
          endpoint(session.answer_url),
          { type: "answer", sdp: answer.sdp },
          request,
        );
        if (!active) return;
        answered = true;
        answeredRef.current = true;
        if (selection.current !== (session.initial_height ?? undefined)) {
          await axios.patch(
            endpoint(session.quality_url),
            { height: selection.current ?? null },
            request,
          );
        }
        flushCandidates();
        heartbeat = setTimeout(
          () => void renew(),
          session.heartbeat_seconds * 1000,
        );
      } catch (error: unknown) {
        if (!axios.isCancel(error)) fail();
      }
    };
    void connect();
    return () => {
      active = false;
      clearTimeout(heartbeat);
      clearTimeout(connectionDeadline);
      controller.abort();
      video.removeEventListener("loadeddata", playing);
      video.removeEventListener("resize", resolution);
      video.removeEventListener("error", fail);
      peer?.close();
      peerRef.current = undefined;
      sessionRef.current = undefined;
      answeredRef.current = false;
      video.pause();
      video.srcObject = null;
      if (session) {
        release(session);
      }
    };
  }, [camera, activePlayback, dashboard, detailCap, audioEnabled, t]);

  useEffect(() => {
    const session = sessionRef.current;
    if (!session || !answeredRef.current) return;
    const controller = new AbortController();
    void axios
      .patch(
        endpoint(session.quality_url),
        { height: selectedHeight ?? null },
        {
          withCredentials: true,
          signal: controller.signal,
          headers: { "X-CSRF-TOKEN": "1" },
        },
      )
      .catch((error: unknown) => {
        if (!axios.isCancel(error)) callbacks.current.onError();
      });
    return () => controller.abort();
  }, [selectedHeight]);

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
    if (!getStats || !activePlayback) return;
    let active = true;
    let lastBytes = 0;
    let lastTime = 0;
    const timer = setInterval(() => {
      const peer = peerRef.current;
      if (!peer || peer.connectionState !== "connected") return;
      void peer
        .getStats()
        .then((report) => {
          if (!active) return;
          report.forEach((value) => {
            if (value.type !== "inbound-rtp" || value.kind !== "video") return;
            const bytes =
              typeof value.bytesReceived === "number" ? value.bytesReceived : 0;
            const now = performance.now();
            const quality = videoRef.current?.getVideoPlaybackQuality();
            const total = quality?.totalVideoFrames ?? 0;
            const dropped = quality?.droppedVideoFrames ?? 0;
            setStats({
              streamType: "WebRTC",
              bandwidth:
                lastTime > 0
                  ? Math.max(0, bytes - lastBytes) / (now - lastTime)
                  : 0,
              totalFrames: total,
              droppedFrames: dropped,
              decodedFrames: total - dropped,
              droppedFrameRate: total > 0 ? (dropped / total) * 100 : 0,
            });
            lastBytes = bytes;
            lastTime = now;
          });
        })
        .catch(() => callbacks.current.onError());
    }, 1000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [getStats, activePlayback, setStats]);

  return (
    <video
      ref={videoRef}
      className={className}
      playsInline
      muted={!audioEnabled}
      data-live-transport="webrtc"
    />
  );
}
