import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Page } from "@playwright/test";
import { test, expect } from "../../fixtures/frigate-test";
import { configFactory } from "../../fixtures/mock-data/config";
import type { ImageResponse } from "../../../src/types/onvif";

const expectedProfileErrorTest = test.extend({
  expectedErrors: [
    [
      /^502\b.*\/api\/onvif\/cameras\/front_door\/presets(?:\?|$)/,
      /^Failed to load resource: the server responded with a status of 502\b/,
      /^failed: net::ERR_(?:ABORTED|FAILED|TIMED_OUT).*\/api\/onvif\/cameras\/front_door\/presets(?:\?|$)/,
      /^Failed to load resource: net::ERR_(?:ABORTED|FAILED|TIMED_OUT)/,
    ],
    { option: true },
  ],
});

const SETTINGS = "/settings?page=cameraOnvif&camera=front_door";
const schema = JSON.parse(
  readFileSync(
    resolve(
      dirname(fileURLToPath(import.meta.url)),
      "../../fixtures/mock-data/config-schema.json",
    ),
    "utf-8",
  ),
);
const image = (): ImageResponse => ({
  profiles: [{ token: "main", name: "Main", video_source_token: "sensor" }],
  profile_token: "main",
  state: "ready",
  imaging: {
    video_source_token: "sensor",
    controls: [
      {
        key: "brightness",
        value: 50,
        limits: { minimum: 10, maximum: 90 },
        choices: [],
        available: true,
      },
      {
        key: "contrast",
        value: 40,
        limits: null,
        choices: [],
        available: false,
      },
    ],
  },
});

async function install(page: Page) {
  const config = configFactory({
    cameras: {
      front_door: {
        onvif: { host: "camera.invalid", user: "fixture", password: "***" },
      },
      backyard: { onvif: { host: "other.invalid" } },
    },
  });
  await page.route("**/api/config", (route) => route.fulfill({ json: config }));
  await page.route("**/api/config/raw_paths", (route) =>
    route.fulfill({ json: config }),
  );
  await page.route("**/api/config/schema.json", (route) =>
    route.fulfill({ json: schema }),
  );
  await page.route("**/api/onvif/cameras/*/ownership", (route) =>
    route.fulfill({
      json: {
        phase: "idle",
        blocked: false,
        manual_remaining_seconds: 0,
        owned_fields: [],
        profile_token: null,
      },
    }),
  );
}

