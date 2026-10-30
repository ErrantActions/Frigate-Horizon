import type { RJSFSchema, WidgetProps } from "@rjsf/utils";
import { useTranslation } from "react-i18next";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { ConfigFormContext } from "@/types/configForm";
import { getSizedFieldClassName } from "../utils";

const EMPTY_VALUE = "empty";
const PROFILE_PREFIX = "profile:";

export function CameraProfileWidget(
  props: WidgetProps<unknown, RJSFSchema, ConfigFormContext>,
) {
  const { id, value, disabled, readonly, onChange, options, required } = props;
  const { t } = useTranslation("views/environment");
  const context = props.registry.formContext;
  const profiles = Object.keys(
    context?.fullCameraConfig?.profiles ?? {},
  ).sort();
  const names = context?.fullConfig?.profiles;
  const selected = typeof value === "string" ? value : null;
  const allowAutomatic = options.allowAutomatic === true;
  const allowEmpty = allowAutomatic || !required;
  return (
    <div className={getSizedFieldClassName(options, "md")}>
      <Select
        value={
          selected === null
            ? allowEmpty
              ? EMPTY_VALUE
              : undefined
            : `${PROFILE_PREFIX}${selected}`
        }
        onValueChange={(next) =>
          onChange(
            next === EMPTY_VALUE ? null : next.slice(PROFILE_PREFIX.length),
          )
        }
        disabled={disabled || readonly}
      >
        <SelectTrigger id={id}>
          <SelectValue placeholder={t("chooseProfile")} />
        </SelectTrigger>
        <SelectContent>
          {allowEmpty && (
            <SelectItem value={EMPTY_VALUE}>
              {allowAutomatic ? t("automatic") : t("notSet")}
            </SelectItem>
          )}
          {profiles.map((name) => (
            <SelectItem key={name} value={`${PROFILE_PREFIX}${name}`}>
              {names?.[name]?.friendly_name ?? name}
            </SelectItem>
          ))}
          {selected !== null && !profiles.includes(selected) && (
            <SelectItem value={`${PROFILE_PREFIX}${selected}`} disabled>
              {t("missingProfile", { profile: selected })}
            </SelectItem>
          )}
        </SelectContent>
      </Select>
      {profiles.length === 0 && (
        <p className="mt-1 text-xs text-muted-foreground">
          {t("noCameraProfiles")}
        </p>
      )}
    </div>
  );
}
