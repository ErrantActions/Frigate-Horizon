import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import { LuSlidersHorizontal } from "react-icons/lu";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { useIsAdmin } from "@/hooks/use-is-admin";
import OnvifImagingPanel from "./OnvifImagingPanel";

export default function LiveImageControls({ camera }: { camera: string }) {
  const { t } = useTranslation("views/onvif");
  const isAdmin = useIsAdmin();
  const [open, setOpen] = useState(false);
  if (!isAdmin) return null;
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="secondary" size="sm" aria-label={t("live.title")}>
          <LuSlidersHorizontal className="size-5" />
        </Button>
      </DialogTrigger>
      <DialogContent
        className="max-h-[90dvh] overflow-y-auto sm:max-w-3xl"
        aria-describedby={undefined}
      >
        <DialogTitle>{t("live.title")}</DialogTitle>
        {open && <OnvifImagingPanel camera={camera} />}
        <Link
          className="text-sm underline"
          to={`/settings?page=cameraEnvironment&camera=${encodeURIComponent(camera)}`}
        >
          {t("profiles.automation")}
        </Link>
      </DialogContent>
    </Dialog>
  );
}
