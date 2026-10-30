import { test, expect } from "../fixtures/frigate-test";
import { STORAGE_TIERS } from "../fixtures/mock-data/storage";
import { BASE_CONFIG } from "../fixtures/mock-data/config";
import type {
  ArchiveTransferProgress,
  StorageCapacity,
} from "../../src/types/storage";

const capacity: StorageCapacity = {
  state: "online",
  sampled_at: 1700000000,
  filesystem_id: "shared",
  filesystem_total_bytes: 10000,
  filesystem_available_bytes: 5000,
  usage_bytes: 3000,
  budget_bytes: 9000,
  budget_remaining_bytes: 5000,
  reserved_bytes: 1000,
  physical_reserved_bytes: 1000,
  written_reservation_bytes: 500,
  reserve_bytes: 100,
  effective_headroom_bytes: 4400,
  over_budget_bytes: 0,
  protected_bytes: null,
};

const transfer: ArchiveTransferProgress = {
  trigger: "local_pressure",
  active: true,
  started_at: 1700000000,
  phase: "verification",
  initial_plan_bytes: 4000,
  initial_plan_files: 2,
  plan_is_estimate: true,
  verified_bytes: 0,
  verified_files: 0,
  incoming_bytes: 1000,
  local_usage_bytes: 10000,
  drain_target_bytes: 9000,
  remaining_bytes: 1000,
  rate_bytes_per_second: null,
  ingest_bytes_per_second: null,
  eta_seconds: null,
  buffer_seconds: null,
  cannot_outpace_ingest: false,
  current_file: {
    name: "segment.mp4",
    camera: "front_door",
    start_time: 1700000000,
    end_time: 1700000010,
    size_bytes: 2000,
    copied_bytes: 2000,
    readback_bytes: 1000,
  },
};

