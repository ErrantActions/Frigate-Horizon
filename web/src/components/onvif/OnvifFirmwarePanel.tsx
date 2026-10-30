import axios from "axios";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { onvifPath } from "@/hooks/use-onvif";

type FirmwareState = "acknowledged_unverified" | "unsupported";

export default function OnvifFirmwarePanel({
  camera,
  revision,
  methods,
  disabled,
  onBusyChange,
}: {
  camera: string;
  revision: string;
  methods: string[];
  disabled: boolean;
  onBusyChange: (busy: boolean) => void;
}) {
  const { t } = useTranslation("views/onvif");
  const [method, setMethod] = useState("");
  const [file, setFile] = useState<File>();
  const [confirmed, setConfirmed] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [state, setState] = useState<FirmwareState>();
  const [inputKey, setInputKey] = useState(0);
  if (!methods.length) return null;
  const upload = async () => {
    if (
      busy ||
      disabled ||
      !file ||
      !file.size ||
      file.size > 32 * 1024 * 1024 ||
      !methods.includes(method) ||
      !confirmed ||
      typed !== method
    )
      return;
    setBusy(true);
    onBusyChange(true);
    setFailed(false);
    setState(undefined);
    const body = new FormData();
    body.append("firmware", file);
    body.append("method", method);
    body.append("catalog_revision", revision);
    body.append("typed_confirmation", typed);
    body.append("confirm_device_change", "true");
    try {
      const response = await axios.post<{ state: FirmwareState }>(
        onvifPath(camera, "administration/firmware"),
        body,
        { timeout: 240000 },
      );
      setState(response.data.state);
    } catch {
      setFailed(true);
    } finally {
      setFile(undefined);
      setConfirmed(false);
      setTyped("");
      setInputKey((value) => value + 1);
      setBusy(false);
      onBusyChange(false);
    }
  };
  return (
    <div className="space-y-3 rounded border p-3">
      <h4 className="font-medium">{t("firmware.title")}</h4>
      <p className="text-sm">{t("firmware.warning")}</p>
      <label className="flex flex-col gap-1">
        {t("firmware.method")}
        <select
          className="rounded border bg-background p-2"
          value={method}
          disabled={disabled || busy}
          onChange={(event) => {
            setMethod(event.target.value);
            setTyped("");
            setConfirmed(false);
          }}
        >
          <option value="">{t("firmware.chooseMethod")}</option>
          {methods.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </label>
      <Input
        key={inputKey}
        type="file"
        aria-label={t("firmware.file")}
        disabled={disabled || busy}
        onChange={(event) => {
          setFile(event.target.files?.[0]);
          setConfirmed(false);
          setTyped("");
        }}
      />
      {file && (file.size === 0 || file.size > 32 * 1024 * 1024) && (
        <p role="alert">{t("firmware.size")}</p>
      )}
      <Input
        aria-label={t("firmware.typed")}
        value={typed}
        disabled={disabled || busy}
        onChange={(event) => setTyped(event.target.value)}
      />
      <label className="flex items-start gap-2 text-sm">
        <input
          type="checkbox"
          checked={confirmed}
          disabled={disabled || busy}
          onChange={(event) => setConfirmed(event.target.checked)}
        />
        {t("firmware.confirm")}
      </label>
      <Button
        disabled={
          disabled ||
          busy ||
          !file ||
          !file.size ||
          file.size > 32 * 1024 * 1024 ||
          !method ||
          !confirmed ||
          typed !== method
        }
        onClick={() => void upload()}
      >
        {t(busy ? "firmware.uploading" : "firmware.submit")}
      </Button>
      {failed && <p role="alert">{t("firmware.unverified")}</p>}
      {state && <p role="status">{t(`firmware.${state}`)}</p>}
    </div>
  );
}
