import { test, expect, FrigateApp } from "../fixtures/frigate-test";
import { statsFactory } from "../fixtures/mock-data/stats";
import type { FrigateStats, ResourceLimits } from "../../src/types/stats";
import {
  cpuCapacityPercent,
  cpuCoreEquivalents,
  formatMetric,
  metricNumber,
  latestMetricPoint,
  usagePercent,
  videoRateMbps,
} from "../../src/utils/resourceUtil";
import { getUnitSizeFromBytes } from "../../src/utils/storageUtil";

function limits(capacity: number | null): ResourceLimits {
  return {
    host_logical_cpus: 64,
    cpu_affinity: 64,
    cpu_quota: capacity,
    cpu_capacity: capacity,
    cpu_scope:
      capacity === null ? "unknown" : capacity < 64 ? "container" : "host",
    host_memory_bytes: 64 * 2 ** 30,
    memory_limit_bytes: null,
    memory_capacity_bytes: 64 * 2 ** 30,
    memory_scope: "host",
    errors: [],
  };
}

async function installUsage(
  app: FrigateApp,
  capacity: number | null,
  cpu = "53",
) {
  const overrides = {
    resources: limits(capacity),
    cpu_usages: { "frigate.full_system": { cpu: "0.0" } },
    processes: {
      go2rtc: { pid: 90, cpu, mem: "0.1089", mem_bytes: 73084 * 1024 },
    },
    gpu_usages: {
      "Test GPU": {
        vendor: "nvidia",
        gpu: "0%",
        enc: "0%",
        dec: "75%",
        mem: "20%",
      },
    },
  };
  await app.installDefaults({ stats: overrides });
  const stats: FrigateStats = statsFactory(overrides);
  await app.page.route("**/api/stats/history**", (route) =>
    route.fulfill({
      json: Array.from({ length: 30 }, (_, index) => ({
        ...stats,
        service: {
          ...stats.service,
          last_updated: Math.floor(Date.now() / 1000) - (29 - index) * 5,
        },
      })),
    }),
  );
}

