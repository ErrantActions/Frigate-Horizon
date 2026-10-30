import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import ActivityIndicator from "@/components/indicators/activity-indicator";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useUserPersistence } from "@/hooks/use-user-persistence";
import { cn } from "@/lib/utils";
import type {
  CameraEnvironmentStatus,
  WeatherSnapshot,
  WeatherStatus,
} from "@/types/environment";
import type { FrigateConfig } from "@/types/frigateConfig";
import { useTranslation } from "react-i18next";
import { useId } from "react";
import { Link } from "react-router-dom";
import useSWR from "swr";
import { SingleSectionPage, type SettingsPageProps } from "./SingleSectionPage";

function StatusFailure() {
  const { t } = useTranslation("views/environment");
  return (
    <p role="alert" className="text-sm text-danger">
      {t("loadFailed")}
    </p>
  );
}

export function WeatherStatusCard() {
  const { t, i18n } = useTranslation("views/environment");
  const unitSelectorId = useId();
  const [unitSystem, setUnitSystem, unitsLoaded] = useUserPersistence<
    "system" | "metric" | "imperial"
  >("weather-unit-system", "system");
  const { data, error } = useSWR<WeatherStatus>("environment/weather", {
    refreshInterval: 5000,
  });
  const { data: config } = useSWR<FrigateConfig>("config");
  const imperial =
    unitSystem === "imperial" ||
    (unitSystem !== "metric" && config?.ui.unit_system === "imperial");
  const unit = (metric: string, other: string) =>
    t(`units.${imperial ? other : metric}`);
  const number = (value: number | null | undefined, suffix: string) =>
    value === null || value === undefined
      ? t("unknown")
      : `${value.toLocaleString(i18n.language, { maximumFractionDigits: 2 })} ${suffix}`;
  const timeOptions: Intl.DateTimeFormatOptions = {
    timeZone: config?.ui.timezone ?? undefined,
    hour12:
      config?.ui.time_format === "12hour"
        ? true
        : config?.ui.time_format === "24hour"
          ? false
          : undefined,
  };
  const timestamp = (value: string | null | undefined, clockOnly = false) =>
    value
      ? new Date(value).toLocaleString(i18n.language, {
          ...timeOptions,
          ...(clockOnly
            ? { hour: "numeric", minute: "2-digit" }
            : { dateStyle: "short", timeStyle: "short" }),
        })
      : t("unknown");
  const dateKey = (value: string | Date) =>
    new Date(value).toLocaleDateString("en-CA", {
      timeZone: config?.ui.timezone ?? undefined,
    });
  const today = dateKey(new Date());
  const solarTime = (event: "sunrise" | "sunset") =>
    data?.data?.solar_days
      .map((day) => day[event])
      .find((value) => value && dateKey(value) === today);
  const rain =
    data?.data?.rain_mm !== null &&
    data?.data?.showers_mm !== null &&
    data?.data?.rain_mm !== undefined &&
    data?.data?.showers_mm !== undefined
      ? data.data.rain_mm + data.data.showers_mm
      : null;
  const rows = (sample: WeatherSnapshot): [string, string][] => [
    [
      t("temperature"),
      number(
        sample.temperature_c === null
          ? null
          : imperial
            ? (sample.temperature_c * 9) / 5 + 32
            : sample.temperature_c,
        unit("celsius", "fahrenheit"),
      ),
    ],
    [
      t("rain"),
      number(
        rain === null ? null : imperial ? rain / 25.4 : rain,
        unit("millimeters", "inches"),
      ),
    ],
    [
      t("snow"),
      number(
        sample.snowfall_cm === null
          ? null
          : imperial
            ? sample.snowfall_cm / 2.54
            : sample.snowfall_cm,
        unit("centimeters", "inches"),
      ),
    ],
    [
      t("fog"),
      sample.weather_code !== null && [45, 48].includes(sample.weather_code)
        ? t("yes")
        : sample.visibility_m === null
          ? t("unknown")
          : sample.visibility_m <= 1000
            ? t("yes")
            : t("no"),
    ],
    [t("cloud"), number(sample.cloud_cover_percent, t("units.percent"))],
    [
      t("visibility"),
      number(
        sample.visibility_m === null
          ? null
          : imperial
            ? sample.visibility_m / 1609.344
            : sample.visibility_m,
        unit("meters", "miles"),
      ),
    ],
    [
      t("wind"),
      number(
        sample.wind_speed_mps === null
          ? null
          : imperial
            ? sample.wind_speed_mps * 2.2369362921
            : sample.wind_speed_mps,
        unit("metersPerSecond", "milesPerHour"),
      ),
    ],
    [
      t("gusts"),
      number(
        sample.wind_gusts_mps === null
          ? null
          : imperial
            ? sample.wind_gusts_mps * 2.2369362921
            : sample.wind_gusts_mps,
        unit("metersPerSecond", "milesPerHour"),
      ),
    ],
    [t("sunrise"), timestamp(solarTime("sunrise"), true)],
    [t("sunset"), timestamp(solarTime("sunset"), true)],
  ];

  return (
    <section
      className="rounded-xl border bg-background_alt p-4"
      aria-labelledby="modeled-weather-heading"
    >
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h4 id="modeled-weather-heading" className="font-medium">
          {t("weatherTitle")}
        </h4>
        {data?.state && (
          <Badge variant="secondary">{t(`weatherStates.${data.state}`)}</Badge>
        )}
      </div>
      <p className="mb-3 text-sm text-muted-foreground">
        {t("weatherDescription")}
      </p>
      <div className="mb-4 space-y-2">
        <Label htmlFor={unitSelectorId}>{t("displayUnits.label")}</Label>
        <Select
          value={unitSystem ?? "system"}
          disabled={!unitsLoaded}
          onValueChange={(value) => {
            if (
              value === "system" ||
              value === "metric" ||
              value === "imperial"
            ) {
              setUnitSystem(value);
            }
          }}
        >
          <SelectTrigger id={unitSelectorId} className="w-full sm:w-64">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="system">{t("displayUnits.system")}</SelectItem>
            <SelectItem value="metric">{t("displayUnits.metric")}</SelectItem>
            <SelectItem value="imperial">
              {t("displayUnits.imperial")}
            </SelectItem>
          </SelectContent>
        </Select>
        <p className="text-xs text-muted-foreground">
          {t("displayUnits.description")}
        </p>
      </div>
      {error || (data && typeof data.state !== "string") ? (
        <StatusFailure />
      ) : !data ? (
        <ActivityIndicator className="size-5" />
      ) : (
        <>
          {!data.service_running && (
            <p role="alert" className="mb-2 text-sm text-danger">
              {t("serviceStopped")}
            </p>
          )}
          {data.error_code === "weather_location_not_found" && (
            <p role="alert" className="mb-2 text-sm text-danger">
              {t("zipCodeNotFound")}
            </p>
          )}
          {data.data ? (
            <>
              <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-3 lg:grid-cols-5">
                {rows(data.data).map(([label, value]) => (
                  <div key={label}>
                    <dt className="text-muted-foreground">{label}</dt>
                    <dd className="mt-1 tabular-nums">{value}</dd>
                  </div>
                ))}
              </dl>
              <div className="mt-4 space-y-1 text-xs text-muted-foreground">
                <p>
                  {t("modelTime")}: {timestamp(data.data.model_time)}
                </p>
                <p>
                  {t("fetchedAt")}: {timestamp(data.data.fetched_at)}
                </p>
                <p>
                  {t("modelInterval", { seconds: data.data.interval_seconds })}
                </p>
              </div>
            </>
          ) : (
            <p className="text-sm text-muted-foreground">
              {data.state === "disabled"
                ? t("weatherDisabledHint")
                : t("waitingForData")}
            </p>
          )}
        </>
      )}
    </section>
  );
}