test.describe("ONVIF device controls @mobile", () => {
  test("firmware requires a selected file, exact operation name, and explicit consent", async ({
    frigateApp,
  }) => {
    await install(frigateApp.page);
    let submitted = 0;
    await frigateApp.page.route(
      "**/api/onvif/cameras/front_door/administration",
      (route) =>
        route.fulfill({
          json: {
            revision: "a".repeat(64),
            complete: false,
            services: [
              {
                namespace: "fixture-device",
                service: "devicemgmt",
                advertised: true,
                binding: "available",
                operations: [
                  {
                    name: "UpgradeSystemFirmware",
                    effect: "mutating",
                    typed_confirmation: true,
                    schema: { type: "object", properties: {} },
                    readiness: "managed_workflow",
                    device_support: "unverified",
                    binary_input: true,
                    binary_output: false,
                  },
                ],
              },
            ],
          },
        }),
    );
    await frigateApp.page.route(
      "**/api/onvif/cameras/front_door/administration/firmware",
      (route) => {
        submitted++;
        expect(route.request().headers()["content-type"]).toContain(
          "multipart/form-data",
        );
        return route.fulfill({
          json: { state: "acknowledged_unverified", result: null },
        });
      },
    );
    await frigateApp.goto(SETTINGS);
    await frigateApp.page
      .getByRole("button", {
        name: "Read advertised service and operation catalog",
      })
      .click();
    const submit = frigateApp.page.getByRole("button", {
      name: "Submit confirmed firmware once",
      exact: true,
    });
    await expect(submit).toBeDisabled();
    await frigateApp.page
      .getByLabel("Camera firmware transfer method")
      .selectOption("UpgradeSystemFirmware");
    await frigateApp.page
      .getByLabel("Manufacturer firmware file")
      .setInputFiles({
        name: "fixture.bin",
        mimeType: "application/octet-stream",
        buffer: Buffer.from("synthetic-firmware-no-camera-writes"),
      });
    await frigateApp.page
      .getByLabel("Type the exact selected firmware operation name")
      .fill("wrong");
    await frigateApp.page
      .getByRole("checkbox", { name: /verified the firmware model/ })
      .check();
    await expect(submit).toBeDisabled();
    expect(submitted).toBe(0);
    await frigateApp.page
      .getByLabel("Type the exact selected firmware operation name")
      .fill("UpgradeSystemFirmware");
    await submit.click();
    await expect(
      frigateApp.page.getByText(/acknowledged the transfer, not installation/),
    ).toBeVisible();
    await expect(submit).toBeDisabled();
    await expect(
      frigateApp.page.getByLabel(
        "Type the exact selected firmware operation name",
      ),
    ).toHaveValue("");
    expect(submitted).toBe(1);
  });

  test("private backup and restore require separate explicit consent", async ({
    frigateApp,
  }) => {
    await install(frigateApp.page);
    let downloads = 0;
    let restores = 0;
    await frigateApp.page.route(
      "**/api/onvif/cameras/front_door/administration",
      (route) =>
        route.fulfill({
          json: {
            revision: "a".repeat(64),
            complete: false,
            services: [
              {
                namespace: "fixture-device",
                service: "devicemgmt",
                advertised: true,
                binding: "available",
                operations: ["GetSystemBackup", "RestoreSystem"].map(
                  (name) => ({
                    name,
                    effect: name === "GetSystemBackup" ? "read" : "mutating",
                    typed_confirmation: name === "RestoreSystem",
                    schema: { type: "object", properties: {} },
                    readiness: "file_transfer_required",
                    device_support: "unverified",
                    binary_input: name === "RestoreSystem",
                    binary_output: name === "GetSystemBackup",
                  }),
                ),
              },
            ],
          },
        }),
    );
    await frigateApp.page.route(
      "**/api/onvif/cameras/front_door/administration/backup",
      async (route) => {
        downloads++;
        expect(route.request().postDataJSON()).toEqual({
          catalog_revision: "a".repeat(64),
          confirm_sensitive_download: true,
        });
        await route.fulfill({
          contentType: "application/zip",
          body: Buffer.from("synthetic-backup-download"),
        });
      },
    );
    await frigateApp.page.route(
      "**/api/onvif/cameras/front_door/administration/restore",
      async (route) => {
        restores++;
        expect(route.request().headers()["content-type"]).toContain(
          "multipart/form-data",
        );
        await route.fulfill({
          json: { state: "acknowledged_unverified", result: null },
        });
      },
    );
    await frigateApp.goto(SETTINGS);
    await frigateApp.page
      .getByRole("button", {
        name: "Read advertised service and operation catalog",
      })
      .click();
    const download = frigateApp.page.getByRole("button", {
      name: "Download camera backup",
      exact: true,
    });
    await expect(download).toBeDisabled();
    expect(downloads).toBe(0);
    expect(restores).toBe(0);
    await frigateApp.page
      .getByRole("checkbox", { name: /authorize downloading this camera/ })
      .check();
    const completedDownload = frigateApp.page.waitForEvent("download");
    await download.click();
    expect((await completedDownload).suggestedFilename()).toBe(
      "camera-backup.zip",
    );
    expect(downloads).toBe(1);
    await expect(download).toBeDisabled();
    await frigateApp.page
      .getByLabel("Horizon camera backup ZIP")
      .setInputFiles({
        name: "fixture-backup.zip",
        mimeType: "application/zip",
        buffer: Buffer.from("synthetic-upload-only"),
      });
    const restore = frigateApp.page.getByRole("button", {
      name: "Restore camera backup once",
      exact: true,
    });
    await expect(restore).toBeDisabled();
    await frigateApp.page
      .getByRole("checkbox", { name: /authorize one disruptive restore/ })
      .check();
    await expect(restore).toBeDisabled();
    await frigateApp.page
      .getByLabel("Type RestoreSystem exactly to confirm")
      .fill("RestoreSystem");
    await restore.click();
    await expect.poll(() => restores).toBe(1);
    await expect(restore).toBeDisabled();
    await expect(
      frigateApp.page.getByText(/acknowledged this operation, but its outcome/),
    ).toBeVisible();
  });

  test("PTZ profile reads are explicit and an empty fixed-camera result stops loading", async ({
    frigateApp,
  }) => {
    await install(frigateApp.page);
    let reads = 0;
    await frigateApp.page.route(
      "**/api/onvif/cameras/front_door/presets",
      async (route) => {
        reads++;
        await route.fulfill({
          json: { state: "unsupported", profiles: [], presets: [] },
        });
      },
    );
    await frigateApp.goto(SETTINGS);
    const load = frigateApp.page.getByRole("button", {
      name: "Read saved camera PTZ profiles",
      exact: true,
    });
    await expect(load).toBeVisible({ timeout: 15000 });
    expect(reads).toBe(0);
    await expect(frigateApp.page.getByText("Loading profiles...")).toHaveCount(
      0,
    );
    await load.click();
    await expect(
      frigateApp.page.getByText(
        "The camera did not report a PTZ-capable media profile.",
        { exact: false },
      ),
    ).toBeVisible();
    await expect(load).toBeEnabled();
    await expect(frigateApp.page.getByText("Loading profiles...")).toHaveCount(
      0,
    );
    await frigateApp.page.clock.install();
    await frigateApp.page.clock.fastForward(15000);
    expect(reads).toBe(1);
  });

  expectedProfileErrorTest(
    "a failed PTZ profile read shows an error and retries only on user action",
    async ({ frigateApp }) => {
      await install(frigateApp.page);
      let reads = 0;
      await frigateApp.page.route(
        "**/api/onvif/cameras/front_door/presets",
        async (route) => {
          reads++;
          await route.fulfill(
            reads === 1
              ? { status: 502, json: { detail: "Camera service unavailable" } }
              : {
                  json: {
                    profiles: [{ token: "ptz", name: "PTZ Main" }],
                    presets: [],
                  },
                },
          );
        },
      );
      await frigateApp.goto(SETTINGS);
      const load = frigateApp.page.getByRole("button", {
        name: "Read saved camera PTZ profiles",
        exact: true,
      });
      await load.click();
      await expect(frigateApp.page.getByRole("alert")).toContainText(
        "The camera service is unavailable",
      );
      await expect(load).toBeEnabled();
      await expect(
        frigateApp.page.getByText("Loading profiles..."),
      ).toHaveCount(0);
      await frigateApp.page.clock.install();
      await frigateApp.page.clock.fastForward(15000);
      expect(reads).toBe(1);
      await load.click();
      await expect(frigateApp.page.getByRole("alert")).toHaveCount(0);
      expect(reads).toBe(2);
      await frigateApp.page
        .getByLabel("ONVIF profile", { exact: true })
        .click();
      await expect(
        frigateApp.page.getByRole("option", { name: "PTZ Main (ptz)" }),
      ).toBeVisible();
    },
  );

  expectedProfileErrorTest(
    "a hung PTZ profile read times out instead of leaving the selector loading forever",
    async ({ frigateApp }) => {
      test.setTimeout(65000);
      await install(frigateApp.page);
      let reads = 0;
      await frigateApp.page.route(
        "**/api/onvif/cameras/front_door/presets",
        async () => {
          reads++;
        },
      );
      await frigateApp.goto(SETTINGS);
      const load = frigateApp.page.getByRole("button", {
        name: "Read saved camera PTZ profiles",
        exact: true,
      });
      await load.click();
      await expect(
        frigateApp.page.getByText("Loading profiles..."),
      ).toBeVisible();
      await expect(frigateApp.page.getByRole("alert")).toContainText(
        "The camera service is unavailable",
        { timeout: 50000 },
      );
      await expect(load).toBeEnabled();
      await expect(
        frigateApp.page.getByText("Loading profiles..."),
      ).toHaveCount(0);
      expect(reads).toBe(1);
    },
  );

  test("new device panels require explicit reads and consent for temporary image writes", async ({
    frigateApp,
  }) => {
    await install(frigateApp.page);
    const requests: string[] = [];
    let writes = 0;
    await frigateApp.page.route(
      "**/api/onvif/cameras/front_door/imaging*",
      async (route) => {
        requests.push(route.request().method());
        const response = image();
        if (route.request().method() === "PUT") {
          writes++;
          const body = route.request().postDataJSON();
          expect(body).toMatchObject({
            profile_token: "main",
            changes: { brightness: 60 },
            expected_values: { brightness: 50 },
            confirm_device_change: true,
            force_persistence: false,
            override_seconds: 900,
          });

          expect(body).not.toHaveProperty("password");
          expect(body).not.toHaveProperty("host");
          response.imaging!.controls[0].value = 60;
        }
        await route.fulfill({ json: response });
      },
    );
    await frigateApp.goto(SETTINGS);
    const connect = frigateApp.page.getByRole("button", {
      name: "Read camera image controls",
      exact: true,
    });
    await expect(connect).toBeVisible();
    expect(requests).toEqual([]);
    await connect.click();
    const brightness = frigateApp.page.getByLabel("Brightness", {
      exact: true,
    });
    await expect(brightness).toHaveValue("50");
    await expect(brightness).toHaveAttribute("min", "10");
    await expect(brightness).toHaveAttribute("max", "90");
    await expect(
      frigateApp.page.getByLabel("Contrast", { exact: true }),
    ).toBeDisabled();
    await brightness.fill("60");
    const apply = frigateApp.page.getByRole("button", {
      name: "Apply confirmed device changes",
      exact: true,
    });
    await expect(apply).toBeDisabled();
    await frigateApp.page
      .getByLabel("I understand this changes the physical camera image", {
        exact: false,
      })
      .check();
    await apply.click();
    await expect(brightness).toHaveValue("60");
    expect(writes).toBe(1);
  });

  test("requires a separate physical-source choice for a multi-lens camera", async ({
    frigateApp,
  }) => {
    await install(frigateApp.page);
    await frigateApp.page.route(
      "**/api/onvif/cameras/front_door/imaging*",
      (route) => {
        const response = image();
        response.profiles.push({
          token: "lens2",
          name: "Lens Two",
          video_source_token: "sensor2",
        });
        const token = new URL(route.request().url()).searchParams.get(
          "profile_token",
        );
        if (token === "lens2") {
          response.profile_token = "lens2";
          response.imaging!.video_source_token = "sensor2";
        } else {
          response.state = "choose_profile";
          response.profile_token = null;
          response.imaging = null;
        }
        return route.fulfill({ json: response });
      },
    );
    await frigateApp.goto(SETTINGS);
    await frigateApp.page
      .getByRole("button", { name: "Read camera image controls", exact: true })
      .click();
    await expect(
      frigateApp.page.getByText(
        "This camera reports multiple physical sources.",
        { exact: false },
      ),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByLabel("Brightness", { exact: true }),
    ).toHaveCount(0);
    await frigateApp.page
      .getByLabel("Physical image source (media profile)", { exact: true })
      .selectOption("lens2");
    await expect(
      frigateApp.page.getByLabel("Brightness", { exact: true }),
    ).toBeVisible();
  });

  test("previews each copy target and rejects incompatible device ranges before consent", async ({
    frigateApp,
  }) => {
    await install(frigateApp.page);
    let targetWrites = 0;
    await frigateApp.page.route(
      "**/api/onvif/cameras/front_door/imaging*",
      (route) => route.fulfill({ json: image() }),
    );
    await frigateApp.page.route(
      "**/api/onvif/cameras/backyard/imaging*",
      (route) => {
        if (route.request().method() === "PUT") targetWrites++;
        const response = image();
        response.imaging!.controls[0].limits = { minimum: 0, maximum: 55 };
        return route.fulfill({ json: response });
      },
    );
    await frigateApp.goto(SETTINGS);
    await frigateApp.page
      .getByRole("button", { name: "Read camera image controls", exact: true })
      .click();
    await frigateApp.page.getByLabel("Brightness", { exact: true }).fill("60");
    await frigateApp.page
      .getByText("Apply staged image changes to other cameras", { exact: true })
      .click();
    const backyard = frigateApp.page
      .locator("div.rounded.border.p-3")
      .filter({ has: frigateApp.page.getByText("backyard", { exact: true }) });
    await backyard
      .getByRole("button", {
        name: "Read target capability preview",
        exact: true,
      })
      .click();
    await expect(
      backyard.getByText(
        "This target did not advertise compatible options for:",
        { exact: false },
      ),
    ).toBeVisible();
    await expect(backyard.getByRole("checkbox")).toHaveCount(0);
    expect(targetWrites).toBe(0);
  });
});
