import { createServer, type Server } from "node:http";
import { readFile } from "node:fs/promises";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { test, expect } from "../fixtures/frigate-test";

const media = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../fixtures/media/live-hls",
);

let server: Server;
let origin: string;

test.beforeAll(async () => {
  server = createServer(async (request, response) => {
    const path = new URL(request.url ?? "/", "http://localhost").pathname;
    const match =
      /^\/(720p|1080p)\/(index\.m3u8|init_[01]\.mp4|seg\d{3}\.mp4)$/.exec(path);
    if (path !== "/master.m3u8" && !match) {
      response.writeHead(404).end();
      return;
    }
    let file: string;
    if (path === "/master.m3u8") {
      file = "master.m3u8";
    } else if (match) {
      file = `${match[1] === "720p" ? "0" : "1"}/${match[2].replace(/^(seg\d{3})\.mp4$/, "$1.m4s")}`;
    } else {
      response.writeHead(404).end();
      return;
    }
    let data: Buffer;
    try {
      data = await readFile(resolve(media, file));
    } catch (error) {
      response.writeHead(500).end(String(error));
      return;
    }
    const playlist = file.endsWith(".m3u8");
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
      "Access-Control-Allow-Origin": "http://localhost:4173",
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

test.describe("Bandwidth-adaptive HTTPS live playback @mobile", () => {
  test("starts low, upgrades on real throughput and downgrades when bandwidth falls", async ({
    frigateApp,
  }) => {
    test.setTimeout(120000);
    const { page } = frigateApp;
    await frigateApp.installDefaults({
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
              height: 1080,
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
    let releaseMaster: () => void = () => {};
    const masterGate = new Promise<void>((ready) => {
      releaseMaster = ready;
    });
    const requests: string[] = [];
    await page.route("**/api/front_door/live/hls/**", async (route) => {
      const path = new URL(route.request().url()).pathname.split(
        "/live/hls/",
      )[1];
      if (path === "status") {
        await route.fulfill({
          json: {
            version: 1,
            protocol: "ll-hls",
            enabled: true,
            available: true,
            playlist: "front_door/live/hls/master.m3u8",
            renditions: [
              { height: 720, bitrate: 200 },
              { height: 1080, bitrate: 650 },
            ],
          },
        });
        return;
      }
      if (path === "master.m3u8") await masterGate;
      requests.push(path);
      await route.continue({ url: `${origin}/${path}` });
    });
    await frigateApp.goto("/#front_door");
    const cdp = await page.context().newCDPSession(page);
    const bandwidth = async (bytesPerSecond: number) => {
      await cdp.send("Network.emulateNetworkConditions", {
        offline: false,
        latency: 20,
        downloadThroughput: bytesPerSecond,
        uploadThroughput: 1000000,
      });
    };
    await cdp.send("Network.enable");
    await bandwidth(65000);
    releaseMaster();
    const card = page.locator('[data-camera="front_door"]').first();
    const video = card.locator('[data-live-transport="hls"]');
    await expect(video).toBeVisible({ timeout: 30000 });
    await expect
      .poll(
        () => video.evaluate((element: HTMLVideoElement) => element.videoWidth),
        { timeout: 30000 },
      )
      .toBe(1280);
    expect(
      requests.find(
        (path) => path.startsWith("720p/") || path.startsWith("1080p/"),
      ),
    ).toMatch(/^720p\//);
    await expect(card).toHaveAttribute(
      "data-live-stream",
      "front_door_transcode_720p",
    );

    await bandwidth(500000);
    await expect(card).toHaveAttribute(
      "data-live-stream",
      "front_door_transcode_1080p",
      { timeout: 30000 },
    );
    await expect
      .poll(() =>
        video.evaluate((element: HTMLVideoElement) => element.videoWidth),
      )
      .toBe(1920);
    await bandwidth(65000);
    await expect(card).toHaveAttribute(
      "data-live-stream",
      "front_door_transcode_720p",
      { timeout: 30000 },
    );
    await expect(video).toBeVisible();
    await expect
      .poll(() =>
        video.evaluate((element: HTMLVideoElement) => element.videoWidth),
      )
      .toBe(1280);
    await expect(card.getByTestId("live-warning-badge")).toHaveCount(0);
    await cdp.detach();
  });
});
