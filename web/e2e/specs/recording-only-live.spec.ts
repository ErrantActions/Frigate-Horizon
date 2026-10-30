import { test, expect } from "../fixtures/frigate-test";

test.describe("Recording-only live viewing @mobile", () => {
  test("plays an idle recording-only camera without detection frames or decoded previews", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    await frigateApp.installDefaults({
      config: {
        cameras: {
          front_door: {
            capture_mode: "recording_only",
            detect: { enabled: false },
            motion: { enabled: false },
            live: { streams: { Main: "front_door" } },
          },
        },
      },
      stats: {
        detectors: { cpu: { inference_speed: 10 } },
        cameras: {
          front_door: {
            capture_mode: "recording_only",
            camera_fps: 0,
            process_fps: 0,
            detection_fps: 0,
            ffmpeg_pid: 0,
          },
          backyard: { camera_fps: 0 },
        },
      },
    });
    const liveConnections: string[] = [];
    const decodedRequests: string[] = [];
    await page.routeWebSocket(
      (url) => url.pathname.startsWith("/live/"),
      (ws) => {
        liveConnections.push(ws.url());
        ws.onMessage(() => {});
      },
    );
    page.on("request", (request) => {
      if (/\/api\/front_door\/latest\./.test(request.url())) {
        decodedRequests.push(request.url());
      }
    });

    await frigateApp.goto("/");
    const camera = page.locator('[data-camera="front_door"]').first();
    await expect(camera.locator("video")).toHaveCount(1);
    await expect(camera.getByText("Stream Offline")).toHaveCount(0);
    await expect
      .poll(() => liveConnections.filter((url) => url.includes("front_door")))
      .not.toHaveLength(0);
    await expect(
      page.locator('[data-camera="backyard"]').getByText("Stream Offline"),
    ).toBeVisible();
    if (!frigateApp.isMobile) {
      await expect(
        page.getByText("Backyard is offline", { exact: true }),
      ).toBeVisible();
    }
    await expect(page.getByText(/front[ _]door is offline/i)).toHaveCount(0);
    expect(decodedRequests).toEqual([]);
    expect(liveConnections.every((url) => !url.includes("jsmpeg"))).toBe(true);
  });

  test("single-camera recording-only viewing uses a direct stream", async ({
    frigateApp,
  }) => {
    await frigateApp.installDefaults({
      config: {
        cameras: {
          front_door: { capture_mode: "recording_only" },
        },
      },
      stats: {
        cameras: {
          front_door: { camera_fps: 0, capture_mode: "recording_only" },
        },
      },
    });
    await frigateApp.page.routeWebSocket(
      (url) => url.pathname.startsWith("/live/"),
      (ws) => ws.onMessage(() => {}),
    );
    await frigateApp.goto("/#front_door");
    const camera = frigateApp.page
      .locator('[data-camera="front_door"]')
      .first();
    await expect(camera.locator("video")).toHaveCount(1);
    await expect(camera.getByText("Stream Offline")).toHaveCount(0);
    await expect(camera.locator("img")).toHaveCount(0);
  });

  test("does not report all recording-only cameras offline in the global status", async ({
    frigateApp,
  }) => {
    await frigateApp.installDefaults({
      config: {
        cameras: {
          front_door: { capture_mode: "recording_only" },
          backyard: { capture_mode: "recording_only" },
          garage: { capture_mode: "recording_only" },
        },
      },
      stats: {
        detectors: { cpu: { inference_speed: 10 } },
        cameras: {
          front_door: { camera_fps: 0, capture_mode: "recording_only" },
          backyard: { camera_fps: 0, capture_mode: "recording_only" },
          garage: { camera_fps: 0, capture_mode: "recording_only" },
        },
      },
    });
    await frigateApp.page.routeWebSocket(
      (url) => url.pathname.startsWith("/live/"),
      (ws) => ws.onMessage(() => {}),
    );
    await frigateApp.goto("/");
    await expect(
      frigateApp.page.locator('[data-camera="front_door"] video').first(),
    ).toHaveCount(1);
    await expect(frigateApp.page.getByText(/is offline/i)).toHaveCount(0);
  });
});
