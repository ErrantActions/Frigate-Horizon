import { test, expect, type FrigateApp } from "../fixtures/frigate-test";
import { systemTab } from "../helpers/system-tabs";

function native(state = "running", age = 0.2) {
  const now = Date.now() / 1000;
  return {
    camera: "front_door",
    available: true,
    status: "stopped",
    data: null,
    native_detection: {
      camera: "front_door",
      source: "reolink_http_api",
      state,
      updated_at: now - age,
      video_decoded: false,
      confidence_available: false,
      events_started: 8,
      events_ended: 7,
      states: { motion: false, person: false, vehicle: true, animal: false },
      history: [{ label: "vehicle", active: true, received_at: now - 2 }],
    },
  };
}

async function reports(
  app: FrigateApp,
  options: {
    state?: string;
    age?: number;
    nativeError?: boolean;
    recordingError?: boolean;
    invalidCamera?: boolean;
    invalidClock?: boolean;
    invalidPayload?: boolean;
    emptyRecordings?: boolean;
  } = {},
) {
  await app.installDefaults({
    config: {
      cameras: {
        front_door: {
          capture_mode: "recording_only",
          detect: { enabled: false },
          record: { enabled: true },
        },
        backyard: {
          capture_mode: "decoded_detection",
          record: { enabled: true },
        },
        garage: { enabled: false },
      },
    },
    stats: {
      service: { last_updated: Date.now() / 1000 },
      cameras: {
        front_door: {
          capture_mode: "recording_only",
          camera_fps: 0,
          process_fps: 0,
          detection_fps: 0,
          ffmpeg_pid: 0,
        },
        backyard: { capture_mode: "decoded_detection" },
      },
    },
  });
  let polls = 0;
  const methods: string[] = [];
  const cameras: string[] = [];
  await app.page.route("**/api/*/compressed-motion-preview**", (route) => {
    polls++;
    methods.push(route.request().method());
    const name = new URL(route.request().url()).pathname.split("/")[2];
    cameras.push(name);
    if (name !== "front_door")
      return route.fulfill({
        json: { camera: name, status: "stopped", available: true, data: null },
        headers: { date: new Date().toUTCString() },
      });
    if (options.nativeError) return route.fulfill({ status: 503, json: {} });
    const value = native(options.state, options.age);
    if (options.invalidCamera) value.camera = "private_other_camera";
    if (options.invalidPayload)
      value.native_detection.updated_at = Number.MAX_SAFE_INTEGER;
    return route.fulfill({
      json: value,
      headers: {
        date: options.invalidClock ? "invalid-clock" : new Date().toUTCString(),
      },
    });
  });
  await app.page.route("**/api/*/recordings**", (route) => {
    if (!new URL(route.request().url()).pathname.endsWith("/recordings"))
      return route.fallback();
    methods.push(route.request().method());
    if (options.recordingError) return route.fulfill({ status: 503, json: {} });
    const now = Date.now() / 1000;
    return route.fulfill({
      json: options.emptyRecordings
        ? []
        : [{ start_time: now - 20, end_time: now - 10 }],
      headers: { date: new Date().toUTCString() },
    });
  });
  return { polls: () => polls, methods, cameras };
}

