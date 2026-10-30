import { createServer, type Server } from "node:http";
import { readFile } from "node:fs/promises";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { test, expect, type FrigateApp } from "../fixtures/frigate-test";

const media = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../fixtures/media/live-hls",
);
let server: Server;
let origin: string;

test.beforeAll(async () => {
  server = createServer(async (request, response) => {
    const url = new URL(request.url ?? "/", "http://localhost");
    const match =
      /^\/(720p|1080p)\/(v?index\.m3u8|init_[01]\.mp4|vinit\.mp4|v?seg\d{3}\.mp4)$/.exec(
        url.pathname,
      );
    if (url.pathname !== "/master.m3u8" && !match) {
      response.writeHead(404).end();
      return;
    }
    let data: Buffer;
    let playlist = true;
    try {
      if (url.pathname === "/master.m3u8") {
        if (url.searchParams.get("audio") === "false") {
          data = Buffer.from(
            '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=456000,RESOLUTION=1280x720,CODECS="avc1.42c01f"\n720p/vindex.m3u8\n',
          );
        } else {
          let master = (
            await readFile(resolve(media, "master.m3u8"))
          ).toString();
          const height = Number(url.searchParams.get("height"));
          const maximum = Number(url.searchParams.get("max_height"));
          master = master.replace(
            /#EXT-X-STREAM-INF:[^\n]+\n[01]\/index\.m3u8/g,
            (variant) => {
              const pixels = Number(/RESOLUTION=\d+x(\d+)/.exec(variant)?.[1]);
              return (height && pixels !== height) ||
                (maximum && pixels > maximum)
                ? ""
                : variant;
            },
          );
          data = Buffer.from(master);
        }
      } else if (match) {
        const file = match[2].startsWith("v")
          ? `video-only/${match[2].replace(/\.mp4$/, ".m4s")}`
          : `${match[1] === "720p" ? "0" : "1"}/${match[2].replace(/^(seg\d{3})\.mp4$/, "$1.m4s")}`;
        playlist = file.endsWith(".m3u8");
        data = await readFile(resolve(media, file));
      } else {
        response.writeHead(404).end();
        return;
      }
    } catch (error) {
      response.writeHead(500).end(String(error));
      return;
    }
    if (playlist) {
      data = Buffer.from(
        data
          .toString()
          .replace("0/index.m3u8", "720p/index.m3u8")
          .replace("1/index.m3u8", "1080p/index.m3u8")
          .replaceAll(".m4s", ".mp4"),
      );
    }
    response.writeHead(200, {
      "Content-Type": playlist ? "application/vnd.apple.mpegurl" : "video/mp4",
      "Content-Length": data.length,
      "Cache-Control": "no-store",
      "Access-Control-Allow-Origin":
        request.headers.origin ?? "http://localhost:4173",
      "Access-Control-Allow-Credentials": "true",
    });
    response.end(data);
  });
  await new Promise<void>((ready) => server.listen(0, "127.0.0.1", ready));
  const address = server.address();
  if (!address || typeof address === "string")
    throw new Error("HLS fixture server did not start");
  origin = `http://127.0.0.1:${address.port}`;
});

test.afterAll(async () => {
  await new Promise<void>((done, reject) =>
    server.close((error) => (error ? reject(error) : done())),
  );
});

async function installLiveConfig(app: FrigateApp) {
  await app.installDefaults({
    config: {
      ui: { live_hls: true, live_compatibility: true },
      go2rtc: {
        streams: {
          front_door: ["rtsp://camera.invalid/main"],
          front_door_transcode_720p: [
            "ffmpeg:front_door#video=h264#height=720",
          ],
          front_door_transcode_1080p: [
            "ffmpeg:front_door#video=h264#height=1080",
          ],
        },
      },
      cameras: {
        front_door: {
          capture_mode: "recording_only",
          live: {
            streams: {
              Main: "front_door",
              "720p": "front_door_transcode_720p",
              "1080p": "front_door_transcode_1080p",
            },
            transcode: {
              enabled: false,
              source: "front_door",
              qualities: [
                { height: 720, bitrate: 200 },
                { height: 1080, bitrate: 650 },
              ],
            },
          },
        },
        backyard: { enabled: false },
        garage: { enabled: false },
      },
    },
  });
}

async function routeLive(
  app: FrigateApp,
  options: { gate?: Promise<void>; available?: () => boolean } = {},
) {
  const requests: URL[] = [];
  await app.page.route("**/api/front_door/live/hls/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.split("/live/hls/")[1];
    requests.push(url);
    if (path === "status") {
      await route.fulfill({
        json: {
          version: 1,
          protocol: "ll-hls",
          enabled: true,
          available: options.available?.() ?? true,
          playlist: "front_door/live/hls/master.m3u8",
          renditions: [
            { height: 720, bitrate: 200 },
            { height: 1080, bitrate: 650 },
          ],
        },
      });
      return;
    }
    if (path === "master.m3u8") await options.gate;
    await route.continue({ url: `${origin}/${path}${url.search}` });
  });
  return requests;
}

