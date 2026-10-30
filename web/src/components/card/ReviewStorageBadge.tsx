import { useTranslation } from "react-i18next";
import { ReviewSegment } from "@/types/review";
import { cn } from "@/lib/utils";

export default function ReviewStorageBadge({
  storage,
  className,
}: {
  storage: ReviewSegment["recording_storage"];
  className?: string;
}) {
  const { t } = useTranslation("views/events");
  if (!storage) return null;
  return (
    <span
      data-testid="review-storage-badge"
      title={t("storage.indexedDescription")}
      className={cn(
        "pointer-events-none inline-flex shrink-0 items-center rounded-md bg-black/75 px-2 py-1 text-xs font-medium text-white",
        className,
      )}
    >
      {storage === "local"
        ? t("storage.local")
        : storage === "archive"
          ? t("storage.archived")
          : t("storage.mixed")}
    </span>
  );
}
