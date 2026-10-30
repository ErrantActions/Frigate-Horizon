import { test, expect, type FrigateApp } from "../../fixtures/frigate-test";
import type {
  AudioRepairItem,
  AudioRepairJob,
} from "../../../src/types/audioRepair";

const BASE = "/api/recordings/audio-repair/jobs";
const SETTINGS = "/settings?page=recordingAudioRepair";
const created = 1792332000;

async function installRepair(
  app: FrigateApp,
  initial: "empty" | "scanning" | "running" = "empty",
) {
  const candidate: AudioRepairItem = {
    id: "audio-candidate",
    camera: "front_door",
    start_time: created - 600,
    end_time: created - 590,
    video_codec: "hevc",
    audio_codec: "aac",
    audio_rate: 16000,
    has_audio: true,
    state: "candidate",
    reason: null,
  };
  const items: AudioRepairItem[] = [
    candidate,
    {
      ...candidate,
      id: "no-audio",
      has_audio: false,
      audio_codec: null,
      audio_rate: null,
      state: "skipped",
      reason: "no-audio",
    },
    {
      ...candidate,
      id: "saved-evidence",
      state: "skipped",
      reason: "protected",
    },
  ];
  let job: AudioRepairJob | undefined =
    initial === "empty" ? undefined : createJob(initial);
  const writes: Array<{ action: string; body: unknown }> = [];

  function createJob(
    status: AudioRepairJob["status"] = "ready",
  ): AudioRepairJob {
    return {
      id: "repair-job",
      status,
      phase: status === "running" ? "repair" : "scan",
      created_at: created,
      updated_at: created,
      total: 3,
      scanned: status === "scanning" ? 1 : 3,
      eligible: status === "scanning" ? 0 : 1,
      selected: status === "running" ? 1 : 0,
      processed: 0,
      repaired: 0,
      skipped: status === "scanning" ? 0 : 2,
      failed: 0,
      current_item_id: status === "running" ? candidate.id : null,
      current_item: status === "running" ? candidate : null,
      error: null,
    };
  }

  await app.page.route(`**${BASE}**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "POST") {
      const action =
        url.pathname === BASE ? "scan" : url.pathname.split("/").at(-1)!;
      let body: unknown;
      if (request.postData()) body = request.postDataJSON();
      writes.push({ action, body });
      if (action === "scan") job = createJob();
      else {
        if (!job) throw new Error("No mock job exists");
        if (action === "start") {
          job = {
            ...job,
            status: "running",
            phase: "repair",
            eligible: 0,
            selected: 1,
            current_item_id: candidate.id,
            current_item: candidate,
          };
        } else if (action === "pause") job = { ...job, status: "paused" };
        else if (action === "resume") {
          job = {
            ...job,
            status: job.phase === "scan" ? "scanning" : "running",
          };
        } else if (action === "stop") job = { ...job, status: "stopped" };
      }
      return route.fulfill({
        status: action === "scan" ? 202 : 200,
        json: job,
      });
    }
    if (url.pathname === BASE) {
      return route.fulfill({
        json: {
          jobs: job ? [job] : [],
          total: job ? 1 : 0,
          limit: 10,
          offset: 0,
        },
      });
    }
    if (url.pathname.endsWith("/items")) {
      const state = url.searchParams.get("state");
      const filtered = state
        ? items.filter((item) => item.state === state)
        : items;
      return route.fulfill({
        json: { items: filtered, total: filtered.length, limit: 50, offset: 0 },
      });
    }
    return route.fulfill({ json: job });
  });
  await app.goto(SETTINGS);
  return { writes };
}

test.describe("Recording audio repair @mobile", () => {
  test("scan lists candidates and skips without rewriting anything", async ({
    frigateApp,
  }) => {
    const repair = await installRepair(frigateApp);
    await expect(
      frigateApp.page.getByText("No audio repair scans have been started."),
    ).toBeVisible();
    expect(repair.writes).toEqual([]);
    await frigateApp.page
      .getByRole("button", { name: "Scan recordings", exact: true })
      .click();
    await expect(
      frigateApp.page.getByRole("status").filter({
        hasText: "Scan complete.",
      }),
    ).toBeVisible();
    expect(repair.writes).toEqual([
      {
        action: "scan",
        body: { limit: 1000 },
      },
    ]);
    await expect(
      frigateApp.page.getByRole("cell", { name: "hevc", exact: true }),
    ).toBeVisible();
    await frigateApp.page
      .getByLabel("Show recordings", { exact: true })
      .selectOption("");
    await expect(
      frigateApp.page.getByText("No audio track exists to repair.", {
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText("Protected evidence is not rewritten.", {
        exact: true,
      }),
    ).toBeVisible();
    expect(
      repair.writes.filter((request) => request.action === "start"),
    ).toEqual([]);
  });

  test("selected repair requires confirmation and can pause resume and stop", async ({
    frigateApp,
  }) => {
    const repair = await installRepair(frigateApp);
    await frigateApp.page
      .getByRole("button", { name: "Scan recordings", exact: true })
      .click();
    await frigateApp.page
      .getByRole("checkbox", { name: /Select front_door recording at/ })
      .check();
    await frigateApp.page
      .getByRole("button", { name: "Start selected (1)", exact: true })
      .click();
    await expect(frigateApp.page.getByRole("alertdialog")).toBeVisible();
    expect(repair.writes.map((value) => value.action)).toEqual(["scan"]);
    await frigateApp.page
      .getByRole("button", { name: "Start repair", exact: true })
      .click();
    await expect(
      frigateApp.page
        .getByRole("status")
        .filter({ hasText: "Repairing audio." }),
    ).toBeVisible();
    expect(repair.writes[1]).toEqual({
      action: "start",
      body: { item_ids: ["audio-candidate"] },
    });
    await frigateApp.page
      .getByRole("button", { name: "Pause", exact: true })
      .click();
    await expect(
      frigateApp.page
        .getByRole("status", { name: "" })
        .filter({ hasText: /^Paused$/ }),
    ).toBeVisible();
    await frigateApp.page
      .getByRole("button", { name: "Resume", exact: true })
      .click();
    await expect(
      frigateApp.page.getByRole("button", { name: "Pause", exact: true }),
    ).toBeVisible();
    await frigateApp.page
      .getByRole("button", { name: "Stop", exact: true })
      .click();
    await expect(
      frigateApp.page.getByRole("status").filter({ hasText: /^Stopped$/ }),
    ).toBeVisible();
    expect(repair.writes.map((value) => value.action)).toEqual([
      "scan",
      "start",
      "pause",
      "resume",
      "stop",
    ]);
  });

  test("all eligible repairs still require an explicit confirmation", async ({
    frigateApp,
  }) => {
    const repair = await installRepair(frigateApp);
    await frigateApp.page
      .getByRole("button", { name: "Scan recordings", exact: true })
      .click();
    await frigateApp.page
      .getByRole("button", { name: "Start all eligible", exact: true })
      .click();
    await frigateApp.page
      .getByRole("button", { name: "Cancel", exact: true })
      .click();
    expect(repair.writes.map((value) => value.action)).toEqual(["scan"]);
    await frigateApp.page
      .getByRole("button", { name: "Start all eligible", exact: true })
      .click();
    await frigateApp.page
      .getByRole("button", { name: "Start repair", exact: true })
      .click();
    await expect(
      frigateApp.page
        .getByRole("status")
        .filter({ hasText: "Repairing audio." }),
    ).toBeVisible();
    expect(repair.writes[1]).toEqual({
      action: "start",
      body: { all_eligible: true },
    });
  });

  test("an existing scan is discoverable and pausable without an automatic repair", async ({
    frigateApp,
  }) => {
    const repair = await installRepair(frigateApp, "scanning");
    await expect(
      frigateApp.page
        .getByRole("status")
        .filter({ hasText: "Scanning recordings." }),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByRole("button", {
        name: "Scan recordings",
        exact: true,
      }),
    ).toBeDisabled();
    expect(repair.writes).toEqual([]);
    await frigateApp.page
      .getByRole("button", { name: "Pause", exact: true })
      .click();
    await frigateApp.page
      .getByRole("button", { name: "Resume", exact: true })
      .click();
    await expect(
      frigateApp.page
        .getByRole("status")
        .filter({ hasText: "Scanning recordings." }),
    ).toBeVisible();
    await frigateApp.page
      .getByRole("button", { name: "Stop", exact: true })
      .click();
    await expect(
      frigateApp.page.getByRole("button", {
        name: "Scan recordings",
        exact: true,
      }),
    ).toBeEnabled();
    expect(repair.writes.map((value) => value.action)).toEqual([
      "pause",
      "resume",
      "stop",
    ]);
  });

  test("rejects an oversized scan before sending a request", async ({
    frigateApp,
  }) => {
    const repair = await installRepair(frigateApp);
    await expect(
      frigateApp.page.getByRole("button", {
        name: "Scan recordings",
        exact: true,
      }),
    ).toBeEnabled();
    await frigateApp.page
      .getByLabel("Maximum recordings to scan", { exact: true })
      .fill("10001");
    await frigateApp.page
      .getByRole("button", { name: "Scan recordings", exact: true })
      .click();
    await expect(
      frigateApp.page.getByText(
        "Enter a valid date range and a scan limit between 1 and 10,000.",
        { exact: true },
      ),
    ).toBeVisible();
    expect(repair.writes).toEqual([]);
  });
});
