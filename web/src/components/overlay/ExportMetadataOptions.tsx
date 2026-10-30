import { useTranslation } from "react-i18next";
import type { ExportMetadataCategory } from "@/types/export";

const categories: ExportMetadataCategory[] = [
  "camera",
  "events",
  "identities",
  "geometry",
  "environment",
  "notes",
  "provenance",
];

export default function ExportMetadataOptions({
  value,
  onChange,
}: {
  value: ExportMetadataCategory[];
  onChange: (value: ExportMetadataCategory[]) => void;
}) {
  const { t } = useTranslation("components/dialog");
  return (
    <details className="rounded-lg border p-3 text-sm">
      <summary className="cursor-pointer font-medium">
        {t("export.metadata.title")}
      </summary>
      <p className="mt-2 text-xs text-muted-foreground">
        {t("export.metadata.hint")}
      </p>
      <div className="mt-3 grid gap-2 sm:grid-cols-2">
        {categories.map((category) => (
          <label key={category} className="flex items-start gap-2">
            <input
              type="checkbox"
              checked={value.includes(category)}
              onChange={(event) =>
                onChange(
                  event.target.checked
                    ? [...value, category]
                    : value.filter((item) => item !== category),
                )
              }
            />
            {t(`export.metadata.categories.${category}`)}
          </label>
        ))}
      </div>
      <p className="mt-3 text-xs" role="status">
        {t("export.metadata.preview", {
          categories: value.length
            ? value
                .map((category) => t(`export.metadata.categories.${category}`))
                .join(", ")
            : t("export.metadata.none"),
        })}
      </p>
      {(value.includes("identities") ||
        value.includes("camera") ||
        value.includes("notes")) && (
        <p className="mt-2 text-xs text-danger">
          {t("export.metadata.sensitive")}
        </p>
      )}
    </details>
  );
}
