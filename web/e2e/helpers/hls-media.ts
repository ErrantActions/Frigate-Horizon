import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Page } from "@playwright/test";

const fixtures = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../fixtures/media/live-hls",
);

export async function mockHlsMedia(
  page: Page,
  pattern: string | RegExp,
  { audio = true, broken = false } = {},
) {
  const requests: string[] = [];
  await page.route(pattern, (route) => {
    const url = new URL(route.request().url());
    requests.push(url.href);
    const name = url.pathname.split("/").at(-1);
    const folder = audio ? "0" : "video-only";
    if (name === "master.m3u8") {
      return route.fulfill({
        contentType: "application/vnd.apple.mpegurl",
        body: `#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=456000,RESOLUTION=1280x720,CODECS="avc1.42c01f${audio ? ",mp4a.40.2" : ""}"\nindex.m3u8\n`,
      });
    }
    if (name === "index.m3u8") {
      const playlist = readFileSync(
        resolve(fixtures, folder, audio ? "index.m3u8" : "vindex.m3u8"),
        "utf8",
      )
        .replace(audio ? "init_0.mp4" : "vinit.m4s", "init.mp4")
        .replace(
          /v?seg(\d{3})\.m4s/g,
          (_match, number: string) => `segment_${number.padStart(5, "0")}.m4s`,
        );
      return route.fulfill({
        contentType: "application/vnd.apple.mpegurl",
        body: broken ? "invalid playlist" : playlist,
      });
    }
    const segment = /^segment_(\d{5})\.m4s$/.exec(name ?? "");
    const file =
      name === "init.mp4"
        ? audio
          ? "init_0.mp4"
          : "vinit.m4s"
        : segment
          ? `${audio ? "" : "v"}seg${Number(segment[1]).toString().padStart(3, "0")}.m4s`
          : undefined;
    if (!file) return route.fulfill({ status: 404 });
    return route.fulfill({
      contentType: "video/mp4",
      body: readFileSync(resolve(fixtures, folder, file)),
    });
  });
  return requests;
}
