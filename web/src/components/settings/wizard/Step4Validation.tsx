import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { useTranslation } from "react-i18next";
import { LuInfo } from "react-icons/lu";
import { useState, useCallback, useMemo } from "react";
import ActivityIndicator from "@/components/indicators/activity-indicator";
import axios from "axios";
import { toast } from "sonner";
import { WizardFormData, StreamConfig, TestResult } from "@/types/cameraWizard";
import { FaCircleCheck, FaTriangleExclamation } from "react-icons/fa6";
import { LuX } from "react-icons/lu";
import { Card, CardContent } from "../../ui/card";
import { maskUri } from "@/utils/cameraUtil";
import { ffprobeToTestResult, getStreamIssues } from "@/utils/streamIssues";

type Step4ValidationProps = {
  wizardData: Partial<WizardFormData>;
  onUpdate: (data: Partial<WizardFormData>) => void;
  onSave: (config: WizardFormData) => void;
  onBack?: () => void;
  isLoading?: boolean;
};

export default function Step4Validation({
  wizardData,
  onUpdate,
  onSave,
  onBack,
  isLoading = false,
}: Step4ValidationProps) {
  const { t } = useTranslation(["views/settings"]);
  const [isValidating, setIsValidating] = useState(false);
  const [testingStreams, setTestingStreams] = useState<Set<string>>(new Set());

  const streams = useMemo(() => wizardData.streams || [], [wizardData.streams]);

  // Use test results from Step 2, but allow re-validation in Step 3
  const validationResults = useMemo(() => {
    const results = new Map<string, TestResult>();
    streams.forEach((stream) => {
      if (stream.testResult) {
        results.set(stream.id, stream.testResult);
      }
    });
    return results;
  }, [streams]);

  const performStreamValidation = useCallback(
    async (stream: StreamConfig): Promise<TestResult> => {
      try {
        const response = await axios.get("ffprobe", {
          params: { paths: stream.url, detailed: true },
          timeout: 10000,
        });

        return ffprobeToTestResult(response.data?.[0]);
      } catch (error) {
        const axiosError = error as {
          response?: { data?: { message?: string; detail?: string } };
          message?: string;
        };
        const errorMessage =
          axiosError.response?.data?.message ||
          axiosError.response?.data?.detail ||
          axiosError.message ||
          "Connection failed";

        return { success: false, error: errorMessage };
      }
    },
    [],
  );

  const validateStream = useCallback(
    async (stream: StreamConfig) => {
      if (!stream.url.trim()) {
        toast.error(t("cameraWizard.commonErrors.noUrl"));
        return;
      }

      setTestingStreams((prev) => new Set(prev).add(stream.id));

      const testResult = await performStreamValidation(stream);

      onUpdate({
        streams: streams.map((s) =>
          s.id === stream.id ? { ...s, testResult } : s,
        ),
      });

      if (testResult.success) {
        toast.success(
          t("cameraWizard.step4.streamValidated", {
            number: streams.findIndex((s) => s.id === stream.id) + 1,
          }),
        );
      } else {
        toast.error(
          t("cameraWizard.step4.streamValidationFailed", {
            number: streams.findIndex((s) => s.id === stream.id) + 1,
          }),
        );
      }

      setTestingStreams((prev) => {
        const newSet = new Set(prev);
        newSet.delete(stream.id);
        return newSet;
      });
    },
    [streams, onUpdate, t, performStreamValidation],
  );

  const validateAllStreams = useCallback(async () => {
    setIsValidating(true);
    const results = new Map<string, TestResult>();

    // Only test streams that haven't been tested or failed
    const streamsToTest = streams.filter(
      (stream) => !stream.testResult || !stream.testResult.success,
    );

    for (const stream of streamsToTest) {
      if (!stream.url.trim()) continue;

      const testResult = await performStreamValidation(stream);
      results.set(stream.id, testResult);
    }

    // Update wizard data with new test results
    if (results.size > 0) {
      const updatedStreams = streams.map((stream) => {
        const newResult = results.get(stream.id);
        if (newResult) {
          return { ...stream, testResult: newResult };
        }
        return stream;
      });

      onUpdate({ streams: updatedStreams });
    }

    setIsValidating(false);

    if (results.size > 0) {
      const successfulTests = Array.from(results.values()).filter(
        (r) => r.success,
      ).length;
      if (successfulTests === results.size) {
        toast.success(t("cameraWizard.step4.reconnectionSuccess"));
      } else {
        toast.warning(t("cameraWizard.step4.reconnectionPartial"));
      }
    }
  }, [streams, onUpdate, t, performStreamValidation]);

  const handleSave = useCallback(() => {
    if (!wizardData.cameraName || !wizardData.streams?.length) {
      toast.error(t("cameraWizard.step4.saveError"));
      return;
    }

    // Convert wizard data to final config format
    const configData = {
      cameraName: wizardData.cameraName,
      host: wizardData.host,
      username: wizardData.username,
      password: wizardData.password,
      brandTemplate: wizardData.brandTemplate,
      customUrl: wizardData.customUrl,
      streams: wizardData.streams,
      appleCompatibility: wizardData.appleCompatibility,
      onvif: wizardData.onvif,
    };

    onSave(configData);
  }, [wizardData, onSave, t]);

  const canSave = useMemo(() => {
    return (
      wizardData.cameraName &&
      wizardData.streams?.length &&
      wizardData.streams.some((s) => s.roles.includes("detect")) &&
      wizardData.streams.every((s) => s.testResult) // All streams must be tested
    );
  }, [wizardData]);

  return (
    <div className="space-y-6">
      <div className="text-sm text-muted-foreground">
        {t("cameraWizard.step4.description")}
      </div>

      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-lg font-medium">
            {t("cameraWizard.step4.validationTitle")}
          </h3>
          <Button
            onClick={validateAllStreams}
            disabled={isValidating || streams.length === 0}
            variant="outline"
          >
            {isValidating && <ActivityIndicator className="mr-2 size-4" />}
            {isValidating
              ? t("cameraWizard.step4.connecting")
              : t("cameraWizard.step4.connectAllStreams")}
          </Button>
        </div>

        <div className="space-y-3">
          {streams.map((stream, index) => {
            const result = validationResults.get(stream.id);
            return (
              <Card key={stream.id} className="bg-secondary text-primary">
                <CardContent className="space-y-4 p-4">
                  <div className="mb-2 flex items-center justify-between">
                    <div className="flex items-end gap-2">
                      <div className="flex flex-col space-y-1">
                        <div className="flex flex-row items-center">
                          <h4 className="mr-2 font-medium">
                            {t("cameraWizard.step4.streamTitle", {
                              number: index + 1,
                            })}
                          </h4>
                          {stream.roles.map((role) => (
                            <Badge
                              variant="outline"
                              key={role}
                              className="mx-1 text-xs"
                            >
                              {role}
                            </Badge>
                          ))}
                        </div>
                        {result && result.success && (
                          <div className="mb-2 text-sm text-muted-foreground">
                            {[
                              result.resolution,
                              result.fps
                                ? `${result.fps} ${t("cameraWizard.testResultLabels.fps")}`
                                : null,
                              result.videoCodec,
                              result.audioCodec,
                            ]
                              .filter(Boolean)
                              .join(" · ")}
                          </div>
                        )}
                      </div>
                    </div>
                    {result?.success && (
                      <div className="flex items-center gap-2 text-sm">
                        <FaCircleCheck className="size-4 text-success" />
                        <span className="text-success">
                          {t("cameraWizard.step2.connected")}
                        </span>
                      </div>
                    )}
                    {result && !result.success && (
                      <div className="flex items-center gap-2 text-sm">
                        <LuX className="size-4 text-danger" />
                        <span className="text-danger">
                          {t("cameraWizard.step2.notConnected")}
                        </span>
                      </div>
                    )}
                  </div>

                  {result?.success && (
                    <p className="mb-3 text-sm text-muted-foreground">
                      {t("cameraWizard.step4.hlsPreviewAfterSave")}
                    </p>
                  )}

                  {result?.success && (
                    <div className="mb-3 flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <span className="text-sm">
                          {t("cameraWizard.step4.ffmpegModule")}
                        </span>
                        <Popover>
                          <PopoverTrigger asChild>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-4 w-4 p-0"
                            >
                              <LuInfo className="size-3" />
                            </Button>
                          </PopoverTrigger>
                          <PopoverContent className="pointer-events-auto w-80 text-xs">
                            <div className="space-y-2">
                              <div className="font-medium">
                                {t("cameraWizard.step4.ffmpegModule")}
                              </div>
                              <div className="text-muted-foreground">
                                {t(
                                  "cameraWizard.step4.ffmpegModuleDescription",
                                )}
                              </div>
                            </div>
                          </PopoverContent>
                        </Popover>
                      </div>
                      <Switch
                        checked={stream.useFfmpeg || false}
                        onCheckedChange={(checked) => {
                          onUpdate({
                            streams: streams.map((s) =>
                              s.id === stream.id
                                ? { ...s, useFfmpeg: checked }
                                : s,
                            ),
                          });
                        }}
                      />
                    </div>
                  )}

                  <div className="mb-2 flex flex-col justify-between gap-1 md:flex-row md:items-center">
                    <span className="break-all text-sm text-muted-foreground">
                      {maskUri(stream.url)}
                    </span>
                    <Button
                      onClick={() => {
                        if (result?.success) {
                          // Disconnect: clear the test result
                          onUpdate({
                            streams: streams.map((s) =>
                              s.id === stream.id
                                ? { ...s, testResult: undefined }
                                : s,
                            ),
                          });
                        } else {
                          // Test/Connect: perform validation
                          validateStream(stream);
                        }
                      }}
                      disabled={
                        testingStreams.has(stream.id) || !stream.url.trim()
                      }
                      variant="outline"
                      size="sm"
                    >
                      {testingStreams.has(stream.id) && (
                        <ActivityIndicator className="mr-2 size-4" />
                      )}
                      {result?.success
                        ? t("cameraWizard.step4.disconnectStream")
                        : testingStreams.has(stream.id)
                          ? t("cameraWizard.step4.connectingStream")
                          : t("cameraWizard.step4.connectStream")}
                    </Button>
                  </div>

                  {result && (
                    <div className="space-y-2">
                      <div className="text-xs">
                        {t("cameraWizard.step4.issues.title")}
                      </div>
                      <div className="rounded-lg bg-background p-3">
                        <StreamIssues stream={stream} wizardData={wizardData} />
                      </div>
                    </div>
                  )}

                  {result && !result.success && (
                    <div className="rounded-md border border-danger/20 bg-danger/10 p-3 text-sm text-danger">
                      <div className="font-medium">
                        {t("cameraWizard.step2.testFailedTitle")}
                      </div>
                      <div className="mt-1 text-xs">{result.error}</div>
                    </div>
                  )}
                </CardContent>
              </Card>
            );
          })}
        </div>
      </div>

      <div className="flex flex-col-reverse gap-2 pt-6 sm:flex-row sm:justify-end">
        {onBack && (
          <Button type="button" onClick={onBack} className="sm:flex-1">
            {t("button.back", { ns: "common" })}
          </Button>
        )}
        <Button
          type="button"
          onClick={handleSave}
          disabled={!canSave || isLoading}
          className="sm:flex-1"
          variant="select"
        >
          {isLoading && <ActivityIndicator className="mr-2 size-4" />}
          {isLoading
            ? t("button.saving", { ns: "common" })
            : t("cameraWizard.step4.saveAndApply")}
        </Button>
      </div>
    </div>
  );
}

