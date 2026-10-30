import { test, expect, type FrigateApp } from "../../fixtures/frigate-test";
import { configFactory } from "../../fixtures/mock-data/config";
import {
  DEFAULT_COMPRESSION_POLICY,
  type CompressionJob,
  type CompressionJobs,
} from "../../../src/types/compression";

const BASE = "/api/recordings/compression/jobs";
const SETTINGS = "/settings?page=recordingCompression";

async function installCompression(app: FrigateApp, readersLeased = true) {
  const policy = { ...DEFAULT_COMPRESSION_POLICY, enabled: true };
  let job: CompressionJob | undefined;
  const writes: { path: string; body: unknown }[] = [];
  await app.page.route("**/api/config/set", async (route) => {
    const body = route.request().postDataJSON();
    writes.push({ path: "/api/config/set", body });
    Object.assign(policy, body.config_data.storage.compression);
    await route.fulfill({ json: { success: true } });
  });
  await app.page.route("**/api/config", async (route) => {
    const config = configFactory();
    await route.fulfill({
      json: { ...config, storage: { ...config.storage, compression: policy } },
    });
  });
  await app.page.route(`**${BASE}**`, async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST") {
      const body = request.postDataJSON();
      writes.push({ path, body });
      if (path === BASE) {
        job = {
          id: "compression-job",
          status: "running",
          created_at: 1792350000,
          updated_at: 1792350000,
          scheduled: false,
          processed: 1,
          compressed: 1,
          skipped: 0,
          failed: 0,
          saved_bytes: 12345,
          max_files: 100,
          target_mbps: 4,
          encoder: policy.encoder,
          video_codec: "hevc",
          cpu_threads: policy.cpu_threads,
          svt_preset: policy.svt_preset,
          frame_rate: policy.frame_rate,
          min_age_days: 14,
          current_item_id: null,
          reason: null,
        };
      } else if (job) {
        job = {
          ...job,
          status:
            body.action === "pause"
              ? "paused"
              : body.action === "resume"
                ? "running"
                : "stopped",
        };
      }
      return route.fulfill({ status: 202, json: job });
    }
    if (path.endsWith("/items")) {
      return route.fulfill({ json: { items: [], total: 0 } });
    }
    const result: CompressionJobs = {
      jobs: job ? [job] : [],
      enabled: true,
      readers_leased: readersLeased,
      recovery_required: false,
      archive_supported: true,
      reservations_ready: true,
    };
    return route.fulfill({ json: result });
  });
  return { writes };
}

test.describe("older-footage compression @mobile", () => {
  test("saves explicit encoder, bitrate, CPU and cadence choices without starting a job", async ({
    frigateApp,
  }) => {
    await frigateApp.installDefaults();
    const { writes } = await installCompression(frigateApp, false);
    await frigateApp.goto(SETTINGS);
    await frigateApp.page
      .getByLabel("Video codec and encoder")
      .selectOption("libsvtav1");
    await frigateApp.page
      .getByLabel("Target average video bitrate (Mbps)")
      .fill("5.5");
    await frigateApp.page.getByLabel("Maximum logical CPU cores").fill("8");
    await frigateApp.page
      .getByLabel("SVT-AV1 speed preset (higher is faster)")
      .fill("12");
    await frigateApp.page
      .getByLabel("Fixed output FPS (blank keeps original cadence)")
      .fill("16");
    await frigateApp.page
      .getByRole("button", { name: "Save compression settings", exact: true })
      .click();
    await expect(
      frigateApp.page.getByLabel("Video codec and encoder"),
    ).toHaveValue("libsvtav1");
    expect(writes).toHaveLength(1);
    expect(writes[0].body).toMatchObject({
      requires_restart: 0,
      config_data: {
        storage: {
          compression: {
            encoder: "libsvtav1",
            target_mbps: 5.5,
            cpu_threads: 8,
            svt_preset: 12,
            frame_rate: 16,
          },
        },
      },
    });
    await expect(
      frigateApp.page.getByRole("button", { name: "Start bounded batch" }),
    ).toBeDisabled();
    await frigateApp.page
      .getByLabel("Video codec and encoder")
      .selectOption("libx264");
    await expect(
      frigateApp.page.getByLabel("SVT-AV1 speed preset (higher is faster)"),
    ).toHaveCount(0);
  });

  test("requires explicit confirmation and supports pause resume stop", async ({
    frigateApp,
  }) => {
    const app = frigateApp;
    await app.installDefaults();
    const { writes } = await installCompression(app);
    await app.goto(SETTINGS);
    await expect(
      app.page.getByRole("heading", { name: "Older-footage compression" }),
    ).toBeVisible();
    expect(writes).toHaveLength(0);
    await app.page.getByRole("button", { name: "Start bounded batch" }).click();
    await expect(app.page.getByRole("alertdialog")).toBeVisible();
    await app.page.getByRole("button", { name: "Cancel", exact: true }).click();
    expect(writes).toHaveLength(0);
    await app.page.getByRole("button", { name: "Start bounded batch" }).click();
    await app.page
      .getByRole("button", { name: "Start compression", exact: true })
      .click();
    await expect(
      app.page.getByRole("button", { name: "Pause", exact: true }),
    ).toBeEnabled();
    await app.page.getByRole("button", { name: "Pause", exact: true }).click();
    await expect(app.page.getByRole("status")).toHaveText("Paused");
    await app.page.getByRole("button", { name: "Resume", exact: true }).click();
    await expect(app.page.getByRole("status")).toHaveText("Running");
    await app.page.getByRole("button", { name: "Stop", exact: true }).click();
    await expect(app.page.getByRole("status")).toHaveText("Stopped");
    expect(writes.map((write) => write.body)).toEqual([
      { confirm: true },
      { action: "pause" },
      { action: "resume" },
      { action: "stop" },
    ]);
  });

  test("fails closed before original readers are certified", async ({
    frigateApp,
  }) => {
    const app = frigateApp;
    await app.installDefaults();
    const { writes } = await installCompression(app, false);
    await app.goto(SETTINGS);
    await expect(app.page.getByRole("alert")).toContainText(
      "renewable recording leases",
    );
    await expect(
      app.page.getByRole("button", { name: "Start bounded batch" }),
    ).toBeDisabled();
    await expect(
      app.page.getByText(/Local and NAS archive recordings are supported/),
    ).toBeVisible();
    expect(writes).toHaveLength(0);
  });
});
