import { useTranslation } from "react-i18next";
import type { ExportTimestampMode } from "@/types/export";
import { Label } from "../ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../ui/select";

export default function ExportTimestampOptions({
  value,
  onChange,
}: {
  value: ExportTimestampMode;
  onChange: (value: ExportTimestampMode) => void;
}) {
  const { t } = useTranslation("components/dialog");
  return (
    <div className="space-y-2">
      <Label>{t("export.timestamp.label")}</Label>
      <Select
        value={value}
        onValueChange={(mode: ExportTimestampMode) => onChange(mode)}
      >
        <SelectTrigger aria-label={t("export.timestamp.label")}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {(["none", "subtitle", "burn_in", "both"] as const).map((mode) => (
            <SelectItem key={mode} value={mode}>
              {t(`export.timestamp.${mode}`)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <p className="text-xs text-muted-foreground">
        {t("export.timestamp.hint")}
      </p>
    </div>
  );
}
