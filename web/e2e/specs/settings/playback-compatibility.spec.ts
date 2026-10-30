import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { test, expect } from "../../fixtures/frigate-test";
import { configFactory } from "../../fixtures/mock-data/config";
import { viewerProfile } from "../../fixtures/mock-data/profile";

const schema = JSON.parse(
  readFileSync(
    resolve(
      dirname(fileURLToPath(import.meta.url)),
      "../../fixtures/mock-data/config-schema.json",
    ),
    "utf8",
  ),
);
const settings = "/settings?page=systemUi";

test.describe("Global playback compatibility settings @mobile", () => {
  test("admin can independently save both global switches without restarting", async ({
    frigateApp,
  }) => {
    const config = configFactory({
      ui: { live_compatibility: true, recording_compatibility: true },
    });
    const page = frigateApp.page;
    await page.route("**/api/config", (route) =>
      route.fulfill({ json: config }),
    );
    await page.route("**/api/config/raw_paths", (route) =>
      route.fulfill({ json: config }),
    );
    await page.route("**/api/config/schema.json", (route) =>
      route.fulfill({ json: schema }),
    );
    await page.route("**/api/config/set", (route) =>
      route.fulfill({ json: { success: true, go2rtc_synced: true } }),
    );
    await frigateApp.goto(settings);
    const live = page.getByRole("switch", {
      name: "Allow Live compatibility streams",
      exact: true,
    });
    const review = page.getByRole("switch", {
      name: "Allow Review compatibility playback",
      exact: true,
    });
    await expect(live).toHaveAttribute("aria-checked", "true");
    await expect(review).toHaveAttribute("aria-checked", "true");
    await live.click();
    await expect(live).toHaveAttribute("aria-checked", "false");
    await expect(review).toHaveAttribute("aria-checked", "true");
    await review.click();
    const saved = page.waitForRequest("**/api/config/set");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    expect((await saved).postDataJSON()).toMatchObject({
      requires_restart: 0,
      update_topic: "config/ui",
      config_data: {
        ui: { live_compatibility: false, recording_compatibility: false },
      },
    });
  });

  test("viewers cannot access the global administrator switches", async ({
    frigateApp,
  }) => {
    await frigateApp.api.install({ profile: viewerProfile() });
    await frigateApp.goto(settings);
    await expect(
      frigateApp.page.getByRole("heading", { name: "Settings", exact: true }),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByRole("switch", {
        name: /Allow (?:Live|Review) compatibility/,
      }),
    ).toHaveCount(0);
    await expect(
      frigateApp.page.getByRole("heading", {
        name: "This page stopped working",
      }),
    ).toHaveCount(0);
  });
});
