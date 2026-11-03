import { useTheme } from "@/context/theme-provider";
import { useDateLocale } from "@/hooks/use-date-locale";
import { FrigateConfig } from "@/types/frigateConfig";
import { ApexAxisChartSeries } from "@/types/graph";
import { formatUnixTimestampToDateTime } from "@/utils/dateUtil";
import { useCallback, useEffect, useMemo, useRef } from "react";
import ApexCharts from "apexcharts";
import Chart from "react-apexcharts";
import { isMobileOnly } from "react-device-detect";
import { useTranslation } from "react-i18next";
import { MdCircle } from "react-icons/md";
import useSWR from "swr";
import { useTimeFormat } from "@/hooks/use-date-utils";
import { formatMetric, latestMetricPoint } from "@/utils/resourceUtil";

const GRAPH_COLORS = ["#5C7CFA", "#ED5CFA", "#FAD75C"];

type CameraLineGraphProps = {
  graphId: string;
  unit: string;
  dataLabels: string[];
  updateTimes: number[];
  data: ApexAxisChartSeries;
  isActive?: boolean;
};
export function CameraLineGraph({
  graphId,
  unit,
  dataLabels,
  updateTimes,
  data,
  isActive = true,
}: CameraLineGraphProps) {
  const { t } = useTranslation(["views/system", "common"]);
  const { data: config } = useSWR<FrigateConfig>("config", {
    revalidateOnFocus: false,
  });

  const lastValues = useMemo<(number | null)[] | undefined>(() => {
    if (!dataLabels || !data || data.length == 0) {
      return undefined;
    }

    return dataLabels.map((_, labelIdx) =>
      latestMetricPoint(data[labelIdx]?.data, updateTimes.length),
    );
  }, [data, dataLabels, updateTimes.length]);

  const { theme, systemTheme } = useTheme();

  const locale = useDateLocale();

  const timeFormat = useTimeFormat(config);
  const format = useMemo(() => {
    return t(`time.formattedTimestampHourMinute.${timeFormat}`, {
      ns: "common",
    });
  }, [t, timeFormat]);

  const updateTimesRef = useRef(updateTimes);
  useEffect(() => {
    updateTimesRef.current = updateTimes;
  }, [updateTimes]);

  const formatTime = useCallback(
    (val: unknown) => {
      const times = updateTimesRef.current;
      const ts = times[Math.round(val as number)];
      if (isNaN(ts)) {
        return "";
      }
      return formatUnixTimestampToDateTime(ts, {
        timezone: config?.ui.timezone,
        date_format: format,
        locale,
      });
    },
    [config?.ui.timezone, format, locale],
  );

  const options = useMemo(() => {
    return {
      chart: {
        id: graphId,
        selection: {
          enabled: false,
        },
        toolbar: {
          show: false,
        },
        zoom: {
          enabled: false,
        },
      },
      colors: GRAPH_COLORS,
      grid: {
        show: false,
      },
      legend: {
        show: false,
      },
      dataLabels: {
        enabled: false,
      },
      stroke: {
        width: 1,
      },
      tooltip: {
        theme: systemTheme || theme,
        y: { formatter: (val: number) => `${formatMetric(val)}${unit}` },
      },
      markers: {
        size: 0,
      },
      xaxis: {
        tickAmount: isMobileOnly ? 2 : 3,
        tickPlacement: "on",
        labels: {
          rotate: 0,
          formatter: formatTime,
          style: {
            colors: "#6B6B6B",
          },
        },
        axisBorder: {
          show: false,
        },
        axisTicks: {
          show: false,
        },
      },
      yaxis: {
        show: true,
        labels: {
          formatter: (val: number) => formatMetric(val),
          style: {
            colors: "#6B6B6B",
          },
        },
        min: 0,
      },
    } as ApexCharts.ApexOptions;
  }, [graphId, systemTheme, theme, formatTime, unit]);

  useEffect(() => {
    ApexCharts.exec(graphId, "updateOptions", options, true, true);
  }, [graphId, options]);

  const hasBeenActive = useRef(isActive);
  useEffect(() => {
    if (isActive && hasBeenActive.current === false) {
      ApexCharts.exec(graphId, "updateSeries", data, true);
    }
    hasBeenActive.current = isActive;
    // only replay animation on visibility change, not data updates
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isActive, graphId]);

  return (
    <div className="flex w-full flex-col">
      {lastValues && (
        <div className="flex flex-wrap items-center gap-2.5">
          {dataLabels.map((label, labelIdx) => (
            <div key={label} className="flex items-center gap-1">
              <MdCircle
                className="size-2"
                style={{ color: GRAPH_COLORS[labelIdx] }}
              />
              <div className="text-xs text-secondary-foreground">
                {t("cameras.label." + label)}
              </div>
              <div className="text-xs text-primary">
                {formatMetric(lastValues[labelIdx])}
                {lastValues[labelIdx] !== null && unit}
              </div>
            </div>
          ))}
        </div>
      )}
      <Chart type="line" options={options} series={data} height="120" />
    </div>
  );
}

