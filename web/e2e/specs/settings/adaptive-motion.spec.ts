import type { Page } from "@playwright/test";
import { test, expect } from "../../fixtures/frigate-test";
import type {
  AdaptiveMotionField,
  AdaptiveMotionSnapshot,
} from "../../../src/types/adaptiveMotion";

const URL = "/settings?page=motionTuner&camera=front_door";
const VALUES: Record<AdaptiveMotionField, number[]> = {
  adjustment: [1.5, -2, 3, 4],
  activity: [11, 22, 33, 44],
  effective: [25, 25, 25, 25],
  p: [0.5, 1, 2, 3],
  i: [0.25, 0.5, 0.75, 1],
  d: [-0.5, -1, 0, 1],
};

function snapshot(field: AdaptiveMotionField): AdaptiveMotionSnapshot {
  return {
    camera: "front_door",
    mode: "shadow",
    status: "learning",
    updated_at: Date.now() / 1000,
    baseline_threshold: 25,
    max_adjustment: 10,
    applied: false,
    positive_holds: 2,
    warning: null,
    error: null,
    profile: {
      window_index: 15,
      start_minute: 450,
      end_minute: 480,
      timezone: "America/Chicago",
      gust_band_mph: 20,
      wind_direction_sector: 3,
      weather_state: "modeled",
      weather_observed_at: Date.now() / 1000,
    },
    grid: {
      columns: 4,
      rows: 4,
      analysis_width: 320,
      analysis_height: 180,
      display_columns: 2,
      display_rows: 2,
      values: VALUES[field],
      field,
    },
  };
}

async function installImage(page: Page) {
  await page.route("**/api/front_door?fps=5&height=720", (route) =>
    route.fulfill({
      contentType: "image/svg+xml",
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540"><rect width="960" height="540" fill="#333"/></svg>',
    }),
  );
  await page.route("**/api/front_door/latest.webp?**", (route) =>
    route.fulfill({
      contentType: "image/svg+xml",
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540"><rect width="960" height="540" fill="#333"/></svg>',
    }),
  );
}

