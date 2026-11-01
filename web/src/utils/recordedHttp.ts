import { baseUrl } from "@/api/baseUrl";
import type { RecordedHTTPSpan } from "@/types/recordedHttp";

export function recordedHttpEndpoint(path: string): string {
  const root = new URL("api/", baseUrl);
  const url = new URL(path, root);
  if (
    url.origin !== root.origin ||
    !url.pathname.startsWith(root.pathname) ||
    url.username ||
    url.password
  ) {
    throw new Error("Recording endpoint is outside the configured API");
  }
  return url.href;
}

export function recordingMediaTime(
  timeline: RecordedHTTPSpan[],
  timestamp: number,
): number | undefined {
  const span = timeline.find(
    (item) => item.source_start <= timestamp && timestamp < item.source_end,
  );
  if (!span || span.source_end <= span.source_start) return undefined;
  return (
    span.media_start +
    ((timestamp - span.source_start) / (span.source_end - span.source_start)) *
      (span.media_end - span.media_start)
  );
}

export function recordingCatalogTime(
  timeline: RecordedHTTPSpan[],
  mediaTime: number,
): number | undefined {
  const span = timeline.find(
    (item) => item.media_start <= mediaTime && mediaTime < item.media_end,
  );
  if (!span || span.media_end <= span.media_start) return undefined;
  return (
    span.source_start +
    ((mediaTime - span.media_start) / (span.media_end - span.media_start)) *
      (span.source_end - span.source_start)
  );
}
