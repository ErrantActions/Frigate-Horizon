import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { test, expect, FrigateApp } from "../../fixtures/frigate-test";

const schema = JSON.parse(
  readFileSync(
    resolve(
      dirname(fileURLToPath(import.meta.url)),
      "../../fixtures/mock-data/config-schema.json",
    ),
    "utf-8",
  ),
);

async function installCopyFixtures(app: FrigateApp) {
  await app.installDefaults({
    config: {
      cameras: {
        front_door: {
          detect: { width: 1920, height: 1080 },
          motion: { threshold: 17, mask: ["0,0,0.5,0.5"] },
          ffmpeg: {
            inputs: [
              {
                path: "rtsp://fixture-user:fixture-secret@source/stream",
                roles: ["detect", "record"],
              },
            ],
          },
        },
        backyard: {
          detect: { width: 1280, height: 720 },
          motion: { threshold: 31, mask: ["0,0,1,1"] },
        },
        garage: { motion: { threshold: 42 } },
      },
    },
  });
  await app.page.route("**/api/config/schema.json", (route) =>
    route.fulfill({ json: schema }),
  );
}

async function openCopy(app: FrigateApp) {
  const copy = app.page.getByRole("button", {
    name: "Copy settings from Front Door",
    exact: true,
  });
  if (!(await copy.isVisible())) {
    await app.page
      .getByRole("button", { name: "Front Door", exact: true })
      .click();
  }
  await app.page
    .getByRole("button", {
      name: "Copy settings from Front Door",
      exact: true,
    })
    .click();
  const dialog = app.page.getByRole("dialog").filter({
    has: app.page.getByRole("heading", {
      name: "Copy camera settings",
      exact: true,
    }),
  });
  await expect(dialog).toBeVisible();
  return dialog;
}

test.describe("Selective camera settings copy @critical @mobile", () => {
  test("copies selected settings to multiple cameras without identity, dimensions or geometry", async ({
    frigateApp,
  }) => {
    await installCopyFixtures(frigateApp);
    await frigateApp.goto("/settings");
    const dialog = await openCopy(frigateApp);
    await dialog
      .getByRole("checkbox", { name: "Backyard", exact: true })
      .check();
    await dialog.getByRole("checkbox", { name: "Garage", exact: true }).check();
    await dialog
      .getByRole("checkbox", { name: "Motion detection", exact: true })
      .check();
    await dialog
      .getByRole("checkbox", {
        name: "Object detection and filters",
        exact: true,
      })
      .check();
    const request = frigateApp.page.waitForRequest(
      (candidate) =>
        candidate.url().endsWith("/api/config/set") &&
        candidate.method() === "PUT",
    );
    await dialog
      .getByRole("button", { name: "Apply to 2 cameras", exact: true })
      .click();
    const payload = (await request).postDataJSON();
    expect(Object.keys(payload.config_data.cameras).sort()).toEqual([
      "backyard",
      "garage",
    ]);
    expect(payload.config_data.cameras.backyard.motion.threshold).toBe(17);
    expect(payload.config_data.cameras.garage.motion.threshold).toBe(17);
    expect(payload.update_topics).toEqual(
      expect.arrayContaining([
        "config/cameras/backyard/motion",
        "config/cameras/garage/motion",
      ]),
    );
    for (const camera of Object.values(payload.config_data.cameras)) {
      expect(camera).not.toHaveProperty("ffmpeg");
      expect(camera).not.toHaveProperty("onvif");
      expect(camera).not.toHaveProperty("enabled");
      expect(camera).not.toHaveProperty("motion.mask");
      expect(camera).not.toHaveProperty("detect.width");
      expect(camera).not.toHaveProperty("detect.height");
      expect(camera).not.toHaveProperty("zones");
    }
    expect(JSON.stringify(payload)).not.toContain("fixture-secret");
    await expect(dialog).toHaveCount(0);
  });

  test("protects unsaved edits on both source and destination cameras", async ({
    frigateApp,
  }) => {
    await installCopyFixtures(frigateApp);
    await frigateApp.goto("/settings?page=cameraMotion&camera=backyard");
    await frigateApp.page
      .getByRole("switch", { name: "Enable motion detection" })
      .click();
    await expect(
      frigateApp.page.getByRole("button", { name: "Save", exact: true }),
    ).toBeEnabled();
    if (frigateApp.isMobile) {
      await frigateApp.page
        .getByRole("button", { name: "Go back", exact: true })
        .click();
    }
    const dialog = await openCopy(frigateApp);
    await expect(
      dialog.getByRole("checkbox", { name: /Backyard/ }),
    ).toBeDisabled();
    await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
    await frigateApp.page
      .getByRole("button", {
        name: "Front Door: Motion detection",
        exact: true,
      })
      .click();
    await frigateApp.page
      .getByRole("switch", { name: "Enable motion detection" })
      .click();
    await expect(
      frigateApp.page.getByRole("button", { name: "Save", exact: true }),
    ).toBeEnabled();
    if (frigateApp.isMobile) {
      await frigateApp.page
        .getByRole("button", { name: "Go back", exact: true })
        .click();
    }
    const sourceDialog = await openCopy(frigateApp);
    await expect(sourceDialog.getByRole("alert")).toContainText(
      "source camera has unsaved edits",
    );
    await sourceDialog
      .getByRole("checkbox", { name: "Garage", exact: true })
      .check();
    await sourceDialog
      .getByRole("checkbox", { name: "Motion detection", exact: true })
      .check();
    await expect(
      sourceDialog.getByRole("button", {
        name: "Apply to 1 camera",
        exact: true,
      }),
    ).toBeDisabled();
  });

  test("an unconfirmed copy requires a refresh and is not retried automatically", async ({
    frigateApp,
  }) => {
    await installCopyFixtures(frigateApp);
    let attempts = 0;
    await frigateApp.page.route("**/api/config/set", (route) => {
      attempts++;
      return route.fulfill({ json: { success: false } });
    });
    await frigateApp.goto("/settings");
    const dialog = await openCopy(frigateApp);
    await dialog
      .getByRole("checkbox", { name: "Backyard", exact: true })
      .check();
    await dialog
      .getByRole("checkbox", { name: "Motion detection", exact: true })
      .check();
    await dialog
      .getByRole("button", { name: "Apply to 1 camera", exact: true })
      .click();
    await expect(dialog.getByRole("alert")).toContainText(
      "copy could not be confirmed",
    );
    await expect(
      dialog.getByRole("button", { name: "Apply to 1 camera", exact: true }),
    ).toBeDisabled();
    expect(attempts).toBe(1);
    await dialog
      .getByRole("button", { name: "Refresh configuration", exact: true })
      .click();
    await expect(
      dialog.getByRole("button", { name: "Apply to 1 camera", exact: true }),
    ).toBeEnabled();
    expect(attempts).toBe(1);
  });
});