type StreamIssuesProps = {
  stream: StreamConfig;
  wizardData: Partial<WizardFormData>;
};

function StreamIssues({ stream, wizardData }: StreamIssuesProps) {
  const { t } = useTranslation(["views/settings"]);

  const issues = useMemo(
    () =>
      getStreamIssues(
        {
          url: stream.url,
          roles: stream.roles,
          brand: wizardData.brandTemplate,
          reolinkProtocol: wizardData.reolinkProtocol,
          useFfmpeg: stream.useFfmpeg,
          restream: stream.restream,
          testResult: stream.testResult,
        },
        t,
      ),
    [stream, wizardData, t],
  );

  if (issues.length === 0) {
    return null;
  }

  return (
    <div className="space-y-2">
      <div className="space-y-1">
        {issues.map((issue, index) => (
          <div key={index} className="flex items-center gap-2 text-sm">
            {issue.type === "good" && (
              <FaCircleCheck className="size-4 flex-shrink-0 text-success" />
            )}
            {issue.type === "warning" && (
              <FaTriangleExclamation className="size-4 flex-shrink-0 text-yellow-500" />
            )}
            {issue.type === "error" && (
              <LuX className="size-4 flex-shrink-0 text-danger" />
            )}
            <span
              className={
                issue.type === "good"
                  ? "text-success"
                  : issue.type === "warning"
                    ? "text-yellow-500"
                    : "text-danger"
              }
            >
              {issue.message}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
