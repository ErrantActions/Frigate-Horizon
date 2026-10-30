import { test, expect } from "../../fixtures/frigate-test";
import { readFileSync } from "node:fs";
import { statsFactory } from "../../fixtures/mock-data/stats";

const configSchema = JSON.parse(
  readFileSync(
    new URL("../../fixtures/mock-data/config-schema.json", import.meta.url),
    "utf-8",
  ),
);

const range = { enabled: false, min_height: 720, max_height: 2160 };
const applied = () => ({
  ...range,
  width: 3840,
  height: 2160,
  source_width: 3840,
  source_height: 2160,
  reason: "disabled" as const,
  load: 0.2,
  sampled_at: Date.now() / 1000,
  last_inference_time: Date.now() / 1000,
});

test.describe("Dynamic object-detection resolution @mobile", () => {
  test.beforeEach(async ({ frigateApp }) => {
    await frigateApp.installDefaults({
      configSchema,
      configRaw:
        "mqtt:\n  enabled: false\ncameras:\n  front_door:\n    detect:\n      width: 3840\n      height: 2160\n      fps: 5\n      dynamic_resolution:\n        enabled: false\n        min_height: 720\n        max_height: 2160\n",
      config: {
        cameras: {
          front_door: {
            detect: {
              width: 3840,
              height: 2160,
              fps: 5,
              dynamic_resolution: range,
            },
          },
        },
      },
      stats: {
        cameras: { front_door: { dynamic_resolution: applied() } },
      },
    });
  });

  test("saves independent bounds live without changing capture FPS, inputs or recordings", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    await frigateApp.goto("/settings?page=cameraDetect&camera=front_door");
    await expect(
      page.getByText("Automatic resolution is off. Analysis uses 3840 × 2160."),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Save", exact: true }),
    ).toBeDisabled();
    await page
      .getByRole("heading", {
        name: "Automatic analysis resolution",
        exact: true,
      })
      .click();
    await page
      .getByRole("switch", {
        name: "Automatic analysis resolution",
        exact: true,
      })
      .check();
    await page
      .getByRole("textbox", { name: "Minimum analysis height", exact: true })
      .fill("1080");
    await page
      .getByRole("textbox", { name: "Maximum analysis height", exact: true })
      .fill("1440");
    const pending = page.waitForRequest(
      (request) =>
        request.url().endsWith("/api/config/set") && request.method() === "PUT",
    );
    await page.getByRole("button", { name: "Save", exact: true }).click();
    const body = (await pending).postDataJSON();
    expect(body.requires_restart).toBe(0);
    expect(body.update_topic).toBe("config/cameras/front_door/detect");
    expect(
      body.config_data.cameras.front_door.detect.dynamic_resolution,
    ).toEqual({
      enabled: true,
      min_height: 1080,
      max_height: 1440,
    });
    expect(body.config_data.cameras.front_door.detect.width).toBeUndefined();
    expect(body.config_data.cameras.front_door.detect.height).toBeUndefined();
    expect(body.config_data.cameras.front_door.detect.fps).toBeUndefined();
    expect(body.config_data.cameras.front_door.ffmpeg).toBeUndefined();
    expect(body.config_data.cameras.front_door.record).toBeUndefined();
  });

  test("distinguishes applied worker resolution, pending saved bounds and stale reports", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    await frigateApp.goto("/settings?page=cameraDetect&camera=front_door");
    await expect(
      page.getByText("Automatic resolution is off. Analysis uses 3840 × 2160."),
    ).toBeVisible();
    frigateApp.ws.send(
      "stats",
      JSON.stringify(
        statsFactory({
          cameras: {
            front_door: { dynamic_resolution: { ...applied(), enabled: true } },
          },
        }),
      ),
    );
    await expect(
      page.getByText(
        "Saved bounds are waiting for the camera worker to apply them.",
      ),
    ).toBeVisible();
    frigateApp.ws.send(
      "stats",
      JSON.stringify(
        statsFactory({
          cameras: {
            front_door: {
              dynamic_resolution: {
                ...applied(),
                enabled: true,
                error: "capture_geometry_pending",
              },
            },
          },
        }),
      ),
    );
    await expect(
      page.getByText(/The requested range exceeds the active capture size/),
    ).toBeVisible();
    frigateApp.ws.send(
      "stats",
      JSON.stringify(
        statsFactory({
          cameras: {
            front_door: {
              dynamic_resolution: {
                ...applied(),
                sampled_at: Date.now() / 1000 - 60,
              },
            },
          },
        }),
      ),
    );
    await expect(
      page.getByText(
        "Waiting for a current resolution report from the camera worker.",
      ),
    ).toBeVisible();
  });

  test("reports actual worker dimensions and does not carry them to another camera", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    await frigateApp.installDefaults({
      configSchema,
      config: {
        cameras: {
          front_door: {
            detect: {
              width: 3840,
              height: 2160,
              fps: 5,
              dynamic_resolution: { ...range, enabled: true },
            },
          },
        },
      },
      stats: {
        cameras: {
          front_door: {
            dynamic_resolution: {
              ...applied(),
              enabled: true,
              reason: "maximum",
            },
          },
        },
      },
    });
    await frigateApp.goto("/settings?page=cameraDetect&camera=front_door");
    await expect(page.getByText("Maximum detail: 3840 × 2160.")).toBeVisible();
    frigateApp.ws.send(
      "stats",
      JSON.stringify(
        statsFactory({
          cameras: {
            front_door: {
              dynamic_resolution: {
                ...applied(),
                enabled: true,
                reason: "overloaded",
                width: 1920,
                height: 1080,
              },
            },
          },
        }),
      ),
    );
    await expect(
      page.getByText(
        "Reduced to 1920 × 1080 because processing is falling behind.",
      ),
    ).toBeVisible();
    await frigateApp.goto("/settings?page=cameraDetect&camera=backyard");
    await expect(
      page.getByText(
        "Reduced to 1920 × 1080 because processing is falling behind.",
      ),
    ).toHaveCount(0);
    await expect(
      page.getByText(
        "Waiting for a current resolution report from the camera worker.",
      ),
    ).toBeVisible();
  });

  test("uses fresher polled worker reports without waiting for a WebSocket broadcast", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    await frigateApp.installDefaults({
      configSchema,
      config: {
        cameras: {
          front_door: {
            detect: {
              width: 3840,
              height: 2160,
              fps: 5,
              dynamic_resolution: { ...range, enabled: true },
            },
          },
        },
      },
      stats: {
        cameras: {
          front_door: {
            dynamic_resolution: {
              ...applied(),
              enabled: true,
              reason: "maximum",
            },
          },
        },
      },
    });
    let reduced = false;
    await page.route("**/api/stats", (route) =>
      route.fulfill({
        json: statsFactory({
          service: { last_updated: Date.now() / 1000 },
          cameras: {
            front_door: {
              dynamic_resolution: {
                ...applied(),
                enabled: true,
                width: reduced ? 1920 : 3840,
                height: reduced ? 1080 : 2160,
                reason: reduced ? "overloaded" : "maximum",
              },
            },
          },
        }),
      }),
    );
    await frigateApp.goto("/settings?page=cameraDetect&camera=front_door");
    await expect(page.getByText("Maximum detail: 3840 × 2160.")).toBeVisible();
    reduced = true;
    await expect(
      page.getByText(
        "Reduced to 1920 × 1080 because processing is falling behind.",
      ),
    ).toBeVisible({ timeout: 10000 });
  });

  test("measures report age in the server clock rather than assuming the browser clock matches", async ({
    frigateApp,
  }) => {
    const serverTime = Date.now() / 1000 - 3600;
    await frigateApp.installDefaults({
      configSchema,
      config: {
        cameras: {
          front_door: {
            detect: {
              width: 3840,
              height: 2160,
              fps: 5,
              dynamic_resolution: range,
            },
          },
        },
      },
      stats: {
        service: { last_updated: serverTime },
        cameras: {
          front_door: {
            dynamic_resolution: { ...applied(), sampled_at: serverTime - 1 },
          },
        },
      },
    });
    await frigateApp.goto("/settings?page=cameraDetect&camera=front_door");
    await expect(
      frigateApp.page.getByText(
        "Automatic resolution is off. Analysis uses 3840 × 2160.",
      ),
    ).toBeVisible();
  });
});
