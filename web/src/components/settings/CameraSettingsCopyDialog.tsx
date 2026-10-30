import { useMemo, useState } from "react";
import axios from "axios";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import type { RJSFSchema } from "@rjsf/utils";
import type { CameraConfig, FrigateConfig } from "@/types/frigateConfig";
import {
  CAMERA_COPY_CATEGORIES,
  CameraCopyCategory,
  buildCameraSettingsCopyPlan,
} from "@/utils/cameraSettingsCopy";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import SaveAllPreviewPopover from "@/components/overlay/detail/SaveAllPreviewPopover";
import ActivityIndicator from "@/components/indicators/activity-indicator";

type CameraSettingsCopyDialogProps = {
  sourceCamera: string;
  cameras: CameraConfig[];
  config: FrigateConfig;
  schema: RJSFSchema;
  pendingKeys: string[];
  onClose: () => void;
  onSaved: (needsRestart: boolean) => Promise<void>;
};

export default function CameraSettingsCopyDialog({
  sourceCamera,
  cameras,
  config,
  schema,
  pendingKeys,
  onClose,
  onSaved,
}: CameraSettingsCopyDialogProps) {
  const { t } = useTranslation(["views/settings", "common"]);
  const [targets, setTargets] = useState<string[]>([]);
  const [categories, setCategories] = useState<CameraCopyCategory[]>([]);
  const [saving, setSaving] = useState(false);
  const [needsRefresh, setNeedsRefresh] = useState(false);
  const pendingCameras = useMemo(
    () =>
      new Set(
        pendingKeys
          .filter((key) => key.includes("::"))
          .map((key) => key.slice(0, key.indexOf("::"))),
      ),
    [pendingKeys],
  );
  const source = config.cameras[sourceCamera];
  const destinations = cameras.filter((camera) => camera.name !== sourceCamera);
  const eligibleTargets = destinations
    .filter((camera) => !pendingCameras.has(camera.name))
    .map((camera) => camera.name);
  const blockedByDraft =
    pendingCameras.has(sourceCamera) ||
    targets.some((camera) => pendingCameras.has(camera));
  const plan = useMemo(
    () =>
      buildCameraSettingsCopyPlan({
        config,
        schema,
        sourceCamera,
        targets,
        categories,
      }),
    [config, schema, sourceCamera, targets, categories],
  );

  const refresh = async () => {
    setSaving(true);
    try {
      await onSaved(false);
    } catch (error) {
      if (!axios.isAxiosError(error)) throw error;
      toast.error(t("cameraCopy.refreshFailed"));
      return;
    } finally {
      setSaving(false);
    }
    setNeedsRefresh(false);
  };

  const apply = async () => {
    if (
      blockedByDraft ||
      needsRefresh ||
      !plan.preview.length ||
      plan.issues.length
    ) {
      toast.error(t("cameraCopy.invalidSelection"));
      return;
    }
    setSaving(true);
    try {
      let response;
      try {
        response = await axios.put<{
          success: boolean;
          go2rtc_synced?: boolean;
        }>("config/set", {
          requires_restart: plan.needsRestart ? 1 : 0,
          update_topics: plan.updateTopics,
          config_data: plan.configData,
          ...(plan.replacePaths.length
            ? { replace_paths: plan.replacePaths }
            : {}),
        });
      } catch (error) {
        if (!axios.isAxiosError(error)) throw error;
        setNeedsRefresh(true);
        toast.error(t("cameraCopy.failed"));
        return;
      }
      if (response.data.success !== true) {
        setNeedsRefresh(true);
        toast.error(t("cameraCopy.failed"));
        return;
      }
      try {
        await onSaved(plan.needsRestart);
      } catch (error) {
        if (!axios.isAxiosError(error)) throw error;
        toast.warning(t("cameraCopy.savedRefreshFailed"));
        onClose();
        return;
      }
      if (response.data.go2rtc_synced === false) {
        toast.warning(t("cameraCopy.savedSyncFailed"));
      } else {
        toast.success(t("cameraCopy.saved", { count: targets.length }));
      }
      onClose();
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open onOpenChange={(open) => !open && !saving && onClose()}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t("cameraCopy.title")}</DialogTitle>
          <DialogDescription>
            {t("cameraCopy.description", {
              cameraName: source?.friendly_name || sourceCamera,
            })}
          </DialogDescription>
        </DialogHeader>
        <p className="text-sm text-muted-foreground">
          {t("cameraCopy.protectedFields")}
        </p>
        {pendingCameras.has(sourceCamera) && (
          <p role="alert" className="text-sm text-destructive">
            {t("cameraCopy.unsavedSource")}
          </p>
        )}
        <fieldset disabled={saving} className="space-y-3">
          <legend className="mb-2 text-sm font-medium">
            {t("cameraCopy.targets")}
          </legend>
          <Button
            variant="outline"
            size="sm"
            onClick={() =>
              setTargets(
                eligibleTargets.every((camera) => targets.includes(camera))
                  ? []
                  : eligibleTargets,
              )
            }
          >
            {t("cameraCopy.toggleTargets")}
          </Button>
          <div className="grid gap-3 sm:grid-cols-2">
            {destinations.map((camera) => (
              <div key={camera.name} className="flex items-start gap-2">
                <Checkbox
                  id={`copy-target-${camera.name}`}
                  disabled={pendingCameras.has(camera.name)}
                  checked={targets.includes(camera.name)}
                  onCheckedChange={(checked) =>
                    setTargets((previous) =>
                      checked === true
                        ? [...previous, camera.name]
                        : previous.filter((name) => name !== camera.name),
                    )
                  }
                />
                <Label htmlFor={`copy-target-${camera.name}`}>
                  {camera.friendly_name || camera.name}
                  {pendingCameras.has(camera.name) && (
                    <span className="block text-xs text-unsaved">
                      {t("cameraCopy.unsavedTarget")}
                    </span>
                  )}
                </Label>
              </div>
            ))}
          </div>
        </fieldset>
        <fieldset disabled={saving} className="space-y-3">
          <legend className="mb-2 text-sm font-medium">
            {t("cameraCopy.sections")}
          </legend>
          <div className="grid gap-3 sm:grid-cols-2">
            {(Object.keys(CAMERA_COPY_CATEGORIES) as CameraCopyCategory[]).map(
              (category) => (
                <div key={category} className="flex items-center gap-2">
                  <Checkbox
                    id={`copy-section-${category}`}
                    checked={categories.includes(category)}
                    onCheckedChange={(checked) =>
                      setCategories((previous) =>
                        checked === true
                          ? [...previous, category]
                          : previous.filter((key) => key !== category),
                      )
                    }
                  />
                  <Label htmlFor={`copy-section-${category}`}>
                    {t("cameraCopy.category." + category)}
                  </Label>
                </div>
              ),
            )}
          </div>
        </fieldset>
        {plan.issues.length > 0 && (
          <p role="alert" className="text-sm text-destructive">
            {t("cameraCopy.unsupported")}
          </p>
        )}
        {needsRefresh && (
          <div role="alert" className="space-y-2 text-sm text-destructive">
            <p>{t("cameraCopy.failed")}</p>
            <Button variant="outline" disabled={saving} onClick={refresh}>
              {t("cameraCopy.refresh")}
            </Button>
          </div>
        )}
        {plan.needsRestart && (
          <p role="status" className="text-sm text-warning">
            {t("cameraCopy.restartRequired")}
          </p>
        )}
        <div className="flex items-center justify-between gap-2 text-sm">
          <span>
            {targets.length && categories.length
              ? t("cameraCopy.preview", { count: plan.preview.length })
              : t("cameraCopy.selectPrompt")}
          </span>
          <SaveAllPreviewPopover items={plan.preview} />
        </div>
        <DialogFooter>
          <Button variant="outline" disabled={saving} onClick={onClose}>
            {t("button.cancel", { ns: "common" })}
          </Button>
          <Button
            disabled={
              saving ||
              blockedByDraft ||
              needsRefresh ||
              !plan.preview.length ||
              !!plan.issues.length
            }
            onClick={apply}
          >
            {saving ? (
              <ActivityIndicator size={18} />
            ) : (
              t("cameraCopy.apply", { count: targets.length })
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
