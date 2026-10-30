import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { test, expect } from "../../fixtures/frigate-test";
import { settingsViewGroups } from "../../../src/types/settings";

const schema = JSON.parse(
  readFileSync(
    resolve(
      dirname(fileURLToPath(import.meta.url)),
      "../../fixtures/mock-data/config-schema.json",
    ),
    "utf-8",
  ),
);

test.describe("Task-based settings navigation @mobile", () => {
  test("nests camera tasks beneath individual cameras while keeping global groups", async ({
    frigateApp,
  }) => {
    const views = settingsViewGroups.flatMap((group) => [...group.views]);
    expect(new Set(views).size).toBe(views.length);
    await frigateApp.goto("/settings");
    for (const label of [
      "Picture and environment",
      "Defaults for all cameras",
      "Accounts and security",
      "Advanced system",
    ]) {
      await expect(
        frigateApp.page.getByRole("button", { name: label, exact: true }),
      ).toBeVisible();
    }
    await expect(
      frigateApp.page.getByRole("button", { name: "Front Door", exact: true }),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByRole("button", {
        name: "Front Door: Motion detection",
        exact: true,
      }),
    ).toHaveCount(0);
    await frigateApp.page
      .getByRole("button", { name: "Front Door", exact: true })
      .click();
    await expect(
      frigateApp.page.getByRole("button", {
        name: "Front Door: Motion detection",
        exact: true,
      }),
    ).toHaveCount(0);
    const detectionGroup = frigateApp.page.getByRole("button", {
      name: "Front Door: Camera detection and events",
      exact: true,
    });
    await detectionGroup.focus();
    await detectionGroup.press("Enter");
    await expect(detectionGroup).toHaveAttribute("aria-expanded", "true");
    await frigateApp.page
      .getByRole("button", {
        name: "Front Door: Motion detection",
        exact: true,
      })
      .click();
    await expect(
      frigateApp.page.getByRole("heading", {
        level: 4,
        name: "Motion detection",
        exact: true,
      }),
    ).toBeVisible();
  });

  test("shows only the expanded camera copy action and one task group at a time", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    await frigateApp.goto("/settings");
    await expect(
      page.getByRole("button", { name: /^Copy settings from / }),
    ).toHaveCount(0);
    await page.getByRole("button", { name: "Front Door", exact: true }).click();
    await expect(
      page.getByRole("button", {
        name: "Copy settings from Front Door",
        exact: true,
      }),
    ).toBeVisible();
    await page
      .getByRole("button", {
        name: "Front Door: Camera detection and events",
        exact: true,
      })
      .click();
    await expect(
      page.getByRole("button", {
        name: "Front Door: Motion detection",
        exact: true,
      }),
    ).toBeVisible();
    await page
      .getByRole("button", {
        name: "Front Door: Cameras and streams",
        exact: true,
      })
      .click();
    await expect(
      page.getByRole("button", {
        name: "Front Door: Motion detection",
        exact: true,
      }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", {
        name: "Front Door: Live playback",
        exact: true,
      }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Backyard", exact: true }).click();
    await expect(
      page.getByRole("button", {
        name: "Copy settings from Front Door",
        exact: true,
      }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", {
        name: "Copy settings from Backyard",
        exact: true,
      }),
    ).toBeVisible();
  });

  test("keeps disabled cameras configurable instead of switching to another camera", async ({
    frigateApp,
  }) => {
    await frigateApp.installDefaults({
      config: {
        cameras: { garage: { enabled: false, enabled_in_config: false } },
      },
    });
    await frigateApp.page.route("**/api/config/schema.json", (route) =>
      route.fulfill({ json: schema }),
    );
    await frigateApp.goto("/settings?page=cameraMotion&camera=garage");
    await expect(
      frigateApp.page.getByRole("heading", {
        level: 4,
        name: "Motion detection",
        exact: true,
      }),
    ).toBeVisible();
    if (frigateApp.isMobile) {
      await frigateApp.page
        .getByRole("button", { name: "Go back", exact: true })
        .click();
    }
    await expect(
      frigateApp.page.getByRole("button", {
        name: "Garage: Motion detection",
        exact: true,
      }),
    ).toHaveAttribute("aria-current", "page");
  });

  test("keeps existing camera and global settings deep links working", async ({
    frigateApp,
  }) => {
    await frigateApp.page.route("**/api/config/schema.json", (route) =>
      route.fulfill({ json: schema }),
    );
    await frigateApp.goto("/settings?page=cameraMotion&camera=front_door");
    await expect(
      frigateApp.page.getByRole("heading", {
        level: 4,
        name: "Motion detection",
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByRole("switch", { name: "Enable motion detection" }),
    ).toBeVisible();
    await frigateApp.goto("/settings?page=globalWeather");
    await expect(
      frigateApp.page.getByRole("textbox", { name: "ZIP code", exact: true }),
    ).toBeVisible();
  });

  test("keeps pending edits when moving between task groups", async ({
    frigateApp,
  }) => {
    await frigateApp.page.route("**/api/config/schema.json", (route) =>
      route.fulfill({ json: schema }),
    );
    await frigateApp.goto("/settings");
    await frigateApp.page
      .getByRole("button", { name: "Picture and environment", exact: true })
      .click();
    await frigateApp.page.getByText("Weather", { exact: true }).click();
    await frigateApp.page
      .getByRole("textbox", { name: "ZIP code", exact: true })
      .fill("00501");
    if (frigateApp.isMobile) {
      await frigateApp.page
        .getByRole("button", { name: "Go back", exact: true })
        .click();
    }
    await frigateApp.page
      .getByRole("button", { name: "Accounts and security", exact: true })
      .click();
    await frigateApp.page.getByText("Users", { exact: true }).click();
    await expect(
      frigateApp.page.getByRole("button", { name: "Add User", exact: true }),
    ).toBeVisible();
    if (frigateApp.isMobile) {
      await frigateApp.page
        .getByRole("button", { name: "Go back", exact: true })
        .click();
    }
    const save = frigateApp.page.getByRole("button", {
      name: "Save All",
      exact: true,
    });
    await expect(save).toBeEnabled();
    const request = frigateApp.page.waitForRequest("**/api/config/set");
    await save.click();
    expect((await request).postDataJSON()).toMatchObject({
      requires_restart: 0,
      config_data: { weather: { zip_code: "00501" } },
    });
  });
});
