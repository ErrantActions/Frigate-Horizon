import { test, expect } from "../../fixtures/frigate-test";
import { STORAGE_TIERS } from "../../fixtures/mock-data/storage";
import { viewerProfile } from "../../fixtures/mock-data/profile";

const mount = {
  container_path: "/media/frigate/archive",
  mounted: false,
  filesystem: "zfs",
  source: null,
  configured_connections: null,
  access_verified: false,
};

test.describe("NFS storage setup", () => {
  test.beforeEach(async ({ frigateApp }) => {
    await frigateApp.page.route("**/api/storage/nfs", (route) =>
      route.fulfill({
        json: {
          mounts: {
            archive: mount,
            recordings: {
              ...mount,
              container_path: "/media/frigate/recordings",
            },
          },
          archive_path: "/media/frigate/archive",
          mount_id: "frigate-archive",
          local_budget_gib: 1024,
          runtime_uid: 1000,
          runtime_gid: 1000,
          automatic_remounts: false,
          recording_enabled: false,
        },
      }),
    );
    await frigateApp.page.route("**/api/recordings/storage/tiers", (route) =>
      route.fulfill({
        json: {
          ...STORAGE_TIERS,
          settings: {
            ...STORAGE_TIERS.settings,
            adaptive_transfers: false,
            parallel_transfers: 2,
          },
        },
      }),
    );
  });

  test("local-first plan is explicit, never mounts, and stale output clears", async ({
    frigateApp,
  }) => {
    const page = frigateApp.page;
    await page.route("**/api/storage/nfs/plan", async (route) => {
      expect(route.request().postDataJSON()).toEqual({
        shares: [
          {
            role: "archive",
            server: "nas.example",
            export: "/mnt/nvr",
            version: "4.1",
            connections: 8,
          },
        ],
        confirm_direct_recordings: false,
      });
      await route.fulfill({
        json: {
          volume_definitions_yaml:
            "volumes:\n  frigate_nfs_archive_test:\n    driver: local\n    driver_opts:\n      o: addr=nas.example,hard,rw,nconnect=8\n",
          service_volumes_yaml:
            "volumes:\n- source: frigate_nfs_archive_test\n  target: /media/frigate/archive\n",
          frigate_storage_yaml:
            "storage:\n  enabled: false\n  emergency_delete: false\n",
          local_first: true,
          enables_recording: false,
          archive_marker: {
            filename: ".frigate-archive",
            contents: "frigate-archive",
          },
        },
      });
    });
    await frigateApp.goto("/settings?page=recordingNfs");
    await expect(
      page.getByRole("heading", { name: "Remote NFS storage" }).last(),
    ).toBeVisible();
    await page.getByLabel("NFS server IP or hostname").fill("nas.example");
    await page.getByLabel("Dedicated export path").fill("/mnt/nvr");
    await page
      .getByRole("button", { name: "Generate TrueNAS App additions" })
      .click();
    await expect(
      page.getByText("Plan generated only.", { exact: false }),
    ).toBeVisible();
    await expect(
      page.getByRole("textbox", { name: "Add to top-level App volumes" }),
    ).toHaveValue(/nconnect=8/);
    await expect(
      page.getByRole("textbox", {
        name: "Merge into Frigate storage settings after reviewing the local buffer budget",
      }),
    ).toHaveValue(/enabled: false/);
    await page.getByLabel("NFS server IP or hostname").fill("other.example");
    await expect(
      page.getByRole("heading", { name: "TrueNAS App setup plan" }),
    ).toHaveCount(0);
  });

  test("direct recording requires an explicit outage and migration acknowledgment", async ({
    frigateApp,
  }) => {
    const page = frigateApp.page;
    await frigateApp.goto("/settings?page=recordingNfs");
    await page.getByLabel("Share role").selectOption("recordings");
    await expect(
      page.getByText("Direct-to-NFS has no automatic local fallback.", {
        exact: false,
      }),
    ).toBeVisible();
    await page.getByLabel("NFS server IP or hostname").fill("nas.example");
    await page.getByLabel("Dedicated export path").fill("/mnt/nvr");
    const generate = page.getByRole("button", {
      name: "Generate TrueNAS App additions",
    });
    await expect(generate).toBeDisabled();
    await page
      .getByLabel(
        "I understand the outage and migration risks of direct NFS recording.",
      )
      .check();
    await expect(generate).toBeEnabled();
  });

  test("reachability failures are visible and never called export success", async ({
    frigateApp,
  }) => {
    const page = frigateApp.page;
    await page.route("**/api/storage/nfs/probe", (route) =>
      route.fulfill({
        json: {
          reachable: false,
          port: 2049,
          export_verified: false,
          error: "timeout",
        },
      }),
    );
    await frigateApp.goto("/settings?page=recordingNfs");
    await page.getByLabel("NFS server IP or hostname").fill("nas.example");
    await page.getByRole("button", { name: "Test NFS port" }).click();
    await expect(page.getByText("The NFS port check timed out.")).toBeVisible();
  });

  test("saving adaptive workers never enables archiving or changes mounts", async ({
    frigateApp,
  }) => {
    const page = frigateApp.page;
    let saved = false;
    await page.route("**/api/config/set", async (route) => {
      expect(route.request().postDataJSON()).toEqual({
        requires_restart: 0,
        update_topic: "config/storage",
        config_data: {
          storage: { adaptive_transfers: true, parallel_transfers: 8 },
        },
        expected_storage: { adaptive_transfers: false, parallel_transfers: 2 },
      });
      saved = true;
      await route.fulfill({ json: { success: true } });
    });
    await frigateApp.goto("/settings?page=recordingNfs");
    await page
      .getByLabel("Automatically tune background transfer concurrency")
      .check();
    await page.getByLabel("Maximum copy/readback workers").fill("8");
    await page
      .getByRole("button", { name: "Save transfer-worker settings only" })
      .click();
    await expect(
      page.getByText("Transfer settings saved.", { exact: false }),
    ).toBeVisible();
    expect(saved).toBe(true);
  });

  test("viewer cannot open storage setup or issue storage requests", async ({
    frigateApp,
  }) => {
    let requested = false;
    frigateApp.page.on("request", (request) => {
      if (request.url().includes("/storage/nfs")) requested = true;
    });
    await frigateApp.api.install({ profile: viewerProfile() });
    await frigateApp.goto("/settings?page=recordingNfs");
    await expect(
      frigateApp.page.getByRole("heading", { name: "Remote NFS storage" }),
    ).toHaveCount(0);
    expect(requested).toBe(false);
  });
});
