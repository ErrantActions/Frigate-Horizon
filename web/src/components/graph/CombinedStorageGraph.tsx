import { useTheme } from "@/context/theme-provider";
import { generateColors } from "@/utils/colorUtil";
import { useCallback, useEffect, useMemo } from "react";
import ApexCharts from "apexcharts";
import Chart from "react-apexcharts";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { getUnitSize } from "@/utils/storageUtil";
import { CameraStorage, StreamStorage } from "@/types/stats";
import { RecordingStorageTiers, RecordingStorageUsage } from "@/types/storage";

import { CiCircleAlert } from "react-icons/ci";
import { useTranslation } from "react-i18next";

type TotalStorage = {
  used: number;
  camera: number;
  total: number;
};

type StorageRow = {
  id: string;
  kind: "camera" | "other" | "unused";
  name: string;
  usage: number;
  bandwidth?: number;
  color: string;
  tiers?: Record<string, RecordingStorageUsage>;
  streams?: {
    main?: StreamStorage;
    sub?: StreamStorage;
  };
};

type CombinedStorageGraphProps = {
  graphId: string;
  cameraStorage: CameraStorage;
  totalStorage: TotalStorage;
  tierStorage?: RecordingStorageTiers;
};
export function CombinedStorageGraph({
  graphId,
  cameraStorage,
  totalStorage,
  tierStorage,
}: CombinedStorageGraphProps) {
  const { t } = useTranslation([
    "views/system",
    "components/player",
    "views/storage",
  ]);

  const { theme, systemTheme } = useTheme();
  const isDark = (systemTheme || theme) == "dark";

  const rows: StorageRow[] = useMemo(() => {
    const entities = Object.keys(tierStorage?.cameras ?? cameraStorage);
    const colors = generateColors(entities.length);

    return [
      ...entities.map((entity, index): StorageRow => ({
        id: entity,
        kind: "camera",
        name:
          tierStorage?.cameras?.[entity].friendly_name ??
          cameraStorage[entity]?.friendly_name ??
          entity.replaceAll("_", " "),
        usage: tierStorage?.cameras
          ? (tierStorage.cameras[entity].tiers.local?.usage_mib ?? 0)
          : (cameraStorage[entity]?.usage ?? 0),
        bandwidth: cameraStorage[entity]?.bandwidth,
        color: colors[index],
        streams: cameraStorage[entity]?.streams,
        tiers: tierStorage?.cameras?.[entity].tiers,
      })),
      {
        id: "other",
        kind: "other",
        name: t("label.other", { ns: "common" }),
        usage: Math.max(0, totalStorage.used - totalStorage.camera),
        bandwidth: 0,
        color: isDark ? "#606060" : "#D5D5D5",
      },
      {
        id: "unused",
        kind: "unused",
        name: t("storage.cameraStorage.unused.title"),
        usage: Math.max(0, totalStorage.total - totalStorage.used),
        bandwidth: 0,
        color: isDark ? "#404040" : "#E5E5E5",
      },
    ];
  }, [cameraStorage, tierStorage, totalStorage, isDark, t]);

  const tiers = useMemo(
    () =>
      Array.from(
        new Set([
          "local",
          "archive",
          ...rows.flatMap((row) => Object.keys(row.tiers ?? {})),
        ]),
      ),
    [rows],
  );
  const tierTotals = useMemo(
    () =>
      Object.fromEntries(
        tiers.map((tier) => [
          tier,
          rows.reduce(
            (total, row) =>
              total +
              (row.kind === "camera"
                ? tier === "local"
                  ? row.usage
                  : (row.tiers?.[tier]?.usage_mib ?? 0)
                : 0),
            0,
          ),
        ]),
      ),
    [rows, tiers],
  );
  const tierName = (tier: string) =>
    tierStorage?.tier_labels?.[tier] ??
    (tier === "local"
      ? t("cameras.local", { ns: "views/storage" })
      : tier === "archive"
        ? t("cameras.archive", { ns: "views/storage" })
        : tier);

  const series = useMemo(
    () =>
      rows.map((row) => ({
        name: row.kind === "camera" ? `${row.name} (${row.id})` : row.name,
        data: [
          totalStorage.total > 0 ? (row.usage / totalStorage.total) * 100 : 0,
        ],
        usage: row.usage,
        color: row.color,
      })),
    [rows, totalStorage.total],
  );

  const hasSubStorage = useMemo(
    () => rows.some((row) => row.streams?.sub),
    [rows],
  );

  const options = useMemo(() => {
    return {
      chart: {
        id: graphId,
        background: isDark ? "#404040" : "#E5E5E5",
        selection: {
          enabled: false,
        },
        toolbar: {
          show: false,
        },
        zoom: {
          enabled: false,
        },
        stacked: true,
        stackType: "100%",
      },
      grid: {
        show: false,
        padding: {
          bottom: -45,
          top: -40,
          left: -20,
          right: -20,
        },
      },
      legend: {
        show: false,
      },
      dataLabels: {
        enabled: false,
      },
      plotOptions: {
        bar: {
          horizontal: true,
        },
      },
      states: {
        active: {
          filter: {
            type: "none",
          },
        },
        hover: {
          filter: {
            type: "none",
          },
        },
      },
      tooltip: {
        enabled: false,
        x: {
          show: false,
        },
        y: {
          formatter: function (val, opts) {
            const entry = opts ? series[opts.seriesIndex] : undefined;
            if (entry) {
              return `${getUnitSize(entry.usage)} (${(val ?? 0).toFixed(2)}%)`;
            }
          },
        },
        theme: systemTheme || theme,
      },
      xaxis: {
        axisBorder: {
          show: false,
        },
        axisTicks: {
          show: false,
        },
        labels: {
          formatter: function (val) {
            return val + "%";
          },
        },
        min: 0,
        max: 100,
      },
      yaxis: {
        show: false,
        min: 0,
        max: 100,
      },
    } as ApexCharts.ApexOptions;
  }, [graphId, isDark, systemTheme, theme, series]);

  useEffect(() => {
    ApexCharts.exec(graphId, "updateOptions", options, true, true);
  }, [graphId, options]);

  // convenience

  const getStreamSplit = useCallback(
    (row: StorageRow, field: keyof StreamStorage) => {
      const mainValue = row.streams?.main?.[field];
      const subValue = row.streams?.sub?.[field];

      // omit the split entirely when either side is unknown, rather than
      // showing a zero that would read as "this stream costs nothing"
      if (mainValue == null || subValue == null) {
        return null;
      }

      return (
        <div className="text-xs text-primary-variant">
          <div>
            <span className="text-muted-foreground">
              {t("quality.main", { ns: "components/player" })}
            </span>{" "}
            {getUnitSize(mainValue)}
          </div>
          <div>
            <span className="text-muted-foreground">
              {t("quality.sub", { ns: "components/player" })}
            </span>{" "}
            {getUnitSize(subValue)}
          </div>
        </div>
      );
    },
    [t],
  );

  return (
    <div className="flex w-full flex-col gap-2.5">
      <div className="flex w-full items-center justify-between gap-1">
        <div className="flex items-center gap-1">
          <div className="text-xs text-primary">
            {getUnitSize(totalStorage.camera)}
          </div>
          <div className="text-xs text-primary">/</div>
          <div className="text-xs text-muted-foreground">
            {getUnitSize(totalStorage.total)}
          </div>
        </div>
      </div>
      <div className="h-5 overflow-hidden rounded-md">
        <Chart type="bar" options={options} series={series} height="100%" />
      </div>
      <div className="custom-legend">
        {!tierStorage?.cameras && (
          <p role="status" className="mb-2 text-sm text-muted-foreground">
            {t("cameras.unavailable", { ns: "views/storage" })}
          </p>
        )}
        <Table data-testid="camera-storage-table">
          <TableCaption>
            {t("cameras.caption", { ns: "views/storage" })}
          </TableCaption>
          <TableHeader>
            <TableRow>
              <TableHead>{t("storage.cameraStorage.camera")}</TableHead>
              <TableHead>
                <div className="flex flex-row items-center gap-1">
                  {tierName("local")}
                  {hasSubStorage && (
                    <Popover>
                      <PopoverTrigger asChild>
                        <button
                          className="focus:outline-none"
                          aria-label={t(
                            "storage.cameraStorage.subStream.information",
                          )}
                        >
                          <CiCircleAlert
                            className="size-5"
                            aria-label={t(
                              "storage.cameraStorage.subStream.information",
                            )}
                          />
                        </button>
                      </PopoverTrigger>
                      <PopoverContent className="w-80">
                        <div className="space-y-2">
                          {t("storage.cameraStorage.subStream.tips")}
                        </div>
                      </PopoverContent>
                    </Popover>
                  )}
                </div>
              </TableHead>
              {tiers
                .filter((tier) => tier !== "local")
                .map((tier) => (
                  <TableHead key={tier}>{tierName(tier)}</TableHead>
                ))}
              <TableHead>
                {t("cameras.localFilesystemShare", { ns: "views/storage" })}
              </TableHead>
              <TableHead>{t("storage.cameraStorage.bandwidth")}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row) => {
              const isAggregate = row.kind !== "camera";

              return (
                <TableRow
                  key={`${row.kind}:${row.id}`}
                  data-testid={
                    isAggregate ? undefined : `camera-storage-${row.id}`
                  }
                >
                  <TableCell className="flex flex-row items-center gap-2 font-medium smart-capitalize">
                    <div
                      className="size-3 rounded-md"
                      style={{ backgroundColor: row.color }}
                    ></div>
                    <div>
                      <div>{row.name}</div>
                      {!isAggregate && row.name !== row.id && (
                        <div className="text-xs text-muted-foreground">
                          {row.id}
                        </div>
                      )}
                    </div>
                    {isAggregate && (
                      <Popover>
                        <PopoverTrigger asChild>
                          <button
                            className="focus:outline-none"
                            aria-label={t(
                              "storage.cameraStorage.unusedStorageInformation",
                            )}
                          >
                            <CiCircleAlert
                              className="size-5"
                              aria-label={t(
                                "storage.cameraStorage.unusedStorageInformation",
                              )}
                            />
                          </button>
                        </PopoverTrigger>
                        <PopoverContent className="w-80">
                          <div className="space-y-2">
                            {t("storage.cameraStorage.unused.tips")}
                          </div>
                        </PopoverContent>
                      </Popover>
                    )}
                  </TableCell>
                  {tiers.map((tier) => {
                    const usage = row.tiers?.[tier];
                    const value =
                      tier === "local"
                        ? row.usage
                        : row.tiers
                          ? (usage?.usage_mib ?? 0)
                          : undefined;
                    const applicable = tier === "local" || !isAggregate;
                    const percent =
                      value !== undefined && tierTotals[tier] > 0
                        ? (value / tierTotals[tier]) * 100
                        : 0;
                    return (
                      <TableCell key={tier} className="min-w-36">
                        <div className="tabular-nums">
                          {applicable && value !== undefined
                            ? getUnitSize(value)
                            : "-"}
                        </div>
                        {!isAggregate && value !== undefined && (
                          <div
                            role="meter"
                            aria-label={t("cameras.share", {
                              ns: "views/storage",
                              camera: row.name,
                              tier: tierName(tier),
                            })}
                            aria-valuemin={0}
                            aria-valuemax={100}
                            aria-valuenow={Math.round(percent)}
                            className="my-2 h-1.5 overflow-hidden rounded-full bg-secondary"
                          >
                            <div
                              className="h-full rounded-full"
                              style={{
                                width: `${Math.min(100, Math.max(0, percent))}%`,
                                backgroundColor: row.color,
                              }}
                            />
                          </div>
                        )}
                        {(usage?.unknown_size_recordings ?? 0) > 0 && (
                          <p className="max-w-52 text-xs text-muted-foreground">
                            {t("cameras.incomplete", {
                              ns: "views/storage",
                              count: usage?.unknown_size_recordings,
                            })}
                          </p>
                        )}
                        {tier === "local" && getStreamSplit(row, "usage")}
                      </TableCell>
                    );
                  })}
                  <TableCell>
                    {(totalStorage.total > 0
                      ? (row.usage / totalStorage.total) * 100
                      : 0
                    ).toFixed(2)}
                    %
                  </TableCell>
                  <TableCell>
                    {isAggregate || row.bandwidth === undefined
                      ? "-"
                      : t("cameras.bandwidth", {
                          ns: "views/storage",
                          size: getUnitSize(row.bandwidth),
                        })}
                    {getStreamSplit(row, "bandwidth")}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
