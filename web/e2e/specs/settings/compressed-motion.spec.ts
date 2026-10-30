import { test, expect } from "../../fixtures/frigate-test";
import type { Route } from "@playwright/test";

const SETTINGS_URL = "/settings?page=motionTuner&camera=front_door";
const SESSION = "a".repeat(32);

function snapshot(tuned = false) {
  const activity = Array(576).fill(0);
  const x = Array(576).fill(0);
  const y = Array(576).fill(0);
  activity[100] = 0.5;
  x[100] = 3;
  y[100] = 2;
  return {
    camera: "front_door",
    session: SESSION,
    status: "running",
    server_time: Date.now() / 1000,
    data: {
      updated_at: Date.now() / 1000 - 0.1,
      width: 3840,
      height: 2160,
      rows: 18,
      columns: 32,
      activity,
      coded_activity: activity,
      vector_x: x,
      vector_y: y,
      uncertainty: null,
      minimum_activity: 0.01,
      autotune: tuned
        ? {
            status: "ready",
            samples: 60,
            minimum_samples: 60,
            held_cells: 0,
            limited_cells: 0,
            vector_floor: activity,
            coded_floor: activity,
            vector_signal: Array(576).fill(0),
            coded_signal: Array(576).fill(0),
            pixels_used: false,
            applies_to_live_detector: false,
          }
        : null,
    },
  };
}

