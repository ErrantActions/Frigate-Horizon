import { test, expect, type FrigateApp } from "../fixtures/frigate-test";
import { systemTab } from "../helpers/system-tabs";

async function nativeHealth(
  app: FrigateApp,
  options: {
    stale?: boolean;
    nativeError?: boolean;
    recordingError?: boolean;
    missing?: boolean;
    decoded?: boolean;
  } = {},
) {
  const now = Date.now() / 1000;
  await app.installDefaults({
    config: {
      cameras: {
        front_door: {
          capture_mode: "recording_only",
          record: { enabled: true },
        },
        backyard: {
          enabled: !!options.decoded,
          capture_mode: "decoded_detection",
        },
        garage: { enabled: false },
      },
    },
    stats: {
      detectors: { cpu: { inference_speed: 10 } },
      service: { uptime: 10000, last_updated: now },
      cameras: {
        front_door: {
          capture_mode: "recording_only",
          camera_fps: 0,
          process_fps: 0,
          skipped_fps: 0,
          ffmpeg_pid: 0,
          connection_quality: "unusable",
          expected_fps: 5,
        },
        backyard: {
          capture_mode: "decoded_detection",
          camera_fps: 0,
          connection_quality: "unusable",
        },
      },
    },
    notices: [
      {
        id: "object_processing_behind",
        kind: "object_processing_behind",
        category: "system",
        severity: "warning",
        scope: null,
        params: {},
        link: "/system#general",
        count: 99,
        first_seen: now - 86400,
        last_seen: now - 3600,
        acknowledgeable: true,
        acknowledged_at: null,
        muted_at: null,
      },
    ],
  });
  const requests: string[] = [];
  await app.page.route(
    "**/api/front_door/compressed-motion-preview**",
    (route) => {
      requests.push(route.request().method());
      if (options.nativeError) return route.fulfill({ status: 503, json: {} });
      return route.fulfill({
        json: {
          camera: "front_door",
          native_detection: {
            camera: "front_door",
            source: "reolink_http_api",
            state: "running",
            updated_at: Date.now() / 1000 - (options.stale ? 30 : 0.1),
            states: {
              motion: false,
              person: false,
              vehicle: false,
              animal: false,
            },
            history: [],
            events_started: 2,
            events_ended: 2,
            video_decoded: false,
            confidence_available: false,
          },
        },
        headers: { date: new Date().toUTCString() },
      });
    },
  );
  await app.page.route("**/api/front_door/recordings**", (route) => {
    if (options.recordingError) return route.fulfill({ status: 503, json: {} });
    const now = Date.now() / 1000;
    return route.fulfill({
      json: options.missing
        ? []
        : [{ start_time: now - 20, end_time: now - 10 }],
      headers: { date: new Date().toUTCString() },
    });
  });
  return requests;
}

test.describe("Native camera health @mobile", () => {
  test.use({
    expectedErrors: [
      /Failed to load resource:.*503|503.*compressed-motion-preview|503.*recordings/,
    ],
  });

  test("zero decoded FPS is expected, source checks are current, inference is standby", async ({
    frigateApp,
  }) => {
    const requests = await nativeHealth(frigateApp);
    await frigateApp.goto("/system");
    const cell = frigateApp.page.getByTestId("camera-connection-front_door");
    await expect(cell.getByTestId("connection-detection")).toHaveText(
      "Reporting",
    );
    await expect(cell.getByTestId("connection-recording")).toHaveText(
      "Reporting",
    );
    await expect(cell.getByText(/decoded FPS is not expected/)).toBeVisible();
    await expect(cell.getByText(/0\.0.*5/)).toHaveCount(0);
    await expect(
      frigateApp.page.getByTestId("hardware-row-detection:0"),
    ).toHaveAttribute("data-state", "idle");
    await expect(frigateApp.page.getByTestId("camera-streams")).toContainText(
      "Optional input-stream diagnostics",
    );
    await expect(
      frigateApp.page.getByText(/Historical tracked-object processing warning/),
    ).toBeVisible();
    expect(requests.every((method) => method === "GET")).toBe(true);
  });

  test("native API failure is not hidden by fresh recording metadata", async ({
    frigateApp,
  }) => {
    await nativeHealth(frigateApp, { nativeError: true });
    await frigateApp.goto("/system");
    const cell = frigateApp.page.getByTestId("camera-connection-front_door");
    await expect(cell.getByTestId("connection-detection")).toHaveText(
      "Unavailable",
    );
    await expect(cell.getByTestId("connection-recording")).toHaveText(
      "Reporting",
    );
  });

  test("recording failure is independent of native alarm health", async ({
    frigateApp,
  }) => {
    await nativeHealth(frigateApp, { recordingError: true });
    await frigateApp.goto("/system");
    const cell = frigateApp.page.getByTestId("camera-connection-front_door");
    await expect(cell.getByTestId("connection-detection")).toHaveText(
      "Reporting",
    );
    await expect(cell.getByTestId("connection-recording")).toHaveText(
      "Unavailable",
    );
  });

  test("stale API and missing recording segments never report healthy", async ({
    frigateApp,
  }) => {
    await nativeHealth(frigateApp, { stale: true, missing: true });
    await frigateApp.goto("/system");
    const cell = frigateApp.page.getByTestId("camera-connection-front_door");
    await expect(cell.getByTestId("connection-detection")).toHaveText("Stale");
    await expect(cell.getByTestId("connection-recording")).toHaveText("Stale");
  });

  test("decoded-camera faults and processing warnings remain actionable in mixed setups", async ({
    frigateApp,
  }) => {
    await nativeHealth(frigateApp, { decoded: true });
    await frigateApp.goto("/system");
    await expect(
      frigateApp.page.getByTestId("camera-connection-backyard"),
    ).toContainText("0.0");
    await expect(
      frigateApp.page.getByText(/Historical tracked-object processing warning/),
    ).toHaveCount(0);
    await expect(
      frigateApp.page
        .getByText(/Tracked object processing could not keep up/)
        .first(),
    ).toBeVisible();
  });

  test("leaving Health stops metadata polling", async ({ frigateApp }) => {
    await frigateApp.page.clock.install();
    const requests = await nativeHealth(frigateApp);
    await frigateApp.goto("/system");
    await expect(
      frigateApp.page.getByTestId("connection-detection"),
    ).toHaveText("Reporting");
    await frigateApp.page.evaluate(() => {
      location.hash = "general";
    });
    await expect(systemTab(frigateApp, "general")).toHaveAttribute(
      "data-state",
      "on",
    );
    const before = requests.length;
    await frigateApp.page.clock.fastForward(15000);
    expect(requests.length).toBe(before);
  });
});
