import { useState, useCallback, useEffect, useMemo } from "react";
import { IoIosWarning } from "react-icons/io";
import { Button } from "@/components/ui/button";
import {
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Label } from "@/components/ui/label";
import {
  FrigateConfig,
  GroupStreamingSettings,
  StreamType,
} from "@/types/frigateConfig";
import ActivityIndicator from "../indicators/activity-indicator";
import useSWR from "swr";
import { LuCheck, LuExternalLink, LuInfo, LuX } from "react-icons/lu";
import { Link } from "react-router-dom";
import { LiveStreamMetadata } from "@/types/live";
import { Trans, useTranslation } from "react-i18next";
import { useDocDomain } from "@/hooks/use-doc-domain";
import { useCameraFriendlyName } from "@/hooks/use-camera-friendly-name";
import { detectCameraAudioFeatures } from "@/utils/cameraUtil";
import { isRestreamedStream } from "@/utils/liveTranscode";
import { dashboardLiveStreams } from "@/utils/adaptiveLive";
import { Switch } from "@/components/ui/switch";

type CameraStreamingDialogProps = {
  camera: string;
  groupStreamingSettings: GroupStreamingSettings;
  streamMetadata?: { [key: string]: LiveStreamMetadata };
  setGroupStreamingSettings: React.Dispatch<
    React.SetStateAction<GroupStreamingSettings>
  >;
  setIsDialogOpen: React.Dispatch<React.SetStateAction<boolean>>;
  onSave?: (settings: GroupStreamingSettings) => void;
};