type EventsPerSecondLineGraphProps = {
  graphId: string;
  unit: string;
  name: string;
  updateTimes: number[];
  data: ApexAxisChartSeries;
  isActive?: boolean;
};
export function EventsPerSecondsLineGraph({
  graphId,
  unit,
  name,
  updateTimes,
  data,
  isActive = true,
}: EventsPerSecondLineGraphProps) {
  const { data: config } = useSWR<FrigateConfig>("config", {
    revalidateOnFocus: false,
  });

  const { theme, systemTheme } = useTheme();

  const lastValue = useMemo(
    () => latestMetricPoint(data[0]?.data, updateTimes.length),
    [data, updateTimes.length],
  );

  const locale = useDateLocale();
  const { t } = useTranslation(["common"]);

  const timeFormat = useTimeFormat(config);
  const format = useMemo(() => {
    return t(`time.formattedTimestampHourMinute.${timeFormat}`, {
      ns: "common",
    });
  }, [t, timeFormat]);

  const updateTimesRef = useRef(updateTimes);
  useEffect(() => {
    updateTimesRef.current = updateTimes;
  }, [updateTimes]);

  const formatTime = useCallback(
    (val: unknown) => {
      const times = updateTimesRef.current;
      const ts = times[Math.round(val as number) - 1];
      if (isNaN(ts)) {
        return "";
      }
      return formatUnixTimestampToDateTime(ts, {
        timezone: config?.ui.timezone,
        date_format: format,
        locale,
      });
    },
    [config?.ui.timezone, format, locale],
  );

  const options = useMemo(() => {
    return {
      chart: {
        id: graphId,
        selection: {
          enabled: false,
        },
        toolbar: {
          show: false,
        },
        zoom: {
          enabled: false,
        },
      },
      colors: GRAPH_COLORS,
      grid: {
        show: false,
      },
      legend: {
        show: false,
      },
      dataLabels: {
        enabled: false,
      },
      stroke: {
        width: 1,
      },
      tooltip: {
        theme: systemTheme || theme,
      },
      markers: {
        size: 0,
      },
      xaxis: {
        tickAmount: isMobileOnly ? 2 : 3,
        tickPlacement: "on",
        labels: {
          rotate: 0,
          formatter: formatTime,
          style: {
            colors: "#6B6B6B",
          },
        },
        axisBorder: {
          show: false,
        },
        axisTicks: {
          show: false,
        },
      },
      yaxis: {
        show: true,
        labels: {
          formatter: (val: number) => formatMetric(val),
          style: {
            colors: "#6B6B6B",
          },
        },
        min: 0,
      },
    } as ApexCharts.ApexOptions;
  }, [graphId, systemTheme, theme, formatTime]);

  useEffect(() => {
    ApexCharts.exec(graphId, "updateOptions", options, true, true);
  }, [graphId, options]);

  const hasBeenActive = useRef(isActive);
  useEffect(() => {
    if (isActive && hasBeenActive.current === false) {
      ApexCharts.exec(graphId, "updateSeries", data, true);
    }
    hasBeenActive.current = isActive;
    // only replay animation on visibility change, not data updates
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isActive, graphId]);

  return (
    <div className="flex w-full flex-col">
      <div className="flex items-center gap-1">
        <div className="text-xs text-secondary-foreground">{name}</div>
        <div className="text-xs text-primary">
          {formatMetric(lastValue)}
          {lastValue !== null && unit}
        </div>
      </div>
      <Chart type="line" options={options} series={data} height="120" />
    </div>
  );
}
