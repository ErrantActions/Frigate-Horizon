import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { Trans } from "react-i18next";
import axios from "axios";
import { toast } from "sonner";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import ActivityIndicator from "@/components/indicators/activity-indicator";
import { CameraNameLabel } from "@/components/camera/FriendlyNameLabel";
import { useCameraFriendlyName } from "@/hooks/use-camera-friendly-name";

type DeleteCameraDialogProps = {
  show: boolean;
  cameras: string[];
  initialCamera?: string;
  onClose: () => void;
  onDeleted: () => void;
};

export default function DeleteCameraDialog({
  show,
  cameras,
  initialCamera,
  onClose,
  onDeleted,
}: DeleteCameraDialogProps) {
  const { t } = useTranslation(["views/settings", "common"]);
  const [phase, setPhase] = useState<"select" | "confirm">(
    initialCamera ? "confirm" : "select",
  );
  const [selectedCamera, setSelectedCamera] = useState(initialCamera ?? "");
  const displayName = useCameraFriendlyName(selectedCamera || undefined);
  const [isDeleting, setIsDeleting] = useState(false);

  const handleClose = useCallback(() => {
    if (isDeleting) return;
    setPhase("select");
    setSelectedCamera("");
    onClose();
  }, [isDeleting, onClose]);

  const handleDelete = useCallback(() => {
    setPhase("confirm");
  }, []);

  const handleBack = useCallback(() => {
    setPhase("select");
  }, []);

  const handleConfirmDelete = useCallback(async () => {
    if (!selectedCamera || isDeleting) return;

    setIsDeleting(true);

    try {
      await axios.delete(
        `cameras/${encodeURIComponent(selectedCamera)}/configuration`,
      );
      toast.success(
        t("cameraManagement.deleteCameraDialog.removeSuccess", {
          cameraName: displayName,
        }),
        { position: "top-center" },
      );
      setPhase("select");
      setSelectedCamera("");
      onDeleted();
    } catch (error) {
      const errorMessage =
        axios.isAxiosError(error) &&
        (error.response?.data?.message || error.response?.data?.detail)
          ? error.response?.data?.message || error.response?.data?.detail
          : t("cameraManagement.deleteCameraDialog.error", {
              cameraName: selectedCamera,
            });

      toast.error(errorMessage, { position: "top-center" });
    } finally {
      setIsDeleting(false);
    }
  }, [selectedCamera, displayName, isDeleting, onDeleted, t]);

  return (
    <Dialog open={show} onOpenChange={handleClose}>
      <DialogContent className="sm:max-w-[425px]">
        {phase === "select" ? (
          <>
            <DialogHeader>
              <DialogTitle>
                {t("cameraManagement.deleteCameraDialog.removeTitle")}
              </DialogTitle>
              <DialogDescription>
                {t("cameraManagement.deleteCameraDialog.keepDataDescription")}
              </DialogDescription>
            </DialogHeader>
            <Select value={selectedCamera} onValueChange={setSelectedCamera}>
              <SelectTrigger>
                <SelectValue
                  placeholder={t(
                    "cameraManagement.deleteCameraDialog.selectPlaceholder",
                  )}
                />
              </SelectTrigger>
              <SelectContent>
                {cameras.map((camera) => (
                  <SelectItem key={camera} value={camera}>
                    <CameraNameLabel camera={camera} />{" "}
                    <span className="text-muted-foreground">({camera})</span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <DialogFooter>
              <Button
                aria-label={t("button.cancel", { ns: "common" })}
                onClick={handleClose}
                type="button"
              >
                {t("button.cancel", { ns: "common" })}
              </Button>
              <Button
                variant="destructive"
                aria-label={t("cameraManagement.removeCamera")}
                onClick={handleDelete}
                disabled={!selectedCamera}
              >
                {t("cameraManagement.removeCamera")}
              </Button>
            </DialogFooter>
          </>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>
                {t("cameraManagement.deleteCameraDialog.confirmTitle")}
              </DialogTitle>
              <DialogDescription>
                <Trans
                  ns="views/settings"
                  values={{
                    cameraName:
                      displayName === selectedCamera
                        ? selectedCamera
                        : `${displayName} (${selectedCamera})`,
                  }}
                  components={{ strong: <span className="font-medium" /> }}
                >
                  cameraManagement.deleteCameraDialog.keepDataWarning
                </Trans>
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button
                aria-label={t("button.back", { ns: "common" })}
                onClick={handleBack}
                type="button"
                disabled={isDeleting}
              >
                {t("button.back", { ns: "common" })}
              </Button>
              <Button
                variant="destructive"
                onClick={handleConfirmDelete}
                disabled={isDeleting}
              >
                {isDeleting ? (
                  <div className="flex flex-row items-center gap-2">
                    <ActivityIndicator />
                    <span>
                      {t("cameraManagement.deleteCameraDialog.removeButton")}
                    </span>
                  </div>
                ) : (
                  t("cameraManagement.deleteCameraDialog.removeButton")
                )}
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