export function CameraStreamingDialog({
  camera,
  groupStreamingSettings,
  streamMetadata,
  setGroupStreamingSettings,
  setIsDialogOpen,
  onSave,
}: CameraStreamingDialogProps) {
  const { t } = useTranslation([
    "components/camera",
    "components/dialog",
    "views/live",
  ]);

  const { getLocaleDocUrl } = useDocDomain();
  const { data: config } = useSWR<FrigateConfig>("config");

  const cameraName = useCameraFriendlyName(camera);

  const [isLoading, setIsLoading] = useState(false);

  const [streamName, setStreamName] = useState(
    Object.entries(
      config?.cameras[camera]
        ? dashboardLiveStreams(config.cameras[camera])
        : {},
    )[0]?.[1] || "",
  );
  const [streamType, setStreamType] = useState<StreamType>("smart");
  const [forceLowBandwidth, setForceLowBandwidth] = useState(false);

  // metadata

  const isRestreamed = useMemo(
    () => isRestreamedStream(config, streamName),
    [config, streamName],
  );

  // Fetch the go2rtc stream metadata directly when the parent didn't provide it
  // so codec/availability detection works regardless of caller
  const { data: fetchedMetadata } = useSWR<LiveStreamMetadata>(
    isRestreamed && streamName && !streamMetadata?.[streamName]
      ? `go2rtc/streams/${streamName}`
      : null,
    {
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      revalidateIfStale: false,
      dedupingInterval: 60000,
    },
  );

  const cameraMetadata = streamName
    ? (streamMetadata?.[streamName] ?? fetchedMetadata)
    : undefined;

  const { audioOutput: supportsAudioOutput } = useMemo(
    () => detectCameraAudioFeatures(cameraMetadata),
    [cameraMetadata],
  );

  // handlers

  useEffect(() => {
    if (!config) {
      return;
    }

    // Get available streams from the config and first stream entry as fallback
    const availableStreams = config?.cameras[camera]
      ? dashboardLiveStreams(config.cameras[camera])
      : {};
    const firstStreamEntry = Object.entries(availableStreams)[0]?.[1] || "";

    if (groupStreamingSettings && groupStreamingSettings[camera]) {
      const cameraSettings = groupStreamingSettings[camera];
      const streamNameFromSettings = cameraSettings.streamName || "";

      const streamExists =
        streamNameFromSettings &&
        Object.values(availableStreams).includes(streamNameFromSettings);

      setStreamName(streamExists ? streamNameFromSettings : firstStreamEntry);
      setStreamType(cameraSettings.streamType || "smart");
      setForceLowBandwidth(
        cameraSettings.dataSaver ??
          ("playerMode" in cameraSettings &&
            cameraSettings.playerMode === "jsmpeg"),
      );
    } else {
      setStreamName(firstStreamEntry);
      setStreamType("smart");
      setForceLowBandwidth(false);
    }
  }, [groupStreamingSettings, camera, config]);

  const handleSave = useCallback(() => {
    setIsLoading(true);
    const updatedSettings = {
      ...groupStreamingSettings,
      [camera]: {
        streamName,
        streamType,
        dataSaver: forceLowBandwidth,
        playAudio: groupStreamingSettings?.[camera]?.playAudio ?? false,
        volume: groupStreamingSettings?.[camera]?.volume ?? 1,
      },
    };

    setGroupStreamingSettings(updatedSettings);
    setIsDialogOpen(false);
    setIsLoading(false);
    onSave?.(updatedSettings);
  }, [
    groupStreamingSettings,
    setGroupStreamingSettings,
    camera,
    streamName,
    streamType,
    forceLowBandwidth,
    setIsDialogOpen,
    onSave,
  ]);

  const handleCancel = useCallback(() => {
    if (!config) {
      return;
    }

    // Get available streams from the config and first stream entry as fallback
    const availableStreams = config?.cameras[camera]
      ? dashboardLiveStreams(config.cameras[camera])
      : {};
    const firstStreamEntry = Object.entries(availableStreams)[0]?.[1] || "";

    if (groupStreamingSettings && groupStreamingSettings[camera]) {
      const cameraSettings = groupStreamingSettings[camera];
      const streamNameFromSettings = cameraSettings.streamName || "";

      const streamExists =
        streamNameFromSettings &&
        Object.values(availableStreams).includes(streamNameFromSettings);

      setStreamName(streamExists ? streamNameFromSettings : firstStreamEntry);
      setStreamType(cameraSettings.streamType || "smart");
      setForceLowBandwidth(
        cameraSettings.dataSaver ??
          ("playerMode" in cameraSettings &&
            cameraSettings.playerMode === "jsmpeg"),
      );
    } else {
      setStreamName(firstStreamEntry);
      setStreamType("smart");
      setForceLowBandwidth(false);
    }

    setIsDialogOpen(false);
  }, [groupStreamingSettings, camera, config, setIsDialogOpen]);

  if (!config) {
    return null;
  }

  return (
    <DialogContent className="sm:max-w-[425px]">
      <DialogHeader className="mb-4">
        <DialogTitle className="smart-capitalize">
          {t("group.camera.setting.title", {
            cameraName: cameraName,
          })}
        </DialogTitle>
        <DialogDescription>
          <Trans ns="components/camera">group.camera.setting.desc</Trans>
        </DialogDescription>
      </DialogHeader>
      <div className="flex flex-col space-y-8">
        {!isRestreamed && (
          <div className="flex flex-col gap-2">
            <Label></Label>
            <div className="flex flex-row items-center gap-1 text-sm text-muted-foreground">
              <LuX className="size-4 text-danger" />
              <div>
                {t("streaming.restreaming.disabled", {
                  ns: "components/dialog",
                })}
              </div>
              <Popover>
                <PopoverTrigger asChild>
                  <div className="cursor-pointer p-0">
                    <LuInfo className="size-4" />
                    <span className="sr-only">
                      {t("button.info", { ns: "common" })}
                    </span>
                  </div>
                </PopoverTrigger>
                <PopoverContent className="w-80 text-xs">
                  {t("streaming.restreaming.desc.title", {
                    ns: "components/dialog",
                  })}
                  <div className="mt-2 flex items-center text-primary">
                    <Link
                      to={getLocaleDocUrl("configuration/live")}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline"
                    >
                      {t("readTheDocumentation", { ns: "common" })}
                      <LuExternalLink className="ml-2 inline-flex size-3" />
                    </Link>
                  </div>
                </PopoverContent>
              </Popover>
            </div>
          </div>
        )}
        {isRestreamed &&
          Object.entries(config?.cameras[camera].live.streams).length > 0 && (
            <div className="flex flex-col items-start gap-2">
              <Label htmlFor="stream" className="text-right">
                {t("group.camera.setting.stream")}
              </Label>
              <Select
                value={streamName}
                onValueChange={setStreamName}
                disabled={forceLowBandwidth}
              >
                <SelectTrigger className="">
                  <SelectValue
                    placeholder={t("group.camera.setting.placeholder")}
                  />
                </SelectTrigger>
                <SelectContent>
                  {camera !== "birdseye" &&
                    Object.entries(
                      config?.cameras[camera]
                        ? dashboardLiveStreams(config.cameras[camera])
                        : {},
                    ).map(([name, stream]) => (
                      <SelectItem key={stream} value={stream}>
                        {name}
                      </SelectItem>
                    ))}
                </SelectContent>
                {!forceLowBandwidth && (
                  <div className="flex flex-row items-center gap-1 text-sm text-muted-foreground">
                    {supportsAudioOutput ? (
                      <>
                        <LuCheck className="size-4 text-success" />
                        <div>{t("group.camera.setting.audioIsAvailable")}</div>
                      </>
                    ) : (
                      <>
                        <LuX className="size-4 text-danger" />
                        <div>
                          {t("group.camera.setting.audioIsUnavailable")}
                        </div>
                        <Popover>
                          <PopoverTrigger asChild>
                            <div className="cursor-pointer p-0">
                              <LuInfo className="size-4" />
                              <span className="sr-only">
                                {t("button.info", { ns: "common" })}
                              </span>
                            </div>
                          </PopoverTrigger>
                          <PopoverContent className="w-80 text-xs">
                            {t("group.camera.setting.audio.tips.title")}
                            <div className="mt-2 flex items-center text-primary">
                              <Link
                                to={getLocaleDocUrl("configuration/live")}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="inline"
                              >
                                {t("readTheDocumentation", { ns: "common" })}
                                <LuExternalLink className="ml-2 inline-flex size-3" />
                              </Link>
                            </div>
                          </PopoverContent>
                        </Popover>
                      </>
                    )}
                  </div>
                )}
              </Select>
            </div>
          )}
        {isRestreamed &&
          Object.entries(config?.cameras[camera].live.streams).length > 0 && (
            <div className="flex flex-col items-start gap-2">
              <div className="flex w-full items-center justify-between">
                <Label htmlFor="force-low-bandwidth" className="cursor-pointer">
                  {t("stream.lowBandwidth.force.label", { ns: "views/live" })}
                </Label>
                <Switch
                  id="force-low-bandwidth"
                  checked={forceLowBandwidth}
                  onCheckedChange={setForceLowBandwidth}
                />
              </div>
              <p className="text-sm text-muted-foreground">
                {t("stream.lowBandwidth.force.desc", { ns: "views/live" })}
              </p>
            </div>
          )}
        <div className="flex flex-col items-start gap-2">
          <Label htmlFor="streaming-method" className="text-right">
            {t("group.camera.setting.streamMethod.label")}
          </Label>
          <Select
            value={streamType}
            onValueChange={(value) => setStreamType(value as StreamType)}
          >
            <SelectTrigger className="">
              <SelectValue
                placeholder={t("group.camera.setting.streamMethod.placeholder")}
              />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="no-streaming">
                {t(
                  "group.camera.setting.streamMethod.method.noStreaming.label",
                )}
              </SelectItem>
              <SelectItem value="smart">
                {t(
                  "group.camera.setting.streamMethod.method.smartStreaming.label",
                )}
              </SelectItem>
              <SelectItem value="continuous">
                {t(
                  "group.camera.setting.streamMethod.method.continuousStreaming.label",
                )}
              </SelectItem>
            </SelectContent>
          </Select>
          {streamType === "no-streaming" && (
            <p className="text-sm text-muted-foreground">
              {t("group.camera.setting.streamMethod.method.noStreaming.desc")}
            </p>
          )}
          {streamType === "smart" && (
            <p className="text-sm text-muted-foreground">
              {t(
                "group.camera.setting.streamMethod.method.smartStreaming.desc",
              )}
            </p>
          )}
          {streamType === "continuous" && (
            <>
              <p className="text-sm text-muted-foreground">
                {t(
                  "group.camera.setting.streamMethod.method.continuousStreaming.desc.title",
                )}
              </p>
              <div className="flex items-center gap-2">
                <IoIosWarning className="mr-2 size-5 text-danger" />
                <div className="max-w-[85%] text-sm">
                  {t(
                    "group.camera.setting.streamMethod.method.continuousStreaming.desc.warning",
                  )}
                </div>
              </div>
            </>
          )}
        </div>
      </div>
      <div className="flex flex-1 flex-col justify-end">
        <div className="flex flex-row gap-2 pt-5">
          <Button
            className="flex flex-1"
            aria-label={t("button.cancel", { ns: "common" })}
            onClick={handleCancel}
          >
            {t("button.cancel", { ns: "common" })}
          </Button>
          <Button
            variant="select"
            aria-label={t("button.save", { ns: "common" })}
            disabled={isLoading}
            className="flex flex-1"
            onClick={handleSave}
          >
            {isLoading ? (
              <div className="flex flex-row items-center gap-2">
                <ActivityIndicator className="size-4" />
                <span>{t("button.saving", { ns: "common" })}</span>
              </div>
            ) : (
              t("button.save", { ns: "common" })
            )}
          </Button>
        </div>
      </div>
    </DialogContent>
  );
}