export function CameraEnvironmentStatusCard({ camera }: { camera?: string }) {
  const { t } = useTranslation("views/environment");
  const { data: config } = useSWR<FrigateConfig>("config");
  const { data, error } = useSWR<CameraEnvironmentStatus>(
    camera ? `environment/cameras/${encodeURIComponent(camera)}` : null,
    { refreshInterval: 2000 },
  );
  const profileNames = (names: string[]) =>
    names.length
      ? names
          .map((name) => config?.profiles[name]?.friendly_name ?? name)
          .join(" + ")
      : t("baseSettings");
  return (
    <section
      className="rounded-xl border bg-background_alt p-4"
      aria-labelledby="environment-profile-heading"
    >
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h4 id="environment-profile-heading" className="font-medium">
          {t("profileTitle")}
        </h4>
        {data?.phase && (
          <Badge variant="secondary">{t(`profileStates.${data.phase}`)}</Badge>
        )}
      </div>
      {error || (data && !Array.isArray(data.applied_profiles)) ? (
        <StatusFailure />
      ) : !data ? (
        <ActivityIndicator className="size-5" />
      ) : (
        <>
          <dl className="grid gap-3 text-sm sm:grid-cols-2">
            <div>
              <dt className="text-muted-foreground">{t("appliedProfiles")}</dt>
              <dd>
                {data.phase === "initializing"
                  ? t("profileStates.initializing")
                  : profileNames(data.applied_profiles)}
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">{t("desiredProfiles")}</dt>
              <dd>{profileNames(data.desired.profiles)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">{t("reason")}</dt>
              <dd>{t(`reasons.${data.desired.reason}`)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">{t("glareSensor")}</dt>
              <dd>{t(`glareStates.${data.glare.state}`)}</dd>
            </div>
          </dl>
          {data.phase === "error" && (
            <p role="alert" className="mt-3 text-sm text-danger">
              {t("profileApplyFailed")}
            </p>
          )}
          {data.desired.weather_change_pending && (
            <p role="status" className="mt-3 text-sm text-selected">
              {t("weatherDwell")}
            </p>
          )}
          {data.glare.lighting_transition && (
            <p className="mt-3 text-sm text-muted-foreground">
              {t("lightingTransition")}
            </p>
          )}
          {data.blocked_fields.length > 0 && (
            <p role="status" className="mt-3 text-sm text-selected">
              {t("preservedServices", {
                services: data.blocked_fields
                  .map((name) => t(`services.${name.replaceAll(".", "_")}`))
                  .join(", "),
              })}
            </p>
          )}
          {data.desired.warnings.map((warning) => (
            <p
              key={warning}
              role="status"
              className="mt-2 text-sm text-muted-foreground"
            >
              {t(`warnings.${warning}`)}
            </p>
          ))}
        </>
      )}
      <Link
        to="/settings?page=profiles"
        className={cn(
          buttonVariants({ variant: "link" }),
          "mt-3 h-auto p-0 text-sm",
        )}
      >
        {t("editProfiles")}
      </Link>
    </section>
  );
}

export function WeatherSettingsView(props: SettingsPageProps) {
  const { t } = useTranslation("views/environment");
  return (
    <div className="flex w-full flex-col gap-5">
      <WeatherStatusCard />
      <p className="text-sm text-muted-foreground">{t("zipLocationHint")}</p>
      <SingleSectionPage
        {...props}
        sectionKey="weather"
        level="global"
        requiresRestart={false}
        showOverrideIndicator={false}
      />
    </div>
  );
}

export function CameraEnvironmentSettingsView(props: SettingsPageProps) {
  const { t } = useTranslation("views/environment");
  return (
    <div className="flex w-full flex-col gap-5">
      {props.selectedCamera && (
        <CameraEnvironmentStatusCard camera={props.selectedCamera} />
      )}
      <p className="text-sm text-muted-foreground">
        {t("physicalDevelopment")}
      </p>
      <SingleSectionPage
        {...props}
        sectionKey="environment"
        level="camera"
        profileState={undefined}
        requiresRestart={false}
        showOverrideIndicator={false}
      />
    </div>
  );
}
