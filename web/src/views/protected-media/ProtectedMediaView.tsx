import { useEffect, useState } from "react";
import useSWR from "swr";
import { useTranslation } from "react-i18next";
import ActivityIndicator from "@/components/indicators/activity-indicator";
import ProtectedMediaCard from "@/components/card/ProtectedMediaCard";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import UnprotectMediaDialog from "@/components/overlay/UnprotectMediaDialog";
import { useIsAdmin } from "@/hooks/use-is-admin";
import {
  ProtectedMediaItem,
  ProtectedMediaResponse,
} from "@/types/protectedMedia";

const PAGE_SIZE = 24;

function dateBound(date: string, end: boolean) {
  const value = new Date(`${date}T00:00:00`);
  if (end) {
    value.setDate(value.getDate() + 1);
    value.setMilliseconds(-1);
  }
  return value.getTime() / 1000;
}

export default function ProtectedMediaView() {
  const { t } = useTranslation(["views/protectedMedia"]);
  const isAdmin = useIsAdmin();
  const [camera, setCamera] = useState("");
  const [after, setAfter] = useState("");
  const [before, setBefore] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<ProtectedMediaItem>();
  const invalidRange = Boolean(after && before && after > before);
  const query = new URLSearchParams({
    limit: String(PAGE_SIZE),
    offset: String(offset),
  });
  if (camera) query.set("camera", camera);
  if (after) query.set("after", String(dateBound(after, false)));
  if (before) query.set("before", String(dateBound(before, true)));
  const { data, error, mutate, isValidating } = useSWR<ProtectedMediaResponse>(
    invalidRange ? null : `protected-media?${query.toString()}`,
    { refreshInterval: 30000 },
  );

  useEffect(() => {
    if (data && offset > 0 && offset >= data.total) {
      setOffset(
        Math.max(0, Math.floor((data.total - 1) / PAGE_SIZE) * PAGE_SIZE),
      );
    }
  }, [data, offset]);

  return (
    <section className="space-y-5 p-4" aria-label={t("title")}>
      <h1 className="text-2xl font-semibold">{t("title")}</h1>
      <p>{t("description")}</p>
      <p className="text-sm text-muted-foreground">{t("leaseHint")}</p>
      <div className="flex flex-wrap items-end gap-4">
        <div className="space-y-2">
          <Label htmlFor="protected-camera">{t("camera")}</Label>
          <select
            id="protected-camera"
            className="block h-10 max-w-full rounded-md border bg-background px-3"
            value={camera}
            onChange={(event) => {
              setCamera(event.target.value);
              setOffset(0);
            }}
          >
            <option value="">{t("allCameras")}</option>
            {(data?.cameras ?? (camera ? [camera] : [])).map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </div>
        <div className="space-y-2">
          <Label htmlFor="protected-after">{t("after")}</Label>
          <Input
            id="protected-after"
            type="date"
            value={after}
            onChange={(event) => {
              setAfter(event.target.value);
              setOffset(0);
            }}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="protected-before">{t("before")}</Label>
          <Input
            id="protected-before"
            type="date"
            value={before}
            onChange={(event) => {
              setBefore(event.target.value);
              setOffset(0);
            }}
          />
        </div>
        <Button
          variant="outline"
          disabled={isValidating || invalidRange}
          onClick={() => void mutate()}
        >
          {t("refresh")}
        </Button>
      </div>
      {invalidRange ? (
        <p role="alert">{t("invalidRange")}</p>
      ) : error ? (
        <p role="alert">{t("loadError")}</p>
      ) : !data ? (
        <ActivityIndicator />
      ) : (
        <>
          <p>{t("total", { count: data.total })}</p>
          <div className="grid gap-3 sm:grid-cols-2">
            {Object.entries(data.storage).map(([tier, usage]) => (
              <div key={tier} className="rounded-lg border p-3">
                <h2 className="font-semibold">
                  {t(`tiers.${tier}`, { defaultValue: tier })}
                </h2>
                <p>{t("physicalRecordings", { count: usage.recordings })}</p>
                <p>
                  {t("knownSize", {
                    size: (usage.known_bytes / 2 ** 30).toLocaleString(
                      undefined,
                      { maximumFractionDigits: 3 },
                    ),
                  })}
                </p>
                {usage.total_bytes === null && (
                  <p>
                    {t("unknownSize", { count: usage.unknown_size_recordings })}
                  </p>
                )}
              </div>
            ))}
          </div>
          <p className="text-sm text-muted-foreground">{t("metadataHint")}</p>
          <p className="text-sm">
            {t("archiveHealth", {
              state: t(`health.${data.archive_state}`, {
                defaultValue: t("health.unknown"),
              }),
            })}{" "}
            {data.archive_updated_at === null
              ? t("healthNeverChecked")
              : t("healthChecked", {
                  time: new Date(
                    data.archive_updated_at * 1000,
                  ).toLocaleString(),
                })}
          </p>
          {data.items.length === 0 ? (
            <p>{t("empty")}</p>
          ) : (
            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
              {data.items.map((item) => (
                <ProtectedMediaCard
                  key={item.id}
                  item={item}
                  isAdmin={isAdmin}
                  onUnprotect={(value) => {
                    setSelected(value);
                  }}
                />
              ))}
            </div>
          )}
          <nav
            className="flex flex-wrap items-center gap-3"
            aria-label={t("pagination")}
          >
            <Button
              variant="outline"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              {t("previous")}
            </Button>
            <span>
              {t("page", { page: Math.floor(offset / PAGE_SIZE) + 1 })}
            </span>
            <Button
              variant="outline"
              disabled={offset + PAGE_SIZE >= data.total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              {t("next")}
            </Button>
          </nav>
        </>
      )}
      <UnprotectMediaDialog
        eventId={selected?.id}
        open={Boolean(selected)}
        onOpenChange={(open) => {
          if (!open) setSelected(undefined);
        }}
        onSuccess={() => void mutate()}
      />
    </section>
  );
}
