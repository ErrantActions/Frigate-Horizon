import { test, expect } from "../fixtures/frigate-test";
import { viewerProfile } from "../fixtures/mock-data/profile";
import type { DiagnosticsReport } from "../../src/types/diagnostics";

const report: DiagnosticsReport = {
  sampled_at: 1700000001,
  stats_time: 1700000000,
  age_seconds: 1,
  stale: false,
  findings: [
    {
      code: "decoder_saturated",
      severity: "warning",
      confidence: "observed",
      camera: null,
      evidence: { decoder_percent: 100 },
    },
    {
      code: "output_queue_full",
      severity: "warning",
      confidence: "observed",
      camera: "front_door",
      evidence: { output_dropped_fps: 2, tracker_fps: 4, process_fps: 2 },
    },
  ],
  cameras: [
    {
      camera: "front_door",
      capture_fps: 5,
      process_fps: 2,
      skipped_fps: 1,
      skipped_percent: 20,
      detection_fps: 12,
      tracker_fps: 4,
      output_dropped_fps: 2,
      source_height: 1080,
      analysis_height: 720,
      detection_enabled: true,
    },
  ],
  gpus: [
    {
      gpu: "NVIDIA RTX A400",
      decoder_percent: 100,
      compute_percent: 18,
      temperature: 86,
    },
  ],
  detector_inference_ms: [7, 10],
  total_cameras: 1,
  truncated: false,
  ai_available: true,
};

test.describe("Interpreted system diagnostics @mobile", () => {
  test("diagnostic tools preserve navigation without reading hidden diagnostics", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    let reads = 0;
    await page.route("**/api/diagnostics", (route) => {
      reads++;
      return route.fulfill({ json: report });
    });
    await frigateApp.goto("/system#general");
    const tools = page.getByRole("button", {
      name: "Diagnostic tools",
      exact: true,
    });
    await tools.focus();
    await tools.press("Enter");
    const diagnostics = page.getByRole("menuitem", {
      name: "Diagnostics",
      exact: true,
    });
    await expect(diagnostics).toBeVisible();
    expect(reads).toBe(0);
    await diagnostics.click();
    await expect(page).toHaveURL(/#diagnostics/);
    await expect(
      page.getByRole("heading", { name: "Diagnostics", exact: true }),
    ).toBeVisible();
    expect(reads).toBeGreaterThan(0);
    await tools.click();
    await expect(
      page.getByRole("menuitem", { name: "Diagnostics", exact: true }),
    ).toHaveAttribute("aria-current", "page");
    await page.keyboard.press("Escape");
  });
  test("separates queue drops from recordings and does not automatically call AI or write config", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    let aiCalls = 0;
    let configWrites = 0;
    await page.route("**/api/diagnostics", (route) =>
      route.fulfill({ json: report }),
    );
    await page.route("**/api/diagnostics/explain", (route) => {
      aiCalls++;
      return route.fulfill({
        json: {
          text: "Measured decoder pressure.",
          based_on_stats_time: report.stats_time,
        },
      });
    });
    await page.route("**/api/config/set", (route) => {
      configWrites++;
      return route.fulfill({ json: { success: true } });
    });
    await frigateApp.goto("/system#diagnostics");
    await expect(
      page.getByRole("heading", { name: "Diagnostics", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText(/Detection skips are not proof of missing recordings/),
    ).toBeVisible();
    await expect(
      page.getByText(/A GPU decoder is at least 95% busy/),
    ).toBeVisible();
    await expect(
      page.getByText(/downstream object-processing queue is full/),
    ).toBeVisible();
    const explain = page.getByRole("button", {
      name: "Explain this snapshot with AI",
      exact: true,
    });
    await expect(explain).toBeDisabled();
    expect(aiCalls).toBe(0);
    await page
      .getByRole("checkbox", { name: /Allow this sanitized metrics snapshot/ })
      .check();
    await explain.click();
    await expect(
      page.getByText("Measured decoder pressure.", { exact: true }),
    ).toBeVisible();
    expect(aiCalls).toBe(1);
    expect(configWrites).toBe(0);
  });

  test("stale reports visibly block AI rather than claiming live health", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    await page.route("**/api/diagnostics", (route) =>
      route.fulfill({ json: { ...report, stale: true, age_seconds: 60 } }),
    );
    await frigateApp.goto("/system#diagnostics");
    await expect(
      page.getByRole("alert").filter({ hasText: /stats snapshot is stale/ }),
    ).toBeVisible();
    await page
      .getByRole("checkbox", { name: /Allow this sanitized metrics snapshot/ })
      .check();
    await expect(
      page.getByRole("button", {
        name: "Explain this snapshot with AI",
        exact: true,
      }),
    ).toBeDisabled();
  });

  test("shows unavailable stages as unknown and pauses polling when leaving the tab", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    let reads = 0;
    await page.route("**/api/diagnostics", (route) => {
      reads++;
      return route.fulfill({
        json: {
          ...report,
          cameras: [
            {
              ...report.cameras[0],
              tracker_fps: null,
              output_dropped_fps: null,
            },
          ],
        },
      });
    });
    await frigateApp.goto("/system#diagnostics");
    await expect(
      page.getByText("Unknown", { exact: true }).first(),
    ).toBeVisible();
    await page.clock.install();
    await page.goto("/system#general");
    const stopped = reads;
    await page.clock.fastForward(30000);
    expect(reads).toBe(stopped);
  });

  test("does not expose the admin diagnostics tab or make diagnostic requests to viewers", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    await frigateApp.installDefaults({ profile: viewerProfile() });
    let reads = 0;
    await page.route("**/api/diagnostics", (route) => {
      reads++;
      return route.fulfill({ json: report });
    });
    await frigateApp.goto("/system#diagnostics");
    await expect(
      page.getByRole("heading", { name: "Diagnostics", exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Select Diagnostics", exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Diagnostic tools", exact: true }),
    ).toHaveCount(0);
    expect(reads).toBe(0);
  });

  test("sends only explicit consent, and labels the explanation as a particular snapshot", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    await page.route("**/api/diagnostics", (route) =>
      route.fulfill({ json: report }),
    );
    await page.route("**/api/diagnostics/explain", (route) => {
      expect(route.request().postDataJSON()).toEqual({
        confirm_send_metrics: true,
      });
      return route.fulfill({
        json: {
          text: "Advisory only; no changes applied.",
          based_on_stats_time: report.stats_time,
        },
      });
    });
    await frigateApp.goto("/system#diagnostics");
    await page
      .getByRole("checkbox", { name: /Allow this sanitized metrics snapshot/ })
      .check();
    await page
      .getByRole("button", {
        name: "Explain this snapshot with AI",
        exact: true,
      })
      .click();
    await expect(
      page.getByText(/AI interpretation of the snapshot from/),
    ).toBeVisible();
    await expect(
      page.getByText("Advisory only; no changes applied.", { exact: true }),
    ).toBeVisible();
  });
});
