import type { ResourceLimits } from "@/types/stats";

export function metricNumber(value: unknown): number | null {
  if (typeof value !== "number" && typeof value !== "string") return null;
  const text =
    typeof value === "string" ? value.trim().replace(/%$/, "") : value;
  if (text === "") return null;
  const numeric = Number(text);
  return Number.isFinite(numeric) ? numeric : null;
}

export function cpuCapacityPercent(
  raw: unknown,
  resources: ResourceLimits | undefined,
): number | null {
  const value = metricNumber(raw);
  const capacity = resources?.cpu_capacity;
  return value !== null &&
    value >= 0 &&
    capacity != null &&
    Number.isFinite(capacity) &&
    capacity > 0
    ? value / capacity
    : null;
}

export function cpuCoreEquivalents(raw: unknown): number | null {
  const value = metricNumber(raw);
  return value !== null && value >= 0 ? value / 100 : null;
}

export function videoRateMbps(kilobytesPerSecond: unknown): number | null {
  const value = metricNumber(kilobytesPerSecond);
  return value !== null && value >= 0 ? value * 0.008 : null;
}

export function metricPoint(point: unknown): number | null {
  return typeof point === "object" && point !== null && "y" in point
    ? metricNumber(point.y)
    : metricNumber(point);
}

export function latestMetricPoint(
  points: readonly unknown[] | undefined,
  expectedIndex: number,
): number | null {
  const point = points?.at(-1);
  if (
    typeof point === "object" &&
    point !== null &&
    "x" in point &&
    point.x !== expectedIndex
  )
    return null;
  return metricPoint(point);
}

export function formatMetric(value: unknown, digits = 2): string {
  const numeric = metricNumber(value);
  if (numeric === null) return "-";
  const minimum = 10 ** -digits;
  if (numeric > 0 && numeric < minimum) return `<${minimum}`;
  return numeric.toLocaleString(undefined, { maximumFractionDigits: digits });
}

export function usagePercent(used: unknown, total: unknown): number | null {
  const numerator = metricNumber(used);
  const denominator = metricNumber(total);
  return numerator !== null &&
    numerator >= 0 &&
    denominator !== null &&
    denominator > 0
    ? (numerator / denominator) * 100
    : null;
}
