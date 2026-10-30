import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import OnvifImagingPanel from "@/components/onvif/OnvifImagingPanel";
import OnvifEncoderPanel from "@/components/onvif/OnvifEncoderPanel";
import OnvifAudioPanel from "@/components/onvif/OnvifAudioPanel";
import OnvifFocusPanel from "@/components/onvif/OnvifFocusPanel";
import OnvifProfilesPanel from "@/components/onvif/OnvifProfilesPanel";
import OnvifAdministrationPanel from "@/components/onvif/OnvifAdministrationPanel";
import {
  OnvifEventsPanel,
  OnvifPresetPanel,
  OnvifRecordingsPanel,
} from "@/components/onvif/OnvifServicesPanel";
import { useOnvif, useOnvifMutation } from "@/hooks/use-onvif";
import type { DeviceStatus } from "@/types/onvif";
import { SingleSectionPage, type SettingsPageProps } from "./SingleSectionPage";

export default function CameraOnvifView(
  props: SettingsPageProps & { camera?: string },
) {
  const camera = props.camera ?? props.selectedCamera;
  return (
    <div className="flex flex-col gap-6">
      <SingleSectionPage
        {...props}
        selectedCamera={camera}
        sectionKey="onvif"
        level="camera"
      />
      {camera && <CameraDeviceControls key={camera} camera={camera} />}
    </div>
  );
}

function CameraDeviceControls({ camera }: { camera: string }) {
  const { t } = useTranslation("views/onvif");
  const [connected, setConnected] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [devices, setDevices] = useState<
    { host: string; port: number; address: string }[]
  >([]);
  const status = useOnvif<DeviceStatus>(camera, "status", connected);
  const mutation = useOnvifMutation();
  return (
    <div className="flex flex-col gap-4">
      <h2 className="text-xl font-medium">{t("title")}</h2>
      <p className="text-sm">{t("savedConnection")}</p>
      <details className="rounded-lg border p-4">
        <summary>{t("discovery.title")}</summary>
        <p className="my-2 text-sm">{t("discovery.help")}</p>
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(event) => setConfirmed(event.target.checked)}
          />
          {t("discovery.confirm")}
        </label>
        <Button
          className="my-2"
          disabled={!confirmed || mutation.busy}
          onClick={async () => {
            const result = await mutation.run<{ devices: typeof devices }>(
              "post",
              "onvif/discover",
              { confirm_device_change: true },
            );
            if (result) setDevices(result.devices);
            setConfirmed(false);
          }}
        >
          {t("discovery.search")}
        </Button>
        <ul>
          {devices.map((device) => (
            <li key={device.address}>
              {device.host}:{device.port}
            </li>
          ))}
        </ul>
        {mutation.failed && <p role="alert">{t("readFailed")}</p>}
      </details>
      <Button
        variant="secondary"
        disabled={status.isValidating}
        onClick={() => {
          if (connected) void status.mutate();
          else setConnected(true);
        }}
      >
        {t("status.load")}
      </Button>
      {status.error && <p role="alert">{t("readFailed")}</p>}
      {status.data && (
        <ul className="grid gap-2 text-sm sm:grid-cols-2">
          {Object.entries(status.data.services).map(([name, supported]) => (
            <li key={name}>
              {t(`services.${name}`)}:{" "}
              {t(supported ? "advertised" : "notAdvertised")}
            </li>
          ))}
        </ul>
      )}
      <OnvifProfilesPanel key={`profiles-${camera}`} camera={camera} />
      <OnvifImagingPanel camera={camera} />
      <OnvifFocusPanel key={`focus-${camera}`} camera={camera} />
      <p className="text-sm">
        <Link
          className="underline"
          to={`/settings?page=cameraEnvironment&camera=${encodeURIComponent(camera)}`}
        >
          {t("profiles.automation")}
        </Link>{" "}
        {t("profiles.automationHelp")}
      </p>
      <OnvifEncoderPanel camera={camera} />
      <OnvifAudioPanel key={`audio-${camera}`} camera={camera} />
      <OnvifPresetPanel camera={camera} />
      <OnvifEventsPanel camera={camera} />
      <OnvifRecordingsPanel camera={camera} />
      <OnvifAdministrationPanel
        key={`administration-${camera}`}
        camera={camera}
      />
    </div>
  );
}