test.describe("HLS-only HTTPS live playback @mobile", () => {
  test.beforeEach(async ({ frigateApp }) => installLiveConfig(frigateApp));

  test("starts low, upgrades on real throughput and downgrades when bandwidth falls", async ({
    frigateApp,
  }) => {
    test.setTimeout(120000);
    const { page } = frigateApp;
    let releaseMaster: () => void = () => {};
    const gate = new Promise<void>((ready) => {
      releaseMaster = ready;
    });
    const requests = await routeLive(frigateApp, { gate });
    await frigateApp.goto("/#front_door");
    const cdp = await page.context().newCDPSession(page);
    try {
      await cdp.send("Network.enable");
      const bandwidth = (bytes: number) =>
        cdp.send("Network.emulateNetworkConditions", {
          offline: false,
          latency: 20,
          downloadThroughput: bytes,
          uploadThroughput: 1000000,
        });
      await bandwidth(65000);
      releaseMaster();
      const card = page.locator('[data-camera="front_door"]').first();
      const video = card.locator('[data-live-transport="hls"]');
      await expect(video).toBeVisible({ timeout: 30000 });
      const width = () =>
        video.evaluate((element: HTMLVideoElement) => element.videoWidth);
      await expect.poll(width, { timeout: 30000 }).toBe(1280);
      expect(
        requests.find((url) => /\/(?:720|1080)p\//.test(url.pathname))
          ?.pathname,
      ).toContain("/720p/");
      await bandwidth(500000);
      await expect(card).toHaveAttribute(
        "data-live-stream",
        "front_door_transcode_1080p",
        { timeout: 30000 },
      );
      await expect.poll(width).toBe(1920);
      await bandwidth(65000);
      await expect(card).toHaveAttribute(
        "data-live-stream",
        "front_door_transcode_720p",
        { timeout: 30000 },
      );
      await expect.poll(width).toBe(1280);
      await expect(card.getByTestId("live-warning-badge")).toHaveCount(0);
    } finally {
      await cdp.detach();
    }
  });

  test("reports unavailable HLS and retries HLS without opening a legacy transport", async ({
    frigateApp,
  }) => {
    let available = false;
    const legacy: string[] = [];
    frigateApp.page.on("websocket", (socket) => {
      if (/\/live\/|webrtc/.test(socket.url())) legacy.push(socket.url());
    });
    await routeLive(frigateApp, { available: () => available });
    await frigateApp.goto("/#front_door");
    const card = frigateApp.page.locator('[data-camera="front_door"]').first();
    await expect(card.getByRole("alert")).toBeVisible();
    available = true;
    await card
      .getByRole("button", { name: "Retry playback", exact: true })
      .click();
    await expect
      .poll(
        () =>
          card
            .locator("video")
            .evaluate((video: HTMLVideoElement) => video.videoWidth),
        { timeout: 15000 },
      )
      .toBeGreaterThan(0);
    await expect(card.getByRole("alert")).toHaveCount(0);
    expect(legacy).toEqual([]);
  });

  test("Data Saver pins the lowest HLS rung", async ({ frigateApp }) => {
    await frigateApp.page.addInitScript(() => {
      Object.defineProperty(navigator, "connection", {
        value: { saveData: true },
      });
    });
    const requests = await routeLive(frigateApp);
    await frigateApp.goto("/#front_door");
    const video = frigateApp.page
      .locator('[data-camera="front_door"] video')
      .first();
    await expect
      .poll(
        () => video.evaluate((element: HTMLVideoElement) => element.videoWidth),
        { timeout: 15000 },
      )
      .toBe(1280);
    expect(
      requests
        .find((url) => url.pathname.endsWith("/master.m3u8"))
        ?.searchParams.get("height"),
    ).toBe("720");
    expect(requests.some((url) => url.pathname.includes("/1080p/"))).toBe(
      false,
    );
  });

  test("decodes real video-only CMAF when AAC MediaSource support is absent", async ({
    frigateApp,
  }) => {
    await frigateApp.page.addInitScript(() => {
      const supported = MediaSource.isTypeSupported.bind(MediaSource);
      MediaSource.isTypeSupported = (mime) =>
        !mime.includes("mp4a") && supported(mime);
    });
    const requests = await routeLive(frigateApp);
    await frigateApp.goto("/#front_door");
    const video = frigateApp.page
      .locator('[data-camera="front_door"] video')
      .first();
    await expect
      .poll(
        () => video.evaluate((element: HTMLVideoElement) => element.videoWidth),
        { timeout: 15000 },
      )
      .toBe(1280);
    await expect
      .poll(() =>
        video.evaluate(
          (element: HTMLVideoElement) =>
            element.getVideoPlaybackQuality().totalVideoFrames,
        ),
      )
      .toBeGreaterThan(2);
    expect(
      requests
        .find((url) => url.pathname.endsWith("/master.m3u8"))
        ?.searchParams.get("audio"),
    ).toBe("false");
    expect(requests.some((url) => url.pathname.includes("/vseg"))).toBe(true);
  });
});
