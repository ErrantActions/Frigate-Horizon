import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { useIsAdmin } from "@/hooks/use-is-admin";
import { useOnvif } from "@/hooks/use-onvif";

type RecordingSources = {
  profiles: { token: string; name: string; video_source_token: string }[];
  sources: { source_ref: string; name: string | null }[];
  limit: number;
  attribution_ready: boolean;
};

export default function OnvifRecordingBindingPanel({
  camera,
}: {
  camera: string;
}) {
  const { t } = useTranslation("views/onvif");
  const isAdmin = useIsAdmin();
  const [readBinding, setReadBinding] = useState(false);
  const [discover, setDiscover] = useState(false);
  const [profile, setProfile] = useState("");
  const [source, setSource] = useState("");
  const [copyFailed, setCopyFailed] = useState(false);
  const binding = useOnvif<{ configured: boolean }>(
    camera,
    "recording_binding",
    readBinding,
  );
  const choices = useOnvif<RecordingSources>(
    camera,
    "recording_sources",
    discover,
  );
  const selected = choices.data?.profiles.find(
    (item) => item.token === profile,
  );
  if (!isAdmin)
    return <p className="text-sm">{t("recordings.binding.viewerHelp")}</p>;
  return (
    <details className="rounded border p-3">
      <summary>{t("recordings.binding.title")}</summary>
      <p className="my-2 text-sm">{t("recordings.binding.help")}</p>
      <div className="my-2 flex flex-wrap gap-2">
        <Button
          variant="secondary"
          disabled={binding.isValidating}
          onClick={() => {
            if (readBinding) void binding.mutate();
            else setReadBinding(true);
          }}
        >
          {t("recordings.binding.read")}
        </Button>
        <Button
          variant="secondary"
          disabled={choices.isValidating}
          onClick={() => {
            if (discover) void choices.mutate();
            else setDiscover(true);
          }}
        >
          {t("recordings.binding.discover")}
        </Button>
      </div>
      {binding.data && (
        <p role="status">
          {t(
            binding.data.configured
              ? "recordings.binding.configured"
              : "recordings.binding.required",
          )}
        </p>
      )}
      {choices.data && (
        <div className="flex flex-col gap-2">
          <label>
            {t("recordings.binding.profile")}
            <select
              className="w-full rounded border bg-background p-2"
              value={profile}
              onChange={(event) => setProfile(event.target.value)}
            >
              <option value="">{t("chooseProfile")}</option>
              {choices.data.profiles.map((item) => (
                <option key={item.token} value={item.token}>
                  {item.name} ({item.video_source_token})
                </option>
              ))}
            </select>
          </label>
          <label>
            {t("recordings.binding.source")}
            <select
              className="w-full rounded border bg-background p-2"
              value={source}
              onChange={(event) => setSource(event.target.value)}
            >
              <option value="">{t("recordings.binding.chooseSource")}</option>
              {choices.data.sources.map((item) => (
                <option key={item.source_ref} value={item.source_ref}>
                  {item.name ?? t("unknown")} ({item.source_ref.slice(0, 12)})
                </option>
              ))}
            </select>
          </label>
          {!choices.data.attribution_ready && (
            <p role="status">{t("recordings.binding.unattributed")}</p>
          )}
          {selected && source && choices.data.attribution_ready && (
            <>
              <pre className="overflow-auto rounded bg-background p-2 text-xs">
                {JSON.stringify(
                  {
                    recording_profile: selected.token,
                    recording_video_source_token: selected.video_source_token,
                    recording_source_ref: source,
                  },
                  null,
                  2,
                )}
              </pre>
              <Button
                variant="secondary"
                onClick={async () => {
                  setCopyFailed(false);
                  try {
                    await navigator.clipboard.writeText(
                      JSON.stringify(
                        {
                          recording_profile: selected.token,
                          recording_video_source_token:
                            selected.video_source_token,
                          recording_source_ref: source,
                        },
                        null,
                        2,
                      ),
                    );
                  } catch {
                    setCopyFailed(true);
                  }
                }}
              >
                {t("recordings.binding.copy")}
              </Button>
              <p className="text-sm">{t("recordings.binding.saveHelp")}</p>
            </>
          )}
        </div>
      )}
      {(binding.error || choices.error) && (
        <p role="alert">{t("readFailed")}</p>
      )}
      {copyFailed && <p role="alert">{t("recordings.binding.copyFailed")}</p>}
    </details>
  );
}