test.describe("Storage optimization safeguards @mobile", () => {
  test.use({ expectedErrors: [/409|500|Internal Server Error|Conflict/] });

  test("two archive handles preserve the budget and rescale with its value", async ({
    frigateApp,
  }) => {
    let settings = {
      ...structuredClone(STORAGE_TIERS.settings),
      local_budget_gib: 3000,
      start_percent: 100,
      drain_percent: 70,
    };
    await frigateApp.page.route("**/api/recordings/storage/tiers", (route) =>
      route.fulfill({ json: { ...STORAGE_TIERS, settings } }),
    );
    await frigateApp.page.route("**/api/config/set", async (route) => {
      const body = route.request().postDataJSON();
      expect(body.config_data.storage).toEqual({
        local_budget_gib: 3000,
        start_percent: 60,
        drain_percent: 40,
      });
      expect(body.expected_storage).toEqual({
        local_budget_gib: 3000,
        start_percent: 100,
        drain_percent: 70,
      });
      settings = { ...settings, ...body.config_data.storage };
      await route.fulfill({ json: { success: true } });
    });
    await frigateApp.goto("/system#storage");
    await frigateApp.page
      .getByRole("button", { name: "Storage settings", exact: true })
      .click();
    const start = frigateApp.page.getByRole("slider", {
      name: "Start archiving at local usage",
      exact: true,
    });
    const stop = frigateApp.page.getByRole("slider", {
      name: "Stop archiving at local usage",
      exact: true,
    });
    await frigateApp.page
      .getByRole("button", { name: "Use 60% start / 40% stop", exact: true })
      .click();
    await expect(start).toHaveAttribute("aria-valuenow", "60");
    await expect(stop).toHaveAttribute("aria-valuenow", "40");
    await expect(start).toHaveAttribute("aria-valuetext", "1,800 GiB");
    await expect(stop).toHaveAttribute("aria-valuetext", "1,200 GiB");
    const budget = frigateApp.page.getByLabel("Local recording budget (GiB)", {
      exact: true,
    });
    await budget.fill("4000");
    await expect(start).toHaveAttribute("aria-valuetext", "2,400 GiB");
    await expect(stop).toHaveAttribute("aria-valuetext", "1,600 GiB");
    await budget.fill("3000");
    await start.focus();
    await start.press("Home");
    await expect(start).toHaveAttribute("aria-valuenow", "60");
    await expect(stop).toHaveAttribute("aria-valuenow", "0");
    await frigateApp.page
      .getByRole("button", { name: "Use 60% start / 40% stop", exact: true })
      .click();
    await start.focus();
    for (let step = 0; step < 20; step++) await start.press("ArrowLeft");
    await expect(start).toHaveAttribute("aria-valuenow", "41");
    await frigateApp.page
      .getByRole("button", { name: "Use 60% start / 40% stop", exact: true })
      .click();
    await frigateApp.page
      .getByRole("button", { name: "Save and apply live", exact: true })
      .click();
    await expect(
      frigateApp.page.getByText(/^Saved\. Storage settings/),
    ).toBeVisible();
    expect(settings.local_budget_gib).toBe(3000);
    expect(settings.start_percent).toBe(60);
    expect(settings.drain_percent).toBe(40);
  });

  test("navigation links are named and not nested in buttons", async ({
    frigateApp,
  }) => {
    await frigateApp.goto("/system#storage");
    await expect(
      frigateApp.page.getByRole("heading", {
        name: "One timeline. Two storage tiers.",
      }),
    ).toBeVisible();
    const live = frigateApp.page.getByRole("link", {
      name: "Live",
      exact: true,
    });
    await expect(live).toBeVisible();
    await expect(frigateApp.page.locator("button a[href]")).toHaveCount(0);
  });

  test("retention summary identifies missing legacy policy data instead of inventing it", async ({
    frigateApp,
  }) => {
    const config = structuredClone(BASE_CONFIG);
    for (const record of [
      config.record,
      ...Object.values(config.cameras).map((camera) => camera.record),
    ]) {
      Reflect.deleteProperty(record, "continuous");
      Reflect.deleteProperty(record, "motion");
    }
    await frigateApp.page.route("**/api/config", (route) =>
      route.fulfill({ json: config }),
    );
    await frigateApp.goto("/system#storage");
    await frigateApp.page
      .getByText("Effective video retention", { exact: true })
      .first()
      .click();
    await expect(
      frigateApp.page.getByRole("columnheader", {
        name: "Continuous retention with archive",
      }),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText(/Global continuous: Unknown days/),
    ).toBeVisible();
  });

  test("preserves unrelated concurrent settings when saving", async ({
    frigateApp,
  }) => {
    let settings = structuredClone(STORAGE_TIERS.settings);
    await frigateApp.page.route("**/api/recordings/storage/tiers", (route) =>
      route.fulfill({ json: { ...STORAGE_TIERS, settings } }),
    );
    await frigateApp.page.route("**/api/config/set", async (route) => {
      const body = route.request().postDataJSON();
      expect(body.config_data.storage).toEqual({ local_budget_gib: 2048 });
      expect(body.expected_storage).toEqual({ local_budget_gib: 1024 });
      settings = { ...settings, ...body.config_data.storage };
      await route.fulfill({ json: { success: true } });
    });
    await frigateApp.goto("/system#storage");
    await frigateApp.page
      .getByRole("button", { name: "Storage settings", exact: true })
      .click();
    await frigateApp.page
      .getByLabel("Local recording budget (GiB)", { exact: true })
      .fill("2048");
    settings = { ...settings, retain_days: 180 };
    await frigateApp.page
      .getByRole("button", { name: "Save and apply live", exact: true })
      .click();
    await expect(
      frigateApp.page.getByText(/^Saved\. Storage settings/),
    ).toBeVisible();
    expect(settings.retain_days).toBe(180);
  });

  test("rejects concurrent changes to an edited field", async ({
    frigateApp,
  }) => {
    let settings = structuredClone(STORAGE_TIERS.settings);
    let saves = 0;
    await frigateApp.page.route("**/api/recordings/storage/tiers", (route) =>
      route.fulfill({ json: { ...STORAGE_TIERS, settings } }),
    );
    await frigateApp.page.route("**/api/config/set", async (route) => {
      saves += 1;
      await route.fulfill({ json: { success: true } });
    });
    await frigateApp.goto("/system#storage");
    await frigateApp.page
      .getByRole("button", { name: "Storage settings", exact: true })
      .click();
    await frigateApp.page
      .getByLabel("Local recording budget (GiB)", { exact: true })
      .fill("2048");
    settings = { ...settings, local_budget_gib: 4096 };
    await frigateApp.page
      .getByRole("button", { name: "Save and apply live", exact: true })
      .click();
    await expect(
      frigateApp.page.getByText(/An edited storage setting changed elsewhere/),
    ).toBeVisible();
    expect(saves).toBe(0);
    await expect(
      frigateApp.page.getByLabel("Local recording budget (GiB)", {
        exact: true,
      }),
    ).toHaveValue("2048");
  });

  test("shows atomic server conflicts without discarding the draft", async ({
    frigateApp,
  }) => {
    await frigateApp.page.route("**/api/config/set", (route) =>
      route.fulfill({
        status: 409,
        json: { success: false, code: "storage_settings_conflict" },
      }),
    );
    await frigateApp.goto("/system#storage");
    await frigateApp.page
      .getByRole("button", { name: "Storage settings", exact: true })
      .click();
    await frigateApp.page
      .getByLabel("Local recording budget (GiB)", { exact: true })
      .fill("2048");
    await frigateApp.page
      .getByRole("button", { name: "Save and apply live", exact: true })
      .click();
    await expect(
      frigateApp.page.getByText(/An edited storage setting changed elsewhere/),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByLabel("Local recording budget (GiB)", {
        exact: true,
      }),
    ).toHaveValue("2048");
  });

  test("storage overview survives a legacy metrics failure", async ({
    frigateApp,
  }) => {
    await frigateApp.page.route("**/api/recordings/storage", (route) =>
      route.fulfill({ status: 500, json: { message: "synthetic failure" } }),
    );
    await frigateApp.goto("/system#storage");
    await expect(
      frigateApp.page.getByRole("heading", {
        name: "One timeline. Two storage tiers.",
      }),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByRole("button", { name: "Retry", exact: true }),
    ).toBeVisible();
  });

  test("pauses storage polling after switching system tabs", async ({
    frigateApp,
  }) => {
    await frigateApp.page.clock.install();
    let requests = 0;
    await frigateApp.page.route("**/api/recordings/storage/tiers", (route) => {
      requests += 1;
      return route.fulfill({ json: STORAGE_TIERS });
    });
    await frigateApp.goto("/system#storage");
    await expect(
      frigateApp.page.getByRole("heading", {
        name: "One timeline. Two storage tiers.",
      }),
    ).toBeVisible();
    await frigateApp.page.evaluate(() => {
      window.location.hash = "general";
    });
    await expect(
      frigateApp.page.getByRole("heading", {
        name: "One timeline. Two storage tiers.",
      }),
    ).not.toBeVisible();
    const before = requests;
    await frigateApp.page.clock.runFor(12_000);
    expect(requests).toBe(before);
  });
});