test.describe("Adaptive motion inspector @mobile", () => {
  test("toggles live stream, grid and heatmap independently with full matrix extrema", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    await installImage(page);
    await page.route("**/api/front_door/adaptive-motion?**", (route) => {
      const data = snapshot("adjustment");
      data.grid.minimum = -9;
      data.grid.maximum = 20;
      data.grid.values = [1, 1, 1, 1];
      return route.fulfill({ json: data });
    });
    await frigateApp.goto(URL);
    await page
      .getByRole("button", { name: "Inspect adaptive motion", exact: true })
      .click();
    await expect(page.getByTestId("adaptive-matrix-extrema")).toHaveText(
      "Full matrix minimum: -9. Maximum: 20.",
    );
    const canvas = page.getByTestId("adaptive-motion-canvas");
    const alpha = () =>
      canvas.evaluate((element: HTMLCanvasElement) => {
        const values = element
          .getContext("2d")!
          .getImageData(0, 0, element.width, element.height).data;
        let sum = 0;
        for (let index = 3; index < values.length; index += 4)
          sum += values[index];
        return sum;
      });
    await expect.poll(alpha).toBeGreaterThan(0);
    await page
      .getByRole("checkbox", { name: "Show grid", exact: true })
      .uncheck();
    await page
      .getByRole("checkbox", { name: "Show heatmap", exact: true })
      .uncheck();
    await expect.poll(alpha).toBe(0);
    await page
      .getByRole("checkbox", { name: "Show grid", exact: true })
      .check();
    await expect.poll(alpha).toBeGreaterThan(0);
    await page
      .getByRole("checkbox", { name: "Show grid", exact: true })
      .uncheck();
    await page
      .getByRole("checkbox", { name: "Show heatmap", exact: true })
      .check();
    await expect.poll(alpha).toBeGreaterThan(0);
    await expect(
      page.getByRole("img", { name: "Live image for front_door" }),
    ).toHaveAttribute("src", /front_door\?fps=5&height=720$/);
    await page
      .getByRole("checkbox", { name: "Live stream", exact: true })
      .uncheck();
    await expect(
      page.getByRole("img", { name: "Live image for front_door" }),
    ).toHaveAttribute("src", /latest\.webp/);
  });

  test("is read-only and opt-in, maps native cells, and draws selected field values", async ({
    frigateApp,
  }, testInfo) => {
    const { page } = frigateApp;
    let requests = 0;
    await installImage(page);
    await page.addInitScript(() => {
      const original = CanvasRenderingContext2D.prototype.fillText;
      CanvasRenderingContext2D.prototype.fillText = function (
        text,
        x,
        y,
        maxWidth,
      ) {
        this.canvas.dataset.lastLabels = `${
          this.canvas.dataset.lastLabels ?? ""
        }|${text}`;
        if (maxWidth === undefined) original.call(this, text, x, y);
        else original.call(this, text, x, y, maxWidth);
      };
    });
    await page.route("**/api/front_door/adaptive-motion?**", (route) => {
      requests++;
      expect(route.request().method()).toBe("GET");
      const params = new globalThis.URL(route.request().url()).searchParams;
      expect(params.get("resolution")).toBe("64");
      const field = params.get("field") as AdaptiveMotionField;
      const data = snapshot(field);
      if (params.has("column")) {
        data.cell = {
          column: Number(params.get("column")),
          row: Number(params.get("row")),
          adjustment: 4,
          applied_adjustment: 0,
          effective_threshold: 25,
          activity: 44,
          episode_count: 7,
          p: 3,
          i: 1,
          d: 1,
          hold_remaining_seconds: 12,
          quiet_seconds: 6,
          decay_rate: 0.25,
        };
      }
      return route.fulfill({ json: data });
    });
    await frigateApp.goto(URL);
    const open = page.getByRole("button", {
      name: "Inspect adaptive motion",
      exact: true,
    });
    await expect(open).toBeVisible();
    expect(requests).toBe(0);
    await open.click();
    await expect(
      page.getByText("Mode: Shadow (learning only). Status: Learning."),
    ).toBeVisible();
    await expect(
      page.getByText("Adjustments are not applied to motion detection.", {
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      page.getByText(/Active half-hour profile 15: 07:30 to 08:00/),
    ).toBeVisible();
    await expect(
      page.getByText(/gust band 20 mph, direction sector 3/),
    ).toBeVisible();
    await expect(
      page.getByText(/Weather state: Modeled weather estimate/),
    ).toBeVisible();
    await expect(
      page.getByText(
        /In shadow mode, the effective threshold remains the baseline threshold/,
      ),
    ).toBeVisible();
    await expect(
      page.getByText(
        "Analysis image: 320 × 180 pixels. Native grid: 4 × 4 cells. Display grid: 2 × 2 cells.",
      ),
    ).toBeVisible();
    const canvas = page.getByTestId("adaptive-motion-canvas");
    const image = page.getByRole("img", { name: "Live image for front_door" });
    await expect(image).toBeVisible();
    await expect
      .poll(() =>
        image.evaluate((element: HTMLImageElement) => element.naturalWidth),
      )
      .toBe(960);
    const bounds = await canvas.boundingBox();
    expect(bounds).not.toBeNull();
    expect(bounds!.width / bounds!.height).toBeCloseTo(16 / 9, 1);
    await page.getByLabel("Show overview averages", { exact: true }).check();
    await expect(canvas).toHaveAttribute("data-last-labels", /\|1.5\|/);
    await canvas.click({
      position: { x: bounds!.width * 0.875, y: bounds!.height * 0.875 },
    });
    const cell = page.getByTestId("adaptive-motion-cell");
    await expect(
      cell.getByRole("heading", { name: "Cell: column 3, row 3" }),
    ).toBeVisible();
    await expect(
      cell
        .locator("div")
        .filter({
          has: page.locator("dt", { hasText: /^Applied adjustment$/ }),
        })
        .locator("dd"),
    ).toHaveText("0");
    await expect(
      cell
        .locator("div")
        .filter({
          has: page.locator("dt", { hasText: /^Recommended adjustment$/ }),
        })
        .locator("dd"),
    ).toHaveText("4");

    for (const field of ["activity", "effective", "p", "i", "d"] as const) {
      await canvas.evaluate((element) => {
        delete (element as HTMLCanvasElement).dataset.lastLabels;
      });
      await page
        .getByRole("combobox", { name: "Heatmap field" })
        .selectOption(field);
      await expect(canvas).toHaveAttribute(
        "data-last-labels",
        new RegExp(`\\|${VALUES[field][0]}\\|`),
      );
      await expect(
        page.getByLabel(
          "Overview average scale (blue negative, transparent zero, orange positive)",
        ),
      ).toContainText(
        field === "activity"
          ? "Activity"
          : field === "effective"
            ? "Effective threshold"
            : field === "p"
              ? "Proportional contribution"
              : field === "i"
                ? "Integral contribution"
                : "Derivative contribution",
      );
    }
    await page
      .getByRole("combobox", { name: "Heatmap field" })
      .selectOption("adjustment");
    await expect(canvas).toHaveAttribute("data-last-labels", /\|1.5\|/);
    await canvas.scrollIntoViewIfNeeded();
    const screenshot = testInfo.outputPath(
      `adaptive-motion-${testInfo.project.name}.png`,
    );
    await page.screenshot({ path: screenshot, fullPage: true });
    await testInfo.attach(`Adaptive motion (${testInfo.project.name})`, {
      path: screenshot,
      contentType: "image/png",
    });
    await page
      .getByRole("button", { name: "Close adaptive motion inspector" })
      .click();
    await expect(canvas).toHaveCount(0);
  });

  test("maps an averaged overview to aspect-scaled 889 by 500 native cells", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    await installImage(page);
    let requestedCell: { column: number; row: number } | undefined;
    await page.route("**/api/front_door/adaptive-motion?**", (route) => {
      const params = new globalThis.URL(route.request().url()).searchParams;
      const data = snapshot("adjustment");
      data.grid.columns = 889;
      data.grid.rows = 500;
      data.grid.display_columns = 64;
      data.grid.display_rows = 64;
      data.grid.values = Array<number>(64 * 64).fill(1.5);
      if (params.has("column")) {
        requestedCell = {
          column: Number(params.get("column")),
          row: Number(params.get("row")),
        };
        data.cell = {
          ...requestedCell,
          adjustment: 4.75,
          applied_adjustment: 0,
          effective_threshold: 25,
          activity: 44,
          episode_count: 7,
          p: 3,
          i: 1,
          d: 0.75,
          hold_remaining_seconds: 12,
          quiet_seconds: 6,
          decay_rate: 0.25,
        };
      }
      return route.fulfill({ json: data });
    });
    await frigateApp.goto(URL);
    await page
      .getByRole("button", { name: "Inspect adaptive motion", exact: true })
      .click();
    await expect(
      page.getByText(
        "Analysis image: 320 × 180 pixels. Native grid: 889 × 500 cells. Display grid: 64 × 64 cells.",
      ),
    ).toBeVisible();
    await expect(
      page.getByText(
        /describe area-averaged display cells, not individual native-grid cells/,
      ),
    ).toBeVisible();
    await expect(
      page.getByLabel(
        "Overview average scale (blue negative, transparent zero, orange positive)",
      ),
    ).toContainText("Recommended adjustment (overview averages)");
    await expect
      .poll(() =>
        page
          .getByRole("img", { name: "Live image for front_door" })
          .evaluate((element: HTMLImageElement) => element.naturalWidth),
      )
      .toBe(960);
    const canvas = page.getByTestId("adaptive-motion-canvas");
    await canvas.scrollIntoViewIfNeeded();
    const bounds = (await canvas.boundingBox())!;
    // Integer mouse coordinates make subpixel native cells deterministic on mobile.
    const x = Math.floor(bounds.x + bounds.width * 0.75) - bounds.x;
    const y = Math.floor(bounds.y + bounds.height * 0.25) - bounds.y;
    const expected = {
      column: Math.floor((x / bounds.width) * 889),
      row: Math.floor((y / bounds.height) * 500),
    };
    expect(expected.column).toBeGreaterThan(64);
    expect(expected.row).toBeGreaterThan(64);
    await canvas.click({ position: { x, y } });
    await expect.poll(() => requestedCell).toEqual(expected);
    const cell = page.getByTestId("adaptive-motion-cell");
    await expect(
      cell.getByRole("heading", {
        name: `Cell: column ${expected.column}, row ${expected.row}`,
      }),
    ).toBeVisible();
    await expect(
      cell.getByText(
        "These values come from the selected native cell, not the overview average. Numbers are rounded for display.",
      ),
    ).toBeVisible();
    await expect(
      cell
        .locator("div")
        .filter({
          has: page.locator("dt", { hasText: /^Recommended adjustment$/ }),
        })
        .locator("dd"),
    ).toHaveText("4.75");
    await page
      .getByRole("spinbutton", { name: "Column", exact: true })
      .fill("888");
    await expect.poll(() => requestedCell?.column).toBe(888);
  });

  test.describe("intentional adaptive-motion endpoint 404", () => {
    // Chromium omits the URL from this diagnostic. Limit the exact message
    // allowance to the single test that intentionally returns this endpoint's 404.
    test.use({
      expectedErrors: [
        /^Failed to load resource: the server responded with a status of 404 \(Not Found\)$/,
      ],
    });

    test("distinguishes applied state, stale reports, errors, and missing API", async ({
      frigateApp,
    }) => {
      const { page } = frigateApp;
      let state: "active" | "stale" | "error" | "missing" = "active";
      await installImage(page);
      await page.route("**/api/front_door/adaptive-motion?**", (route) => {
        if (state === "missing")
          return route.fulfill({ status: 404, json: { detail: "Not found" } });
        const data = snapshot("adjustment");
        data.mode = "active";
        data.applied = state === "active";
        data.status = state === "active" ? "learning" : state;
        data.warning = state === "stale" ? "baseline_retest" : null;
        data.error = state === "error" ? "adaptive_profile_unavailable" : null;
        data.profile!.weather_state = state === "stale" ? "stale" : "unknown";
        return route.fulfill({ json: data });
      });
      await frigateApp.goto(URL);
      await page
        .getByRole("button", { name: "Inspect adaptive motion", exact: true })
        .click();
      await expect(
        page.getByText("Adjustments are applied to motion detection.", {
          exact: true,
        }),
      ).toBeVisible();
      state = "stale";
      await expect(
        page.getByText("Mode: Active. Status: Stale."),
      ).toBeVisible();
      await expect(
        page.getByText(
          "Warning: The baseline threshold is being retested. Treat current estimates as provisional.",
        ),
      ).toBeVisible();
      await expect(
        page.getByText(/Weather state: Outdated weather data/),
      ).toBeVisible();
      await expect(
        page.getByText("Adjustments are not applied to motion detection.", {
          exact: true,
        }),
      ).toBeVisible();
      state = "error";
      await expect(
        page.getByText(
          "Analysis error: The adaptive motion learning profile is unavailable. No current cell analysis can be confirmed.",
        ),
      ).toBeVisible();
      state = "missing";
      await expect(
        page.getByText(/unavailable on this server \(HTTP 404\)/),
      ).toBeVisible();
      await expect(
        page.getByText("Mode: Disabled. Status: Disabled."),
      ).toHaveCount(0);
    });
  });

  test("translates runtime warning codes and safely handles unknown codes", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    const warnings = [
      [
        "grid_finer_than_motion_analysis",
        "The grid is finer than the motion analysis image. Neighboring cells may share the same image evidence.",
      ],
      [
        "inherited_grid_estimates",
        "Some cells use inherited estimates rather than independently learned values.",
      ],
      [
        "baseline_retest",
        "The baseline threshold is being retested. Treat current estimates as provisional.",
      ],
      [
        "camera_alignment_unavailable",
        "Learning is paused because a stable camera background cannot be verified.",
      ],
      [
        "camera_view_changed",
        "The camera view changed. Previous spatial history is preserved, and learning starts fresh for the new view.",
      ],
      [
        "persistent_local_motion",
        "Repeated local motion was detected. Inspect for webs or a lens obstruction. This is not a confirmed spider classification.",
      ],
      [
        "future_warning_code",
        "Adaptive motion reported a warning. Current estimates may be provisional.",
      ],
    ] as const;
    let warning: string = warnings[0][0];
    await installImage(page);
    await page.route("**/api/front_door/adaptive-motion?**", (route) => {
      const data = snapshot("adjustment");
      data.warning = warning;
      if (warning === "camera_alignment_unavailable") data.status = "paused";
      data.error = "future_error_code";
      data.profile!.weather_state = "future_weather_code";
      return route.fulfill({ json: data });
    });
    await frigateApp.goto(URL);
    await page
      .getByRole("button", { name: "Inspect adaptive motion", exact: true })
      .click();
    await expect(
      page.getByText(
        /Frigate reads the camera frame dimensions and calculates cell size/,
      ),
    ).toBeVisible();
    await expect(
      page.getByText(
        /Motion analysis scales automatically when adaptive motion is enabled/,
      ),
    ).toBeVisible();
    await expect(
      page.getByText(/Larger grids increase processing and memory use/),
    ).toBeVisible();
    for (const [code, message] of warnings) {
      warning = code;
      await expect(
        page.getByText(`Warning: ${message}`, { exact: true }),
      ).toBeVisible();
      await expect(page.getByText(code, { exact: true })).toHaveCount(0);
      if (code === "camera_alignment_unavailable") {
        await expect(
          page.getByText("Mode: Shadow (learning only). Status: Paused."),
        ).toBeVisible();
      }
    }
    await expect(
      page.getByText(
        "Analysis error: Adaptive motion analysis is unavailable. Check system logs for details.",
      ),
    ).toBeVisible();
    await expect(page.getByText(/Weather state: Unknown/)).toBeVisible();
    await expect(
      page.getByText("future_error_code", { exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByText("future_weather_code", { exact: true }),
    ).toHaveCount(0);
  });

  test("suspends polling while hidden and after closing", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    let requests = 0;
    await installImage(page);
    await page.route("**/api/front_door/adaptive-motion?**", (route) => {
      requests++;
      return route.fulfill({ json: snapshot("adjustment") });
    });
    await frigateApp.goto(URL);
    await page.clock.install();
    await page
      .getByRole("button", { name: "Inspect adaptive motion", exact: true })
      .click();
    await expect(
      page.getByText("Mode: Shadow (learning only). Status: Learning."),
    ).toBeVisible();
    await page.evaluate(() => {
      Object.defineProperty(document, "hidden", {
        configurable: true,
        value: true,
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    const hiddenCount = requests;
    await page.clock.fastForward(10_000);
    expect(requests).toBe(hiddenCount);
    await page.evaluate(() => {
      Object.defineProperty(document, "hidden", {
        configurable: true,
        value: false,
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await expect.poll(() => requests).toBe(hiddenCount + 1);
    await page
      .getByRole("button", { name: "Close adaptive motion inspector" })
      .click();
    const closedCount = requests;
    await page.clock.fastForward(10_000);
    expect(requests).toBe(closedCount);
  });
});