test.describe("Compressed vector inspector @mobile", () => {
  test("compares learned-noise-filtered vectors with raw vectors without saving settings", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    await page.route(
      "**/api/front_door/compressed-motion-preview**",
      (route) => {
        if (route.request().method() === "POST")
          return route.fulfill({
            json: {
              camera: "front_door",
              session: SESSION,
              status: "warming_up",
              data: null,
            },
          });
        if (route.request().method() === "DELETE")
          return route.fulfill({ json: { success: true } });
        return route.fulfill({
          json: new URL(route.request().url()).searchParams.has("session")
            ? snapshot(true)
            : {
                camera: "front_door",
                available: true,
                status: "stopped",
                data: null,
              },
        });
      },
    );
    await frigateApp.goto(SETTINGS_URL);
    await page
      .getByRole("button", {
        name: "Inspect compressed motion vectors",
        exact: true,
      })
      .click();
    await expect(
      page.getByRole("checkbox", {
        name: "Learn compressed noise floor",
        exact: true,
      }),
    ).toBeChecked();
    await page
      .getByRole("button", { name: "Start vector preview", exact: true })
      .click();
    await expect(page.getByTestId("compressed-noise-status")).toContainText(
      "Noise floor ready",
    );
    const arrowPixels = () =>
      page
        .getByTestId("compressed-vector-canvas")
        .evaluate((node: HTMLCanvasElement) => {
          const pixels = node
            .getContext("2d")!
            .getImageData(0, 0, node.width, node.height).data;
          let count = 0;
          for (let i = 0; i < pixels.length; i += 4)
            if (pixels[i + 1] > 180 && pixels[i + 2] > 180) count++;
          return count;
        });
    await expect.poll(arrowPixels).toBe(0);
    await page
      .getByRole("checkbox", {
        name: "Show activity above learned noise",
        exact: true,
      })
      .uncheck();
    await expect.poll(arrowPixels).toBeGreaterThan(0);
    await page
      .getByRole("button", { name: "Stop vector preview", exact: true })
      .click();
  });

  test("a polling request racing Stop does not leave a false failure alert", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    let reads = 0;
    let pending: Route | undefined;
    await page.route(
      "**/api/front_door/compressed-motion-preview**",
      async (route) => {
        if (route.request().method() === "POST") {
          return route.fulfill({
            json: {
              camera: "front_door",
              session: SESSION,
              status: "warming_up",
              data: null,
            },
          });
        }
        if (route.request().method() === "DELETE") {
          if (pending)
            await pending.fulfill({
              status: 404,
              json: { detail: "Session stopped" },
            });
          return route.fulfill({ json: { success: true } });
        }
        if (new URL(route.request().url()).searchParams.has("session")) {
          reads++;
          if (reads > 1) {
            pending = route;
            return;
          }
          return route.fulfill({ json: snapshot() });
        }
        return route.fulfill({
          json: {
            camera: "front_door",
            available: true,
            status: "stopped",
            data: null,
          },
        });
      },
    );
    await frigateApp.goto(SETTINGS_URL);
    await page
      .getByRole("button", {
        name: "Inspect compressed motion vectors",
        exact: true,
      })
      .click();
    await page
      .getByRole("button", { name: "Start vector preview", exact: true })
      .click();
    await expect.poll(() => reads).toBeGreaterThan(1);
    await page
      .getByRole("button", { name: "Stop vector preview", exact: true })
      .click();
    await expect(
      page.getByRole("button", { name: "Start vector preview", exact: true }),
    ).toBeEnabled();
    await expect(
      page
        .getByRole("alert")
        .filter({ hasText: "Vector preview is unavailable" }),
    ).toHaveCount(0);
  });

  test("requires explicit start, renders real vectors and stops on close without saving config", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    let starts = 0;
    let stops = 0;
    let saves = 0;
    page.on("request", (request) => {
      if (request.url().includes("config/set")) saves++;
    });
    await page.route(
      "**/api/front_door/compressed-motion-preview**",
      (route) => {
        const method = route.request().method();
        if (method === "POST") {
          starts++;
          expect(route.request().postDataJSON()).toEqual({
            min_distance: 2,
            max_distance: 10,
            min_activity: 0.01,
            auto_tune: true,
          });
          return route.fulfill({
            json: {
              camera: "front_door",
              session: SESSION,
              status: "warming_up",
              data: null,
            },
          });
        }
        if (method === "DELETE") {
          stops++;
          return route.fulfill({ json: { success: true } });
        }
        return route.fulfill({
          json: new URL(route.request().url()).searchParams.has("session")
            ? snapshot()
            : {
                camera: "front_door",
                available: true,
                status: "stopped",
                data: null,
              },
        });
      },
    );
    await frigateApp.goto(SETTINGS_URL);
    await page
      .getByRole("button", {
        name: "Inspect compressed motion vectors",
        exact: true,
      })
      .click();
    await expect(
      page.getByRole("button", { name: "Start vector preview", exact: true }),
    ).toBeEnabled();
    expect(starts).toBe(0);
    await page.getByLabel("Minimum vector distance", { exact: true }).fill("2");
    await page
      .getByLabel("Maximum expected distance (optional)", { exact: true })
      .fill("10");
    await page
      .getByLabel("Minimum cell activity (0 to 1)", { exact: true })
      .fill("0.01");
    await page
      .getByRole("button", { name: "Start vector preview", exact: true })
      .click();
    await expect(
      page.getByRole("status").filter({ hasText: "Preview: running" }),
    ).toBeVisible();
    await expect
      .poll(() =>
        page
          .getByTestId("compressed-vector-canvas")
          .evaluate((node: HTMLCanvasElement) => {
            const pixels = node
              .getContext("2d")!
              .getImageData(0, 0, node.width, node.height).data;
            let count = 0;
            for (let index = 0; index < pixels.length; index += 4) {
              if (pixels[index + 1] > 180 && pixels[index + 2] > 180) count++;
            }
            return count;
          }),
      )
      .toBeGreaterThan(0);
    await page
      .getByRole("button", {
        name: "Close compressed vector inspector",
        exact: true,
      })
      .click();
    await expect.poll(() => stops).toBe(1);
    expect(starts).toBe(1);
    expect(saves).toBe(0);
  });

  test("rejects invalid ranges and displays unavailable builds without starting anything", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    let starts = 0;
    await page.route(
      "**/api/front_door/compressed-motion-preview**",
      (route) => {
        if (route.request().method() === "POST") starts++;
        return route.fulfill({
          json: {
            camera: "front_door",
            available: false,
            status: "stopped",
            data: null,
          },
        });
      },
    );
    await frigateApp.goto(SETTINGS_URL);
    await page
      .getByRole("button", {
        name: "Inspect compressed motion vectors",
        exact: true,
      })
      .click();
    await expect(
      page.getByText(
        "The native vector extractor is not installed in this build.",
        { exact: true },
      ),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Start vector preview", exact: true }),
    ).toBeDisabled();
    expect(starts).toBe(0);
  });

  test("does not draw malformed or stale snapshots as current vectors", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    await page.route(
      "**/api/front_door/compressed-motion-preview**",
      (route) => {
        if (route.request().method() === "POST") {
          return route.fulfill({
            json: {
              camera: "front_door",
              session: SESSION,
              status: "warming_up",
              data: null,
            },
          });
        }
        if (route.request().method() === "DELETE")
          return route.fulfill({ json: { success: true } });
        const value = snapshot();
        value.data.vector_x = [Number.NaN];
        return route.fulfill({
          json: new URL(route.request().url()).searchParams.has("session")
            ? value
            : {
                camera: "front_door",
                available: true,
                status: "stopped",
                data: null,
              },
        });
      },
    );
    await frigateApp.goto(SETTINGS_URL);
    await page
      .getByRole("button", {
        name: "Inspect compressed motion vectors",
        exact: true,
      })
      .click();
    await page
      .getByRole("button", { name: "Start vector preview", exact: true })
      .click();
    await expect(
      page
        .getByRole("alert")
        .filter({ hasText: "Vector preview is unavailable" }),
    ).toBeVisible();
    await expect(
      page.getByText("Preview: running", { exact: true }),
    ).toHaveCount(0);
    await page
      .getByRole("button", { name: "Stop vector preview", exact: true })
      .click();
  });

  test("stops the leased preview when the document is hidden", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    let stops = 0;
    await page.route(
      "**/api/front_door/compressed-motion-preview**",
      (route) => {
        if (route.request().method() === "POST")
          return route.fulfill({
            json: {
              camera: "front_door",
              session: SESSION,
              status: "warming_up",
              data: null,
            },
          });
        if (route.request().method() === "DELETE") {
          stops++;
          return route.fulfill({ json: { success: true } });
        }
        return route.fulfill({
          json: new URL(route.request().url()).searchParams.has("session")
            ? snapshot()
            : {
                camera: "front_door",
                available: true,
                status: "stopped",
                data: null,
              },
        });
      },
    );
    await frigateApp.goto(SETTINGS_URL);
    await page
      .getByRole("button", {
        name: "Inspect compressed motion vectors",
        exact: true,
      })
      .click();
    await page
      .getByRole("button", { name: "Start vector preview", exact: true })
      .click();
    await expect(
      page.getByText("Preview: running", { exact: true }),
    ).toBeVisible();
    await page.evaluate(() => {
      Object.defineProperty(document, "hidden", {
        configurable: true,
        value: true,
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await expect.poll(() => stops).toBe(1);
  });
});
