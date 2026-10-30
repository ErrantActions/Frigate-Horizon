import { test, expect } from "../../fixtures/frigate-test";
import { readFileSync } from "node:fs";

const configSchema = JSON.parse(
  readFileSync(
    new URL("../../fixtures/mock-data/config-schema.json", import.meta.url),
    "utf-8",
  ),
);

test.describe("Detect resolution presets @mobile", () => {
  test.beforeEach(async ({ frigateApp }) => {
    await frigateApp.installDefaults({
      configSchema,
      configRaw:
        "mqtt:\n  enabled: false\ncameras:\n  front_door:\n    detect:\n      width: 640\n      height: 360\n      fps: 5\n",
      config: {
        cameras: {
          front_door: { detect: { width: 640, height: 360, fps: 5 } },
        },
      },
    });
    await frigateApp.goto("/settings?page=cameraDetect&camera=front_door");
  });

  test("stages paired 720p dimensions without changing FPS or inputs", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    const writes: unknown[] = [];
    await page.route("**/api/config/set", async (route) => {
      writes.push(route.request().postDataJSON());
      await route.fulfill({ json: { success: true, message: "Saved" } });
    });
    await page
      .getByRole("combobox", { name: "Detect resolution preset", exact: true })
      .selectOption("p720");
    expect(writes).toHaveLength(0);
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect.poll(() => writes.length).toBe(1);
    const request = writes[0] as {
      config_data: {
        cameras: {
          front_door: {
            detect: { width: number; height: number; fps?: number };
            ffmpeg?: unknown;
          };
        };
      };
      requires_restart: number;
    };
    expect(request.config_data.cameras.front_door.detect.width).toBe(1280);
    expect(request.config_data.cameras.front_door.detect.height).toBe(720);
    expect(request.config_data.cameras.front_door.detect.fps ?? 5).toBe(5);
    expect(request.config_data.cameras.front_door.ffmpeg).toBeUndefined();
    expect(request.requires_restart).toBe(1);
  });

  test("source clears both explicit dimensions and explains the detect input", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    await page
      .getByRole("combobox", { name: "Detect resolution preset", exact: true })
      .selectOption("source");
    await expect(
      page.getByText(/It does not switch a substream to the main stream/),
    ).toBeVisible();
    const request = page.waitForRequest(
      (value) =>
        value.url().endsWith("/api/config/set") && value.method() === "PUT",
    );
    await page.getByRole("button", { name: "Save", exact: true }).click();
    const data = (await request).postDataJSON();
    expect(data.config_data.cameras.front_door.detect.width).toBe("");
    expect(data.config_data.cameras.front_door.detect.height).toBe("");
  });

  test("offers popular sizes and custom without silently saving", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    const selector = page.getByRole("combobox", {
      name: "Detect resolution preset",
      exact: true,
    });
    for (const preset of ["p360", "p480", "p720", "p1080", "p1440", "p2160"]) {
      await selector.selectOption(preset);
      await expect(selector).toHaveValue(preset);
    }
    await expect(
      page.getByRole("status").filter({
        hasText: /Full 4K detect frames can increase processing load/,
      }),
    ).toBeVisible();
    await selector.selectOption("custom");
    await expect(selector).toHaveValue("custom");
  });
});
