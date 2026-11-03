import { useTheme } from "@/context/theme-provider";
import { useEffect, useMemo } from "react";
import ApexCharts from "apexcharts";
import Chart from "react-apexcharts";
import { getUnitSize } from "@/utils/storageUtil";
import { formatMetric, usagePercent } from "@/utils/resourceUtil";

type StorageGraphProps = {
  graphId: string;
  used: number;
  total: number;
};
export function StorageGraph({ graphId, used, total }: StorageGraphProps) {
  const { theme, systemTheme } = useTheme();
  const percent = usagePercent(used, total);

  const options = useMemo(() => {
    return {
      chart: {
        id: graphId,
        background: (systemTheme || theme) == "dark" ? "#404040" : "#E5E5E5",
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
      grid: {
        show: false,
        padding: {
          bottom: -40,
          top: -60,
          left: -20,
          right: 0,
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
      },
      xaxis: {
        min: 0,
        max: 100,
        axisBorder: {
          show: false,
        },
        axisTicks: {
          show: false,
        },
        labels: {
          show: false,
        },
      },
      yaxis: {
        show: false,
        min: 0,
        max: 100,
      },
    } as ApexCharts.ApexOptions;
  }, [graphId, systemTheme, theme]);

  useEffect(() => {
    ApexCharts.exec(graphId, "updateOptions", options, true, true);
  }, [graphId, options]);

  return (
    <div className="flex w-full flex-col gap-2.5">
      <div className="flex w-full items-center justify-between gap-1">
        <div className="flex items-center gap-1">
          <div className="text-xs text-primary">{getUnitSize(used)}</div>
          <div className="text-xs text-primary">/</div>
          <div className="text-xs text-muted-foreground">
            {getUnitSize(total)}
          </div>
        </div>
        <div className="text-xs text-primary">
          {formatMetric(percent)}
          {percent !== null && "%"}
        </div>
      </div>
      <div className="h-5 overflow-hidden rounded-md">
        <Chart
          type="bar"
          options={options}
          series={[{ data: [{ x: "storage", y: percent }] }]}
          height="100%"
        />
      </div>
    </div>
  );
}
