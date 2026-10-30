import axios from "axios";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { baseUrl } from "@/api/baseUrl";
import { Button } from "@/components/ui/button";
import { onvifPath } from "@/hooks/use-onvif";
import type { ReplaySession } from "@/types/onvif";
import { toast } from "sonner";

/** Only a same-origin opaque session URL reaches the browser's video element. */
export default function OnvifReplayPlayer({
  camera,
  recording,
  onClose,
}: {
  camera: string;
  recording: { token: string; start: string; end: string };
  onClose: () => void;
}) {
  const { t } = useTranslation("views/onvif");
  const [session, setSession] = useState<ReplaySession>();
  const [failed, setFailed] = useState(false);
  const [ended, setEnded] = useState(false);
  const [verificationFailed, setVerificationFailed] = useState(false);
  const current = useRef<string | undefined>(undefined);
  const video = useRef<HTMLVideoElement>(null);
  const translationRef = useRef(t);
  useEffect(() => {
    translationRef.current = t;
  }, [t]);
  const path = (identifier: string) =>
    `${baseUrl}api/${onvifPath(camera, `replay/sessions/${encodeURIComponent(identifier)}`)}`;
  const releaseId = useCallback(
    async (identifier: string) => {
      const target = `${baseUrl}api/${onvifPath(camera, `replay/sessions/${encodeURIComponent(identifier)}`)}`;
      try {
        const response = await fetch(target, {
          method: "DELETE",
          credentials: "same-origin",
          headers: { "X-CSRF-TOKEN": "1" },
          keepalive: true,
        });
        if (!response.ok && ![404, 410].includes(response.status)) {
          toast.warning(translationRef.current("recordings.cleanupFailed"));
        }
      } catch (error) {
        if (!(error instanceof TypeError)) throw error;
        toast.warning(translationRef.current("recordings.cleanupFailed"));
      }
    },
    [camera],
  );
  const release = () => {
    const identifier = current.current;
    current.current = undefined;
    if (identifier) {
      void releaseId(identifier);
    }
  };
  useEffect(() => {
    const controller = new AbortController();
    void axios
      .post<ReplaySession>(
        onvifPath(camera, "replay/sessions"),
        {
          recording_token: recording.token,
          start: recording.start,
          end: recording.end,
        },
        { signal: controller.signal },
      )
      .then((response) => {
        if (controller.signal.aborted) {
          void releaseId(response.data.id);
          return;
        }
        current.current = response.data.id;
        setSession(response.data);
      })
      .catch((error) => {
        if (axios.isCancel(error)) return;
        if (!axios.isAxiosError(error)) throw error;
        setFailed(true);
      });
    return () => {
      controller.abort();
      const identifier = current.current;
      current.current = undefined;
      if (identifier) {
        void releaseId(identifier);
      }
    };
  }, [camera, recording.end, recording.start, recording.token, releaseId]);

  return (
    <section className="flex flex-col gap-3 rounded border p-3">
      <h4>{t("recordings.browserTitle")}</h4>
      <p className="text-sm">{t("recordings.browserHelp")}</p>
      {!session && !failed && <p role="status">{t("recordings.preparing")}</p>}
      {session && (
        <p role="status">
          {t(`recordings.audio.${session.audio_status ?? "pending"}`)}
        </p>
      )}
      {session && !failed && (
        <video
          ref={video}
          className="max-h-[60dvh] w-full rounded bg-black"
          aria-label={t("recordings.browserTitle")}
          src={`${path(session.id)}/stream.mp4`}
          controls
          autoPlay
          muted
          playsInline
          preload="auto"
          onLoadedMetadata={() => {
            void axios
              .get<ReplaySession>(path(session.id), { timeout: 10000 })
              .then((response) => {
                if (current.current === session.id) {
                  setSession(response.data);
                  setVerificationFailed(false);
                }
              })
              .catch((error) => {
                if (!axios.isAxiosError(error)) throw error;
                if (current.current === session.id) {
                  setVerificationFailed(true);
                }
              });
          }}
          onEnded={() => {
            setEnded(true);
            release();
          }}
          onError={() => {
            setFailed(true);
            release();
          }}
        />
      )}
      {ended && <p role="status">{t("recordings.ended")}</p>}
      {failed && <p role="alert">{t("recordings.unsupported")}</p>}
      {verificationFailed && (
        <p role="alert">{t("recordings.verificationFailed")}</p>
      )}
      <Button
        variant="secondary"
        onClick={() => {
          video.current?.pause();
          release();
          onClose();
        }}
      >
        {t("recordings.close")}
      </Button>
    </section>
  );
}
