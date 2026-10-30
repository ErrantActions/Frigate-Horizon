import { FormEvent, useContext, useId, useMemo, useState } from "react";
import axios from "axios";
import useSWR from "swr";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import type { TimeRange } from "@/types/timeline";
import { AuthContext } from "@/context/auth-context";
import { useAllowedCameras } from "@/hooks/use-allowed-cameras";

export type RecordingBookmark = {
  id: string;
  camera: string;
  timestamp: number;
  end_time: number | null;
  title: string;
  notes: string;
  owner: string;
  revision: number;
  can_edit: boolean;
  available_at_anchor: boolean;
  protects_footage: boolean;
};

type Draft = {
  id?: string;
  timestamp: number;
  end_time: number | null;
  title: string;
  notes: string;
  revision?: number;
};
type BookmarkPage = { items: RecordingBookmark[]; has_more: boolean };

type BookmarkProps = {
  camera: string;
  timestamp: number;
  selection?: TimeRange;
  bounds: TimeRange;
  timezone: string;
  visible?: boolean;
  onSeek: (timestamp: number) => void;
};

export default function RecordingBookmarks(props: BookmarkProps) {
  const { auth } = useContext(AuthContext);
  const allowed = useAllowedCameras();
  if (auth.isLoading || !allowed.includes(props.camera)) return null;
  const identity = JSON.stringify([auth.user?.username, auth.user?.role]);
  return <BookmarkEditor key={identity} {...props} identity={identity} />;
}