test.describe("Resource units and denominators @medium @mobile", () => {
  test("normalizes one-core CPU against actual available capacity", () => {
    expect(cpuCapacityPercent("53", limits(64))).toBeCloseTo(0.828125);
    expect(cpuCoreEquivalents("53")).toBe(0.53);
    expect(cpuCapacityPercent("53", limits(2))).toBe(26.5);
    expect(cpuCapacityPercent("53", limits(0.5))).toBe(106);
  });

  test("distinguishes zero, unknown, and malformed values", () => {
    expect(cpuCapacityPercent("0", limits(64))).toBe(0);
    for (const value of [undefined, null, "", "N/A", "NaN", Infinity]) {
      expect(metricNumber(value)).toBeNull();
      expect(cpuCapacityPercent(value, limits(64))).toBeNull();
    }
    expect(cpuCapacityPercent("53", limits(null))).toBeNull();
    expect(cpuCapacityPercent("53", undefined)).toBeNull();
    expect(formatMetric(0)).toBe("0");
    expect(formatMetric(null)).toBe("-");
    expect(formatMetric(0.0001)).toBe("<0.01");
    expect(latestMetricPoint([{ x: 1, y: 53 }], 2)).toBeNull();
    expect(latestMetricPoint([{ x: 2, y: 0 }], 2)).toBe(0);
  });

  test("keeps memory binary and missing storage capacity unknown", () => {
    expect(getUnitSizeFromBytes(73084 * 1024)).toBe("71.37 MiB");
    expect(getUnitSizeFromBytes(Infinity)).toBe("-");
    expect(usagePercent(0, 100)).toBe(0);
    expect(usagePercent(50, 100)).toBe(50);
    expect(usagePercent(50, 0)).toBeNull();
    expect(usagePercent(undefined, 100)).toBeNull();
    expect(videoRateMbps(250)).toBe(2);
    expect(videoRateMbps(0)).toBe(0);
    expect(videoRateMbps(undefined)).toBeNull();
  });

  test("displays go2rtc as capacity share with raw core and resident-memory detail", async ({
    frigateApp,
  }) => {
    await installUsage(frigateApp, 64);
    await frigateApp.goto("/system#general");
    if (!frigateApp.isMobile) {
      await expect(
        frigateApp.page.getByRole("link", { name: "Host CPU 0%", exact: true }),
      ).toBeVisible();
    }
    await expect(
      frigateApp.page.getByText("0.83%", { exact: true }),
    ).toBeAttached();
    await expect(
      frigateApp.page.getByText("0.53 logical CPU equivalents", {
        exact: true,
      }),
    ).toBeAttached();
    await expect(
      frigateApp.page.getByText("71.37 MiB resident RAM (RSS)", {
        exact: true,
      }),
    ).toBeAttached();
    await expect(
      frigateApp.page.getByText(/Process CPU is a share of 64/),
    ).toBeAttached();
    await expect(
      frigateApp.page.getByText(/GPU percentages are device-wide/),
    ).toBeAttached();
    await expect(
      frigateApp.page.getByText("75%", { exact: true }),
    ).toBeAttached();
  });

  test("uses a container quota without dividing GPU engine usage", async ({
    frigateApp,
  }) => {
    await installUsage(frigateApp, 2);
    await frigateApp.goto("/system#general");
    await expect(
      frigateApp.page.getByText("26.5%", { exact: true }),
    ).toBeAttached();
    await expect(
      frigateApp.page.getByText(/Process CPU is a share of 2 available/),
    ).toBeAttached();
    await expect(
      frigateApp.page.getByText("75%", { exact: true }),
    ).toBeAttached();
  });

  test("unknown CPU capacity is explicit instead of showing a fake zero", async ({
    frigateApp,
  }) => {
    await installUsage(frigateApp, null);
    await frigateApp.goto("/system#general");
    await expect(
      frigateApp.page.getByText(/CPU capacity is unavailable/),
    ).toBeAttached();
    await expect(
      frigateApp.page.getByText("0.53 logical CPU equivalents", {
        exact: true,
      }),
    ).toBeAttached();
    await expect(
      frigateApp.page.getByText("0.83%", { exact: true }),
    ).toHaveCount(0);
  });

  test("a half-used shared-memory filesystem draws a half-width usage bar", async ({
    frigateApp,
  }) => {
    await frigateApp.installDefaults({
      stats: {
        service: {
          storage: { "/dev/shm": { used: 64, free: 64, total: 128 } },
        },
      },
    });
    await frigateApp.goto("/system#storage");
    const graph = frigateApp.page.locator("#apexchartsgeneral-shared-memory");
    await graph.scrollIntoViewIfNeeded();
    await expect
      .poll(() =>
        graph.evaluate((element) => {
          const bar = element.querySelector(".apexcharts-bar-area");
          const clip = element.querySelector("clipPath[id^=gridRectMask] rect");
          if (
            !(bar instanceof SVGGraphicsElement) ||
            !(clip instanceof SVGRectElement)
          )
            return null;
          return bar.getBBox().width / clip.width.baseVal.value;
        }),
      )
      .toBeCloseTo(0.5, 1);
    await expect(
      graph.locator("../../..").getByText("50%", { exact: true }),
    ).toBeAttached();
  });

  test("camera CPU uses the same denominator and preserves unavailable samples", async ({
    frigateApp,
  }) => {
    await frigateApp.installDefaults({ stats: { resources: limits(64) } });
    const base = statsFactory();
    const snapshot: FrigateStats = {
      ...base,
      resources: limits(64),
      cameras: {
        ...base.cameras,
        front_door: {
          ...base.cameras.front_door,
          ffmpeg_cpu: "53",
          capture_cpu: "0",
        },
      },
    };
    await frigateApp.page.route("**/api/stats/history**", (route) =>
      route.fulfill({
        json: Array.from({ length: 30 }, () => snapshot),
      }),
    );
    await frigateApp.goto("/system#cameras");
    await expect(
      frigateApp.page.getByText("0.83%", { exact: true }),
    ).toBeAttached();
    await expect(
      frigateApp.page.getByText(
        /Logical CPU equivalents: FFmpeg 0.53, capture 0, detection -/,
      ),
    ).toBeAttached();
    await expect(frigateApp.page.getByText("53%", { exact: true })).toHaveCount(
      0,
    );
  });
});