test.describe("Camera detection reports @mobile", () => {
  test.use({
    expectedErrors: [
      [
        /503.*compressed-motion-preview/,
        /503.*recordings/,
        /Failed to load resource:.*503/,
      ],
      { scope: "test" },
    ],
  });
  test("separates native API, recording and decoder health without starting video", async ({
    frigateApp,
  }) => {
    const observed = await reports(frigateApp);
    const unwanted: string[] = [];
    frigateApp.page.on("request", (request) => {
      if (/\/latest\.|\/live\/|config\/set/.test(request.url()))
        unwanted.push(request.url());
    });
    await frigateApp.goto("/system#general");
    await frigateApp.page
      .getByRole("button", { name: "Diagnostic tools", exact: true })
      .click();
    await frigateApp.page
      .getByRole("menuitem", { name: "Detection Reports", exact: true })
      .click();
    await expect(frigateApp.page).toHaveURL(/#detectionReports/);
    const card = frigateApp.page.getByTestId("detection-report-front_door");
    await expect(card.getByTestId("detection-health")).toHaveText("Reporting");
    await expect(card.getByTestId("recording-health")).toHaveText("Reporting");
    await expect(
      card.getByText("Reolink HTTP API", { exact: true }),
    ).toBeVisible();
    await expect(
      card.getByText("Vehicle: active", { exact: true }),
    ).toBeVisible();
    await expect(card.getByText("Off", { exact: true })).toBeVisible();
    await expect(
      card.getByText("8 opened / 7 closed", { exact: true }),
    ).toBeVisible();
    await expect(
      frigateApp.page
        .getByTestId("detection-report-backyard")
        .getByText("Frigate", { exact: true }),
    ).toBeVisible();
    await expect(
      frigateApp.page
        .getByTestId("detection-report-garage")
        .getByTestId("detection-health"),
    ).toHaveText("Disabled");
    expect(unwanted).toEqual([]);
    expect(observed.methods.every((method) => method === "GET")).toBe(true);
    expect(observed.cameras).not.toContain("garage");
  });

  test("does not hide native API failure behind fresh recordings", async ({
    frigateApp,
  }) => {
    await reports(frigateApp, { nativeError: true });
    await frigateApp.goto("/system#detectionReports");
    const card = frigateApp.page.getByTestId("detection-report-front_door");
    await expect(card.getByTestId("detection-health")).toHaveText(
      "Unavailable",
    );
    await expect(card.getByTestId("recording-health")).toHaveText("Reporting");
    await expect(card.getByRole("alert")).toBeVisible();
    await expect(card.getByText("Vehicle: clear", { exact: true })).toHaveCount(
      0,
    );
  });

  test("stale alarm states are unknown rather than quiet", async ({
    frigateApp,
  }) => {
    await reports(frigateApp, { age: 30 });
    await frigateApp.goto("/system#detectionReports");
    const card = frigateApp.page.getByTestId("detection-report-front_door");
    await expect(card.getByTestId("detection-health")).toHaveText("Stale");
    await expect(card.getByTestId("recording-health")).toHaveText("Reporting");
    await expect(
      card.getByText("Vehicle: Unknown", { exact: true }),
    ).toBeVisible();
    await expect(
      card.getByText("Vehicle: active", { exact: true }),
    ).toHaveCount(0);
  });

  test("recording failure does not erase a working detection API report", async ({
    frigateApp,
  }) => {
    await reports(frigateApp, { recordingError: true });
    await frigateApp.goto("/system#detectionReports");
    const card = frigateApp.page.getByTestId("detection-report-front_door");
    await expect(card.getByTestId("detection-health")).toHaveText("Reporting");
    await expect(card.getByTestId("recording-health")).toHaveText(
      "Unavailable",
    );
  });

  test("invalid clocks and mismatched cameras never appear healthy", async ({
    frigateApp,
  }) => {
    await reports(frigateApp, { invalidClock: true });
    await frigateApp.goto("/system#detectionReports");
    const card = frigateApp.page.getByTestId("detection-report-front_door");
    await expect(card.getByTestId("detection-health")).toHaveText("Unknown");
    await reports(frigateApp, { invalidCamera: true });
    await frigateApp.goto("/system#detectionReports");
    await expect(card.getByTestId("detection-health")).toHaveText(
      "Unavailable",
    );
    await expect(
      frigateApp.page.getByText("private_other_camera", { exact: true }),
    ).toHaveCount(0);
  });

  test("leaving the tab stops metadata polling", async ({ frigateApp }) => {
    await frigateApp.page.route("**/api/stats/history?**", (route) =>
      route.fulfill({ json: [] }),
    );
    await frigateApp.page.clock.install();
    const observed = await reports(frigateApp);
    await frigateApp.goto("/system#detectionReports");
    await expect(
      frigateApp.page
        .getByTestId("detection-report-front_door")
        .getByTestId("detection-health"),
    ).toHaveText("Reporting");
    await frigateApp.page.evaluate(() => {
      window.location.hash = "general";
    });
    await expect(systemTab(frigateApp, "general")).toHaveAttribute(
      "data-state",
      "on",
    );
    const before = observed.polls();
    await frigateApp.page.clock.fastForward(15000);
    expect(observed.polls()).toBe(before);
  });

  test("missing recording segments do not mean the camera is offline", async ({
    frigateApp,
  }) => {
    await reports(frigateApp, { emptyRecordings: true });
    await frigateApp.goto("/system#detectionReports");
    const card = frigateApp.page.getByTestId("detection-report-front_door");
    await expect(card.getByTestId("detection-health")).toHaveText("Reporting");
    await expect(card.getByTestId("recording-health")).toHaveText("Stale");
  });

  test("invalid native timestamps cannot crash or produce a healthy report", async ({
    frigateApp,
  }) => {
    await reports(frigateApp, { invalidPayload: true });
    await frigateApp.goto("/system#detectionReports");
    const card = frigateApp.page.getByTestId("detection-report-front_door");
    await expect(card.getByTestId("detection-health")).toHaveText(
      "Unavailable",
    );
    await expect(card.getByTestId("recording-health")).toHaveText("Reporting");
  });

  test("viewer access does not expose native-report requests or the admin tab", async ({
    frigateApp,
  }) => {
    const observed = await reports(frigateApp);
    await frigateApp.page.route("**/api/profile", (route) =>
      route.fulfill({
        json: { username: "viewer", role: "viewer" },
      }),
    );
    await frigateApp.goto("/system#detectionReports");
    await expect(
      frigateApp.page.getByRole("heading", { name: "Access Denied" }),
    ).toBeVisible();
    await expect(systemTab(frigateApp, "Detection Reports")).toHaveCount(0);
    await expect(
      frigateApp.page.getByTestId("detection-report-front_door"),
    ).toHaveCount(0);
    expect(observed.polls()).toBe(0);
  });
});
