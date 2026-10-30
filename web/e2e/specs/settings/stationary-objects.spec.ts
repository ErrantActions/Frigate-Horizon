import { test, expect } from "../../fixtures/frigate-test";
import { readFileSync } from "node:fs";
import type { StationaryObjectsSnapshot } from "../../../src/types/stationaryObjects";

const configSchema = JSON.parse(
  readFileSync(
    new URL("../../fixtures/mock-data/config-schema.json", import.meta.url),
    "utf-8",
  ),
);

const snapshot: StationaryObjectsSnapshot = {
  camera: "front_door",
  status: "ready",
  sampled_at: 1700000001,
  frame_time: 1700000000,
  frame_age_seconds: 1,
  frame_width: 640,
  frame_height: 360,
  stationary_threshold: 50,
  total: 1,
  limit: 100,
  objects: [
    {
      id: "1699999980-abcdef",
      label: "car",
      box: [10, 20, 100, 150],
      score: 0.85,
      stationary_frames: 75,
      position_changes: 0,
      tracked_seconds: 20,
      last_tracking_update: 1700000000,
      updated_in_current_frame: true,
    },
  ],
};

test.describe("Stationary objects on Object detection @mobile", () => {
  test.beforeEach(async ({ frigateApp }) => {
    await frigateApp.installDefaults({
      configSchema,
      configRaw:
        "mqtt:\n  enabled: false\ncameras:\n  front_door:\n    detect:\n      width: 640\n      height: 360\n      fps: 5\n",
    });
  });

  test("is opt-in, read-only, camera-scoped and shows real tracker details", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    let reads = 0;
    let writes = 0;
    await page.route("**/api/front_door/stationary-objects", async (route) => {
      reads++;
      expect(route.request().method()).toBe("GET");
      await route.fulfill({ json: snapshot });
    });
    await page.route("**/api/config/set", async (route) => {
      writes++;
      await route.fulfill({ json: { success: true } });
    });
    await frigateApp.goto("/settings?page=cameraDetect&camera=front_door");
    const button = page.getByRole("button", {
      name: "Show stationary objects",
      exact: true,
    });
    await expect(button).toBeVisible();
    expect(reads).toBe(0);
    await button.click();
    const region = page.getByRole("region", {
      name: "Stationary object inspector",
      exact: true,
    });
    await expect(region).toBeVisible();
    await expect(
      region.getByText("1699999980-abcdef", { exact: true }),
    ).toBeVisible();
    await expect(region.getByText("85%", { exact: true })).toBeVisible();
    await expect(
      region.getByRole("cell", { name: "75", exact: true }),
    ).toBeVisible();
    await expect(
      region.getByText("Updated in current tracker frame", { exact: true }),
    ).toBeVisible();
    await expect(region.getByRole("img")).toHaveAttribute(
      "src",
      /front_door\?fps=5&height=640&bbox=1&stationary=true/,
    );
    expect(writes).toBe(0);
    await expect(
      page.getByRole("button", { name: "Save", exact: true }),
    ).toBeDisabled();
  });

  test("switches live and snapshot overlays without changing configuration or restarting the stream on table refresh", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    let reads = 0;
    let writes = 0;
    await page.route("**/api/front_door/stationary-objects", (route) =>
      route.fulfill({ json: { ...snapshot, sampled_at: ++reads } }),
    );
    await page.route("**/api/config/set", async (route) => {
      writes++;
      await route.fulfill({ json: { success: true } });
    });
    await frigateApp.goto("/settings?page=cameraDetect&camera=front_door");
    await page
      .getByRole("button", { name: "Show stationary objects", exact: true })
      .click();
    const region = page.getByRole("region", {
      name: "Stationary object inspector",
      exact: true,
    });
    const image = region.getByRole("img");
    const live = region.getByRole("checkbox", { name: "Live view (5 FPS)" });
    const detections = region.getByRole("checkbox", {
      name: "Show all live detection boxes",
    });
    await expect(live).toBeChecked();
    await expect(detections).not.toBeChecked();
    await expect(image).toHaveAttribute("src", /stationary=true/);
    const initialSource = await image.getAttribute("src");
    const initialReads = reads;
    await expect.poll(() => reads).toBeGreaterThan(initialReads);
    await expect(image).toHaveAttribute("src", initialSource!);
    await detections.check();
    await expect(image).toHaveAttribute("src", /stationary=false/);
    await expect(region.getByText(/unconfirmed candidates/)).toBeVisible();
    await live.uncheck();
    await expect(image).toHaveAttribute(
      "src",
      /latest\.jpg\?bbox=1&stationary=false/,
    );
    await detections.uncheck();
    await expect(image).toHaveAttribute(
      "src",
      /latest\.jpg\?bbox=1&stationary=true/,
    );
    expect(writes).toBe(0);
    await expect(
      page.getByRole("button", { name: "Save", exact: true }),
    ).toBeDisabled();
  });

  test("labels stale snapshots and older updates instead of claiming fresh detections", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    await page.route("**/api/front_door/stationary-objects", (route) =>
      route.fulfill({
        json: {
          ...snapshot,
          status: "stale",
          frame_age_seconds: 60,
          objects: [
            { ...snapshot.objects[0], updated_in_current_frame: false },
          ],
        },
      }),
    );
    await frigateApp.goto("/settings?page=cameraDetect&camera=front_door");
    await page
      .getByRole("button", { name: "Show stationary objects", exact: true })
      .click();
    await expect(page.getByText(/These are last-known tracks/)).toBeVisible();
    await expect(
      page.getByText("Not updated in current tracker frame", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText(/does not mean the AI ran on this frame/),
    ).toBeVisible();
    await expect(
      page
        .getByRole("region", {
          name: "Stationary object inspector",
          exact: true,
        })
        .getByRole("img"),
    ).toHaveCount(0);
  });

  test("rejects cross-camera data and never shows it as an empty successful result", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    await page.route("**/api/front_door/stationary-objects", (route) =>
      route.fulfill({ json: { ...snapshot, camera: "back_yard" } }),
    );
    await frigateApp.goto("/settings?page=cameraDetect&camera=front_door");
    await page
      .getByRole("button", { name: "Show stationary objects", exact: true })
      .click();
    await expect(
      page.getByRole("alert").filter({ hasText: /response was invalid/ }),
    ).toBeVisible();
    await expect(
      page.getByText("1699999980-abcdef", { exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByText("No accepted stationary objects are currently tracked.", {
        exact: true,
      }),
    ).toHaveCount(0);
  });

  test("stops polling when hidden and closed, then resumes when visible", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    let reads = 0;
    await page.route("**/api/front_door/stationary-objects", async (route) => {
      reads++;
      await route.fulfill({ json: snapshot });
    });
    await frigateApp.goto("/settings?page=cameraDetect&camera=front_door");
    await page
      .getByRole("button", { name: "Show stationary objects", exact: true })
      .click();
    await expect(
      page.getByText("1699999980-abcdef", { exact: true }),
    ).toBeVisible();
    await page.clock.install();
    await page.evaluate(() => {
      Object.defineProperty(document, "hidden", {
        configurable: true,
        get: () => true,
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await expect(
      page.getByText("Inspection is paused while this page is hidden.", {
        exact: true,
      }),
    ).toBeVisible();
    const paused = reads;
    await expect(
      page.getByRole("img", { name: /objects for front_door/ }),
    ).toHaveCount(0);
    await page.clock.fastForward(6000);
    expect(reads).toBe(paused);
    await page.evaluate(() => {
      Object.defineProperty(document, "hidden", {
        configurable: true,
        get: () => false,
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await expect.poll(() => reads).toBeGreaterThan(paused);
    await page
      .getByRole("button", { name: "Hide stationary objects", exact: true })
      .click();
    const closed = reads;
    await page.clock.fastForward(6000);
    expect(reads).toBe(closed);
    await expect(
      page.getByRole("region", {
        name: "Stationary object inspector",
        exact: true,
      }),
    ).toHaveCount(0);
  });

  test("does not appear in global Object detection defaults", async ({
    frigateApp,
  }) => {
    await frigateApp.goto("/settings?page=globalDetect");
    await expect(
      frigateApp.page.getByRole("button", {
        name: "Show stationary objects",
        exact: true,
      }),
    ).toHaveCount(0);
  });

  test("switching cameras clears old tracks and requires reopening the inspector", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    await page.route("**/api/front_door/stationary-objects", (route) =>
      route.fulfill({ json: snapshot }),
    );
    await page.route("**/api/backyard/stationary-objects", (route) =>
      route.fulfill({
        json: {
          ...snapshot,
          camera: "backyard",
          objects: [{ ...snapshot.objects[0], id: "1700000000-back-yard" }],
        },
      }),
    );
    await frigateApp.goto("/settings?page=cameraDetect&camera=front_door");
    await page
      .getByRole("button", { name: "Show stationary objects", exact: true })
      .click();
    await expect(
      page.getByText("1699999980-abcdef", { exact: true }),
    ).toBeVisible();
    await frigateApp.goto("/settings?page=cameraDetect&camera=backyard");
    await expect(
      page.getByText("1699999980-abcdef", { exact: true }),
    ).toHaveCount(0);
    await page
      .getByRole("button", { name: "Show stationary objects", exact: true })
      .click();
    await expect(
      page.getByText("1700000000-back-yard", { exact: true }),
    ).toBeVisible();
  });

  test.describe("intentional unavailable stationary endpoint", () => {
    test.use({ expectedErrors: [/404|Not Found/] });

    test("reports an unavailable backend explicitly and retry does not save settings", async ({
      frigateApp,
    }) => {
      const { page } = frigateApp;
      let available = false;
      let writes = 0;
      await page.route("**/api/front_door/stationary-objects", (route) =>
        available
          ? route.fulfill({ json: snapshot })
          : route.fulfill({ status: 404, json: { detail: "Not Found" } }),
      );
      await page.route("**/api/config/set", async (route) => {
        writes++;
        await route.fulfill({ json: { success: true } });
      });
      await frigateApp.goto("/settings?page=cameraDetect&camera=front_door");
      await page
        .getByRole("button", { name: "Show stationary objects", exact: true })
        .click();
      await expect(
        page
          .getByRole("alert")
          .filter({ hasText: /unavailable on this backend/ }),
      ).toBeVisible();
      await expect(
        page.getByText(
          "No accepted stationary objects are currently tracked.",
          { exact: true },
        ),
      ).toHaveCount(0);
      available = true;
      await page.getByRole("button", { name: "Retry", exact: true }).click();
      await expect(
        page.getByText("1699999980-abcdef", { exact: true }),
      ).toBeVisible();
      expect(writes).toBe(0);
    });
  });
});