function BookmarkEditor({
  camera,
  timestamp,
  selection,
  bounds,
  timezone,
  onSeek,
  identity,
  visible = true,
}: BookmarkProps & { identity: string }) {
  const { t } = useTranslation("components/player");
  const editorId = useId();
  const [open, setOpen] = useState(false);
  const [offset, setOffset] = useState(0);
  const base = `${encodeURIComponent(camera)}/recordings/bookmarks`;
  const { data, error, mutate } = useSWR<BookmarkPage>(
    visible && open
      ? {
          path: base,
          identity,
          params: {
            after: bounds.after,
            before: bounds.before,
            limit: 25,
            offset,
          },
        }
      : null,
    async ({
      path,
      params,
    }: {
      path: string;
      params: { after: number; before: number; limit: number; offset: number };
    }) => (await axios.get<BookmarkPage>(path, { params })).data,
    { revalidateOnFocus: false, keepPreviousData: false },
  );
  const [draft, setDraft] = useState<Draft>();
  const [pendingDelete, setPendingDelete] = useState<RecordingBookmark>();
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string>();
  const formatter = useMemo(
    () =>
      new Intl.DateTimeFormat(undefined, {
        dateStyle: "medium",
        timeStyle: "long",
        timeZone: timezone,
      }),
    [timezone],
  );
  const format = (value: number) => formatter.format(value * 1000);

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!draft || busy) return;
    setBusy(true);
    setFailure(undefined);
    try {
      if (draft.id) {
        await axios.patch(`${base}/${draft.id}`, {
          title: draft.title,
          notes: draft.notes,
          revision: draft.revision,
        });
      } else {
        await axios.post(base, {
          timestamp: draft.timestamp,
          end_time: draft.end_time,
          title: draft.title,
          notes: draft.notes,
        });
      }
      setDraft(undefined);
    } catch (error) {
      setFailure(
        t(
          axios.isAxiosError(error) && error.response?.status === 409
            ? "bookmarks.conflict"
            : "bookmarks.saveFailed",
        ),
      );
      return;
    } finally {
      setBusy(false);
    }
    try {
      await mutate();
    } catch {
      setFailure(t("bookmarks.refreshFailed"));
    }
  };

  const remove = async () => {
    if (!pendingDelete || busy) return;
    setBusy(true);
    setFailure(undefined);
    try {
      await axios.delete(`${base}/${pendingDelete.id}`, {
        params: { revision: pendingDelete.revision },
      });
      setPendingDelete(undefined);
    } catch (error) {
      setFailure(
        t(
          axios.isAxiosError(error) && error.response?.status === 409
            ? "bookmarks.conflict"
            : "bookmarks.deleteFailed",
        ),
      );
      return;
    } finally {
      setBusy(false);
    }
    try {
      await mutate();
    } catch {
      setFailure(t("bookmarks.refreshFailed"));
    }
  };

  return (
    <details
      className="mt-3 border-t pt-2"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="cursor-pointer font-medium">
        {t("bookmarks.title")}
      </summary>
      {open && (
        <div className="mt-3 space-y-3">
          <p className="text-xs text-muted-foreground">
            {t("bookmarks.sharing")}
          </p>
          <p className="text-xs text-muted-foreground">
            {t("bookmarks.retention")}
          </p>
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => {
              setFailure(undefined);
              setDraft({
                timestamp: selection?.after ?? timestamp,
                end_time: selection?.before ?? null,
                title: "",
                notes: "",
              });
            }}
          >
            {t("bookmarks.add")}
          </Button>
          {draft && (
            <form onSubmit={save} className="space-y-2 rounded-lg border p-3">
              <p className="text-xs">
                {draft.end_time == null
                  ? format(draft.timestamp)
                  : t("bookmarks.range", {
                      start: format(draft.timestamp),
                      end: format(draft.end_time),
                    })}
              </p>
              <div className="space-y-1">
                <label htmlFor={`${editorId}-title`}>
                  {t("bookmarks.name")}
                </label>
                <Input
                  id={`${editorId}-title`}
                  value={draft.title}
                  maxLength={128}
                  required
                  disabled={busy}
                  onChange={(event) =>
                    setDraft({ ...draft, title: event.target.value })
                  }
                />
              </div>
              <div className="space-y-1">
                <label htmlFor={`${editorId}-notes`}>
                  {t("bookmarks.notes")}
                </label>
                <Textarea
                  id={`${editorId}-notes`}
                  value={draft.notes}
                  maxLength={4000}
                  rows={3}
                  disabled={busy}
                  onChange={(event) =>
                    setDraft({ ...draft, notes: event.target.value })
                  }
                />
              </div>
              <div className="flex gap-2">
                <Button
                  size="sm"
                  type="submit"
                  disabled={busy || !draft.title.trim()}
                >
                  {t("bookmarks.save")}
                </Button>
                <Button
                  size="sm"
                  type="button"
                  variant="ghost"
                  disabled={busy}
                  onClick={() => setDraft(undefined)}
                >
                  {t("bookmarks.cancel")}
                </Button>
              </div>
            </form>
          )}
          {failure && (
            <p role="alert" className="text-danger">
              {failure}
            </p>
          )}
          {error ? (
            <div role="alert">
              <p>{t("bookmarks.loadFailed")}</p>
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  void mutate().catch(() =>
                    setFailure(t("bookmarks.loadFailed")),
                  );
                }}
              >
                {t("bookmarks.retry")}
              </Button>
            </div>
          ) : !data ? (
            <p role="status">{t("bookmarks.loading")}</p>
          ) : data.items.length === 0 ? (
            <p>{t("bookmarks.empty")}</p>
          ) : (
            <ul className="max-h-72 space-y-2 overflow-y-auto">
              {data.items.map((item) => (
                <li key={item.id} className="rounded-lg border p-3">
                  <p className="font-medium">{item.title}</p>
                  <p className="text-xs text-muted-foreground">
                    {format(item.timestamp)} (
                    {t("bookmarks.author", { owner: item.owner })})
                  </p>
                  <p className="mt-1 whitespace-pre-wrap break-words text-xs">
                    {item.notes}
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {t(
                      item.available_at_anchor
                        ? "bookmarks.indexed"
                        : "bookmarks.missing",
                    )}
                  </p>
                  <div className="mt-2 flex flex-wrap gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={
                        item.timestamp < bounds.after ||
                        item.timestamp >= bounds.before
                      }
                      onClick={() => onSeek(item.timestamp)}
                    >
                      {t("bookmarks.open")}
                    </Button>
                    {item.can_edit && (
                      <>
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={busy}
                          onClick={() => {
                            setDraft(item);
                            setFailure(undefined);
                          }}
                        >
                          {t("bookmarks.edit")}
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={busy}
                          onClick={() => setPendingDelete(item)}
                        >
                          {t("bookmarks.delete")}
                        </Button>
                      </>
                    )}
                  </div>
                  {pendingDelete?.id === item.id && (
                    <div
                      className="mt-2 rounded border p-2"
                      role="group"
                      aria-label={t("bookmarks.confirmDelete")}
                    >
                      <p>{t("bookmarks.deleteHint")}</p>
                      <Button
                        size="sm"
                        variant="destructive"
                        disabled={busy}
                        onClick={() => void remove()}
                      >
                        {t("bookmarks.confirmDelete")}
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={busy}
                        onClick={() => setPendingDelete(undefined)}
                      >
                        {t("bookmarks.cancel")}
                      </Button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="outline"
              disabled={offset === 0 || busy}
              onClick={() => setOffset(Math.max(0, offset - 25))}
            >
              {t("bookmarks.previous")}
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={!data?.has_more || busy}
              onClick={() => setOffset(offset + 25)}
            >
              {t("bookmarks.next")}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={() => {
                void mutate().catch(() =>
                  setFailure(t("bookmarks.refreshFailed")),
                );
              }}
            >
              {t("bookmarks.retry")}
            </Button>
          </div>
        </div>
      )}
    </details>
  );
}