test.describe("Tiered recording storage @mobile", () => {
  test("lists parallel jobs and labels aggregate transfer rates", async ({
    frigateApp,
  }) => {
    const first = { ...transfer.current_file!, id: "first", phase: "copying" };
    const second = {
      ...first,
      id: "second",
      name: "parallel-second.mp4",
      phase: "verification",
    };
    await frigateApp.page.route("**/api/recordings/storage/tiers", (route) =>
      route.fulfill({
        json: {
          ...STORAGE_TIERS,
          settings: { ...STORAGE_TIERS.settings, parallel_transfers: 2 },
          health: {
            ...STORAGE_TIERS.health,
            state: "moving",
            transfer: {
              ...transfer,
              current_file: first,
              active_files: [first, second],
              parallel_transfers: 2,
              aggregate_rates: true,
              copy_rate_bytes_per_second: 125_000_000,
              verification_rate_bytes_per_second: 200_000_000,
            },
          },
        },
      }),
    );
    await frigateApp.goto("/system#storage");
    await frigateApp.page
      .getByText("Individual file activity", { exact: true })
      .click();
    await expect(
      frigateApp.page.getByText("2 active archive transfers", { exact: true }),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText(/parallel-second.mp4/),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText(
        "Aggregate copy throughput: 125 MB/s (current parallel batch average).",
      ),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText(
        "Aggregate archive readback throughput: 200 MB/s (current parallel batch average).",
      ),
    ).toBeVisible();
  });

  test("shows live copy and readback speeds separately from verified throughput", async ({
    frigateApp,
  }) => {
    await frigateApp.page.route("**/api/recordings/storage/tiers", (route) =>
      route.fulfill({
        json: {
          ...STORAGE_TIERS,
          health: {
            ...STORAGE_TIERS.health,
            state: "moving",
            transfer: {
              ...transfer,
              copy_rate_bytes_per_second: 125_000_000,
              verification_rate_bytes_per_second: 200_000_000,
              rate_bytes_per_second: 60_000_000,
              eta_seconds: 120,
            },
          },
        },
      }),
    );
    await frigateApp.goto("/system#storage");
    await expect(
      frigateApp.page.getByText(
        "Copy speed: 125 MB/s (active-copy batch average).",
      ),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText(
        "Archive readback speed: 200 MB/s (active-verification batch average).",
      ),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText(/Verified archival throughput: 60 MB\/s/),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText(/Estimated drain time: about 2 min/),
    ).toBeVisible();
  });

  test("separates readback from batch completion and shows shared physical capacity", async ({
    frigateApp,
  }) => {
    await frigateApp.page.route("**/api/recordings/storage/tiers", (route) =>
      route.fulfill({
        json: {
          ...STORAGE_TIERS,
          health: {
            ...STORAGE_TIERS.health,
            state: "moving",
            capacity: { local: capacity, archive: capacity },
            transfer,
          },
        },
      }),
    );
    await frigateApp.goto("/system#storage");
    await expect(
      frigateApp.page.getByText("Reading back and verifying the archive copy"),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText(
        /Triggered early by the estimated two-hour local free-space reserve/,
      ),
    ).toBeVisible();
    await frigateApp.page
      .getByText("Individual file activity", { exact: true })
      .click();
    await expect(
      frigateApp.page.getByRole("progressbar", {
        name: "Current file readback",
      }),
    ).toHaveAttribute("value", "1000");
    await expect(
      frigateApp.page.getByRole("progressbar", { name: "Initial batch plan" }),
    ).toHaveAttribute("value", "0");
    await expect(
      frigateApp.page.getByText(/Committed after verification: 0 B, 0 files/),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText(/Additional net incoming: 1,000 B/),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText(/These tiers share a mounted filesystem/),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText("Effective incoming headroom", { exact: true }),
    ).toHaveCount(2);
    await expect(
      frigateApp.page.getByText(/Estimated drain time:/),
    ).toHaveCount(0);
  });

  test("shows buffer idle and explicit unavailable capacity without a false ETA", async ({
    frigateApp,
  }) => {
    await frigateApp.page.route("**/api/recordings/storage/tiers", (route) =>
      route.fulfill({
        json: {
          ...STORAGE_TIERS,
          health: {
            ...STORAGE_TIERS.health,
            capacity: {
              local: capacity,
              archive: {
                ...capacity,
                state: "offline",
                usage_bytes: null,
                effective_headroom_bytes: null,
              },
            },
            transfer: {
              ...transfer,
              active: false,
              phase: "buffering",
              current_file: null,
              buffer_seconds: 120,
            },
          },
        },
      }),
    );
    await frigateApp.goto("/system#storage");
    await expect(
      frigateApp.page.getByText("Idle: buffering new recordings locally", {
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText(/Filesystem sample: Offline/),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText(
        /Estimated time to the start threshold: about 2 min/,
      ),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText(/Estimated drain time:/),
    ).toHaveCount(0);
  });

  test("does not finish a drain when ingest overtakes the completed initial plan", async ({
    frigateApp,
  }) => {
    await frigateApp.page.route("**/api/recordings/storage/tiers", (route) =>
      route.fulfill({
        json: {
          ...STORAGE_TIERS,
          health: {
            ...STORAGE_TIERS.health,
            state: "moving",
            transfer: {
              ...transfer,
              phase: "waiting",
              current_file: null,
              verified_files: 2,
              verified_bytes: 4000,
              rate_bytes_per_second: 256,
              ingest_bytes_per_second: 512,
              cannot_outpace_ingest: true,
            },
          },
        },
      }),
    );
    await frigateApp.goto("/system#storage");
    await expect(
      frigateApp.page.getByRole("progressbar", { name: "Initial batch plan" }),
    ).toHaveAttribute("value", "2");
    await expect(
      frigateApp.page.getByText(/remaining drain 1,000 B/),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText(
        /The measured transfer rate is not exceeding ingest/,
      ),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText(/Estimated drain time:/),
    ).toHaveCount(0);
  });

  test("shows each camera's local and NAS usage and share of each tier", async ({
    frigateApp,
  }) => {
    await frigateApp.page.route("**/api/recordings/storage", (route) =>
      route.fulfill({
        json: {
          front_door: {
            friendly_name: "Front Door",
            usage: 256 * 1024,
            bandwidth: 1024,
            usage_percent: 0,
          },
        },
      }),
    );
    await frigateApp.goto("/system#storage");
    await expect(
      frigateApp.page.getByTestId("camera-storage-table"),
    ).toHaveCount(1);
    await expect(
      frigateApp.page.getByRole("columnheader", {
        name: "Local filesystem share",
      }),
    ).toBeVisible();
    const front = frigateApp.page.getByTestId("camera-storage-front_door");
    const back = frigateApp.page.getByTestId("camera-storage-backyard");
    const garage = frigateApp.page.getByTestId("camera-storage-garage");
    await expect(front.getByText("Front Door", { exact: true })).toBeVisible();
    await expect(front.getByText("256.00 GiB", { exact: true })).toBeVisible();
    await expect(front.getByText("2.00 TiB", { exact: true })).toBeVisible();
    await expect(
      front.getByText("1.00 GiB / hour", { exact: true }),
    ).toBeVisible();
    await expect(back.getByText("160.00 GiB", { exact: true })).toBeVisible();
    await expect(garage.getByText("96.00 GiB", { exact: true })).toBeVisible();
    await expect(
      front.getByRole("meter", {
        name: "Front Door: share of known Local recordings recording usage",
      }),
    ).toHaveAttribute("aria-valuenow", "50");
    await expect(
      front.getByRole("meter", {
        name: "Front Door: share of known NAS archive recording usage",
      }),
    ).toHaveAttribute("aria-valuenow", "50");
  });

  test("keeps duplicate display names distinct and calls out incomplete sizes", async ({
    frigateApp,
  }) => {
    await frigateApp.page.route("**/api/recordings/storage/tiers", (route) =>
      route.fulfill({
        json: {
          ...STORAGE_TIERS,
          local: { usage_mib: 4096, recordings: 5 },
          archive: { usage_mib: 0, recordings: 0 },
          cameras: {
            front: {
              friendly_name: "Driveway",
              tiers: {
                local: {
                  usage_mib: 1024,
                  recordings: 3,
                  unknown_size_recordings: 2,
                },
              },
            },
            front_old: {
              friendly_name: "Driveway",
              tiers: { local: { usage_mib: 3072, recordings: 2 } },
            },
          },
        },
      }),
    );
    await frigateApp.goto("/system#storage");
    const front = frigateApp.page.getByTestId("camera-storage-front");
    const old = frigateApp.page.getByTestId("camera-storage-front_old");
    await expect(front.getByText("front", { exact: true })).toBeVisible();
    await expect(old.getByText("front_old", { exact: true })).toBeVisible();
    await expect(front.getByText("1.00 GiB", { exact: true })).toBeVisible();
    await expect(old.getByText("3.00 GiB", { exact: true })).toBeVisible();
    await expect(
      front.getByText(
        "Size is unknown for 2 recording segments; shown usage is incomplete.",
      ),
    ).toBeVisible();
    await expect(front.getByRole("meter").first()).toHaveAttribute(
      "aria-valuenow",
      "25",
    );
    await expect(old.getByRole("meter").first()).toHaveAttribute(
      "aria-valuenow",
      "75",
    );
  });

  test("does not present missing per-camera metrics as zero storage", async ({
    frigateApp,
  }) => {
    const { cameras: _cameras, ...withoutCameras } = STORAGE_TIERS;
    await frigateApp.page.route("**/api/recordings/storage/tiers", (route) =>
      route.fulfill({ json: withoutCameras }),
    );
    await frigateApp.goto("/system#storage");
    await expect(
      frigateApp.page.getByText(
        "Per-camera storage information is not available from this backend.",
      ),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText(
        "No cameras or retained recordings are available.",
      ),
    ).toHaveCount(0);
  });

  test("shows both tiers and switches dashboard appearance", async ({
    frigateApp,
  }) => {
    await frigateApp.goto("/system#storage");
    await expect(
      frigateApp.page.getByRole("heading", {
        name: "One timeline. Two storage tiers.",
      }),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByRole("heading", { name: "NAS archive" }),
    ).toBeVisible();
    const dashboard = frigateApp.page.locator("[data-storage-theme]");
    await frigateApp.page
      .getByRole("button", { name: "Compact", exact: true })
      .click();
    await expect(dashboard).toHaveAttribute(
      "data-storage-theme",
      "control-room",
    );
    await expect(dashboard).not.toHaveClass(/\bdark\b/);
    await frigateApp.page
      .getByRole("button", { name: "Comfortable", exact: true })
      .click();
    await expect(dashboard).toHaveAttribute("data-storage-theme", "clean");
    await expect(
      frigateApp.page.getByRole("progressbar", {
        name: "Local recording budget usage",
      }),
    ).toHaveAttribute("aria-valuenow", "50");
  });

  test("saves a budget live without requesting a restart", async ({
    frigateApp,
  }) => {
    let settings = structuredClone(STORAGE_TIERS.settings);
    await frigateApp.page.route("**/api/recordings/storage/tiers", (route) =>
      route.fulfill({ json: { ...STORAGE_TIERS, settings } }),
    );
    await frigateApp.page.route("**/api/config/set", async (route) => {
      const body = route.request().postDataJSON();
      expect(body.requires_restart).toBe(0);
      expect(body.update_topic).toBe("config/storage");
      expect(body.config_data.storage.local_budget_gib).toBe(2048);
      expect(body.config_data.storage).toEqual({ local_budget_gib: 2048 });
      expect(body.expected_storage).toEqual({ local_budget_gib: 1024 });
      settings = { ...settings, ...body.config_data.storage };
      await route.fulfill({ json: { success: true } });
    });
    await frigateApp.goto("/system#storage");
    await frigateApp.page
      .getByRole("button", { name: "Storage settings", exact: true })
      .click();
    await frigateApp.page
      .getByLabel("Local recording budget (GiB)", { exact: true })
      .fill("2048");
    await expect(
      frigateApp.page.getByRole("progressbar", {
        name: "Local recording budget usage",
      }),
    ).toHaveAttribute("aria-valuenow", "50");
    await frigateApp.page
      .getByRole("button", { name: "Save and apply live", exact: true })
      .click();
    await expect(
      frigateApp.page.getByText(
        "Saved. Storage settings will apply live; no Frigate restart is needed.",
      ),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByRole("progressbar", {
        name: "Local recording budget usage",
      }),
    ).toHaveAttribute("aria-valuenow", "25");
  });

  test("requires confirmation before shortening retention", async ({
    frigateApp,
  }) => {
    await frigateApp.goto("/system#storage");
    await frigateApp.page
      .getByRole("button", { name: "Storage settings", exact: true })
      .click();
    await frigateApp.page
      .getByLabel("Archive retention (days)", { exact: true })
      .fill("30");
    const save = frigateApp.page.getByRole("button", {
      name: "Save and apply live",
      exact: true,
    });
    await expect(save).toBeDisabled();
    await frigateApp.page
      .getByRole("checkbox", {
        name: "I understand that reducing age or capacity limits, or enabling oldest-first eviction, can permanently delete unprotected recordings before minimum retention.",
      })
      .check();
    await expect(save).toBeEnabled();
    await expect(
      frigateApp.page.getByLabel("Archive directory inside the container", {
        exact: true,
      }),
    ).toBeDisabled();
  });

  test("reports an archive outage without hiding local storage", async ({
    frigateApp,
  }) => {
    await frigateApp.page.route("**/api/recordings/storage/tiers", (route) =>
      route.fulfill({
        json: {
          ...STORAGE_TIERS,
          health: {
            ...STORAGE_TIERS.health,
            state: "blocked",
            error: "archive_unavailable",
            updated_at: 1700000000,
          },
        },
      }),
    );
    await frigateApp.goto("/system#storage");
    await expect(
      frigateApp.page.getByRole("alert").filter({
        hasText:
          "Incoming archive transfers are paused or the archive is not responding.",
      }),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByRole("heading", {
        name: "Local recordings",
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText(/available on archive filesystem/),
    ).toHaveCount(0);
    await expect(
      frigateApp.page
        .getByTestId("camera-storage-front_door")
        .getByText("2.00 TiB", { exact: true }),
    ).toBeVisible();
  });

  test("keeps archive identity locked while saved settings await acknowledgment", async ({
    frigateApp,
  }) => {
    await frigateApp.page.route("**/api/recordings/storage/tiers", (route) =>
      route.fulfill({
        json: {
          ...STORAGE_TIERS,
          settings: { ...STORAGE_TIERS.settings, enabled: false },
          archive: { usage_mib: 0, recordings: 0 },
          health: {
            ...STORAGE_TIERS.health,
            settings_pending: true,
          },
        },
      }),
    );
    await frigateApp.goto("/system#storage");
    await expect(
      frigateApp.page.getByRole("status").filter({
        hasText: "Settings are saved and waiting for the archive worker",
      }),
    ).toBeVisible();
    await frigateApp.page
      .getByRole("button", { name: "Storage settings", exact: true })
      .click();
    await expect(
      frigateApp.page.getByLabel("Archive directory inside the container", {
        exact: true,
      }),
    ).toBeDisabled();
    await expect(
      frigateApp.page.getByLabel("Archive identity marker", { exact: true }),
    ).toBeDisabled();
    await expect(
      frigateApp.page.getByLabel("Local recording budget (GiB)", {
        exact: true,
      }),
    ).toBeEnabled();
  });
});
