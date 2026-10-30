import { useState } from "react";
import { useTranslation } from "react-i18next";
import { baseUrl } from "@/api/baseUrl";
import { ProtectedMediaItem } from "@/types/protectedMedia";
import { Button, buttonVariants } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

type Props = {
  item: ProtectedMediaItem;
  isAdmin: boolean;
  onUnprotect: (item: ProtectedMediaItem) => void;
};

export default function ProtectedMediaCard({
  item,
  isAdmin,
  onUnprotect,
}: Props) {
  const { t } = useTranslation(["views/protectedMedia"]);
  const [playing, setPlaying] = useState(false);
  const [playbackFailed, setPlaybackFailed] = useState(false);
  const original = item.original_url
    ? `${baseUrl}api/${item.original_url}`
    : undefined;
  const title = t("itemTitle", { camera: item.camera });

  return (
    <article
      className="flex min-w-0 flex-col gap-3 rounded-lg border p-4"
      aria-label={title}
    >
      <img
        className="aspect-video w-full rounded object-cover"
        src={`${baseUrl}api/${item.thumbnail_url}`}
        alt={t("thumbnail", { camera: item.camera })}
        loading="lazy"
      />
      <h2 className="break-words font-semibold">{item.camera}</h2>
      <p className="text-sm">
        <time dateTime={new Date(item.start_time * 1000).toISOString()}>
          {new Date(item.start_time * 1000).toLocaleString()}
        </time>
        {" / "}
        {item.end_time === null ? (
          t("ongoing")
        ) : (
          <time dateTime={new Date(item.end_time * 1000).toISOString()}>
            {new Date(item.end_time * 1000).toLocaleString()}
          </time>
        )}
      </p>
      <p>{t("reason.saved_event")}</p>
      <p className="text-sm">{t(`availability.${item.availability}`)}</p>
      <p className="text-sm">
        {item.tiers
          .map((tier) => t(`tiers.${tier}`, { defaultValue: tier }))
          .join(", ")}
      </p>
      <div className="mt-auto flex flex-wrap gap-2">
        <Button
          variant="secondary"
          disabled={!original}
          onClick={() => {
            setPlaybackFailed(false);
            setPlaying(true);
          }}
        >
          {t("playOriginal")}
        </Button>
        {original && (
          <a
            className={buttonVariants({ variant: "secondary" })}
            href={original}
            download={`${item.id}.mp4`}
          >
            {t("exportOriginal")}
          </a>
        )}
        {item.snapshot_url && (
          <a
            className={buttonVariants({ variant: "secondary" })}
            href={`${baseUrl}api/${item.snapshot_url}`}
            download={`${item.id}.jpg`}
          >
            {t("snapshot")}
          </a>
        )}
        {isAdmin && (
          <Button variant="outline" onClick={() => onUnprotect(item)}>
            {t("unprotect")}
          </Button>
        )}
      </div>
      <Dialog open={playing} onOpenChange={setPlaying}>
        <DialogContent className="max-w-4xl">
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            <DialogDescription>{t("originalHint")}</DialogDescription>
          </DialogHeader>
          {playbackFailed ? (
            <p role="alert">{t("playbackError")}</p>
          ) : (
            playing && (
              <video
                className="w-full"
                controls
                autoPlay
                playsInline
                src={original}
                aria-label={t("playOriginal")}
                onError={() => setPlaybackFailed(true)}
              />
            )
          )}
        </DialogContent>
      </Dialog>
    </article>
  );
}
