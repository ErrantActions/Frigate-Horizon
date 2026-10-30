import { useEffect, useState } from "react";
import axios from "axios";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";

type Props = {
  eventId: string | undefined;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSuccess: () => void;
};

export default function UnprotectMediaDialog({
  eventId,
  open,
  onOpenChange,
  onSuccess,
}: Props) {
  const { t } = useTranslation(["views/protectedMedia"]);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    setFailed(false);
  }, [eventId, open]);

  async function unprotect() {
    if (!eventId || busy) return;
    setBusy(true);
    setFailed(false);
    try {
      const response = await axios.delete(
        `events/${encodeURIComponent(eventId)}/retain`,
        { data: { confirm: true, acknowledge_retention: true } },
      );
      if (response.data.success !== true) {
        setFailed(true);
        return;
      }
      onOpenChange(false);
      onSuccess();
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <AlertDialog
      open={open}
      onOpenChange={(value) => {
        if (!busy) onOpenChange(value);
      }}
    >
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{t("confirmTitle")}</AlertDialogTitle>
          <AlertDialogDescription>
            {t("confirmDescription")}
          </AlertDialogDescription>
        </AlertDialogHeader>
        {failed && <p role="alert">{t("unprotectError")}</p>}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>{t("cancel")}</AlertDialogCancel>
          <Button disabled={busy} onClick={() => void unprotect()}>
            {t("confirmUnprotect")}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
