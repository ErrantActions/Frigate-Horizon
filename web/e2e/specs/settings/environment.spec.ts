import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Page } from "@playwright/test";
import { test, expect } from "../../fixtures/frigate-test";
import { configFactory } from "../../fixtures/mock-data/config";
import {
  DISABLED_CAMERA_ENVIRONMENT,
  DISABLED_WEATHER,
} from "../../fixtures/mock-data/environment";
import type { WeatherStatus } from "../../../src/types/environment";

const CONFIG_SCHEMA = JSON.parse(
  readFileSync(
    resolve(
      dirname(fileURLToPath(import.meta.url)),
      "../../fixtures/mock-data/config-schema.json",
    ),
    "utf-8",
  ),
);
const WEATHER_URL = "/settings?page=globalWeather";
const CAMERA_URL = "/settings?page=cameraEnvironment&camera=front_door";

async function leaveSectionForSaveAll(page: Page, mobile: boolean) {
  await expect(
    page.getByRole("button", { name: "Save", exact: true }),
  ).toBeEnabled();
  if (mobile) {
    await page.getByRole("button", { name: "Go back", exact: true }).click();
  }
  await page.getByText("UI settings", { exact: true }).click();
  if (mobile) {
    await page.getByRole("button", { name: "Go back", exact: true }).click();
  }
}

async function installSettings(
  page: Page,
  manualProfile: string | null = null,
  unitSystem: "metric" | "imperial" = "metric",
) {
  const config = configFactory({
    ui: { unit_system: unitSystem, timezone: "UTC" },
    weather: {
      enabled: true,
      latitude: 52.52,
      longitude: 13.41,
      refresh_seconds: 600,
      stale_after_seconds: 1800,
    },
    profiles: {
      rain: { friendly_name: "Rain" },
      empty: { friendly_name: "Empty named profile" },
    },
    cameras: {
      front_door: {
        profiles: {
          rain: { motion: { threshold: 40 } },
          empty: { motion: { threshold: 45 } },
        },
        environment: {
          enabled: true,
          timezone: "UTC",
          manual_profile: manualProfile,
        },
      },
    },
  });
  await page.route("**/api/config", (route) => route.fulfill({ json: config }));
  await page.route("**/api/config/schema.json", (route) =>
    route.fulfill({ json: CONFIG_SCHEMA }),
  );
  await page.route("**/api/config/raw_paths", (route) =>
    route.fulfill({ json: config }),
  );
  await page.route("**/api/config/set", (route) =>
    route.fulfill({ json: { success: true, require_restart: false } }),
  );
}

function modeledWeather(): WeatherStatus {
  const sunrise = new Date();
  sunrise.setUTCHours(6, 0, 0, 0);
  const sunset = new Date();
  sunset.setUTCHours(18, 0, 0, 0);
  return {
    ...DISABLED_WEATHER,
    enabled: true,
    state: "fresh",
    data: {
      provider: "open-meteo",
      modeled: true,
      model_time: new Date().toISOString(),
      fetched_at: new Date().toISOString(),
      interval_seconds: 900,
      temperature_c: 20,
      rain_mm: 1,
      showers_mm: 0.5,
      snowfall_cm: 0,
      weather_code: 0,
      cloud_cover_percent: 85,
      wind_speed_mps: 5,
      wind_gusts_mps: 10,
      is_day: true,
      visibility_m: null,
      visibility_model_time: null,
      solar_days: [
        {
          day_start: sunrise.toISOString(),
          sunrise: sunrise.toISOString(),
          sunset: sunset.toISOString(),
        },
      ],
    },
  };
}

test.describe("Environmental settings @mobile", () => {
  test("saves a sunrise-relative camera image profile without changing day/night offsets or restarting", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    await installSettings(page);
    await frigateApp.goto(CAMERA_URL);
    await page
      .getByRole("heading", { name: "Day and night", exact: true })
      .click();
    const sunrise = page.locator("button[id$='_solar_sunrise_profile']");
    await sunrise.click();
    await page.getByRole("option", { name: "Rain", exact: true }).click();
    const start = page
      .locator("[id$='_solar_sunrise_start_minutes']")
      .getByRole("slider");
    const end = page
      .locator("[id$='_solar_sunrise_end_minutes']")
      .getByRole("slider");
    for (let step = 0; step < 5; step++) await start.press("ArrowLeft");
    for (let step = 0; step < 3; step++) await end.press("PageUp");
    const request = page.waitForRequest("**/api/config/set");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    const body = (await request).postDataJSON();
    expect(body).toMatchObject({
      requires_restart: 0,
      update_topic: "config/cameras/front_door/environment",
      config_data: {
        cameras: {
          front_door: {
            environment: {
              solar: {
                sunrise_profile: "rain",
                sunrise_start_minutes: -20,
                sunrise_end_minutes: 150,
              },
            },
          },
        },
      },
    });
    expect(body.config_data.cameras.front_door.detect).toBeUndefined();
    expect(body.config_data.cameras.front_door.record).toBeUndefined();
    expect(
      body.config_data.cameras.front_door.environment.solar
        .sunrise_offset_minutes,
    ).toBeUndefined();
    expect(
      body.config_data.cameras.front_door.environment.solar
        .sunset_offset_minutes,
    ).toBeUndefined();
  });

  for (const saveButton of ["Save", "Save All"]) {
    test(`saves a leading-zero ZIP code with ${saveButton}`, async ({
      frigateApp,
    }) => {
      await installSettings(frigateApp.page);
      if (frigateApp.isMobile && saveButton === "Save All") {
        await frigateApp.goto("/settings");
        await frigateApp.page
          .getByRole("button", { name: "Picture and environment", exact: true })
          .click();
        await frigateApp.page.getByText("Weather", { exact: true }).click();
      } else {
        await frigateApp.goto(WEATHER_URL);
      }
      const postal = frigateApp.page.getByRole("textbox", {
        name: "ZIP code",
        exact: true,
      });
      await expect(postal).toBeVisible();
      await expect(
        frigateApp.page.locator("input[id$='_latitude']"),
      ).toHaveCount(0);
      await postal.fill("00501");
      if (saveButton === "Save All") {
        await leaveSectionForSaveAll(frigateApp.page, frigateApp.isMobile);
      }
      const request = frigateApp.page.waitForRequest("**/api/config/set");
      await frigateApp.page
        .getByRole("button", { name: saveButton, exact: true })
        .click();
      expect((await request).postDataJSON()).toMatchObject({
        requires_restart: 0,
        config_data: { weather: { zip_code: "00501" } },
      });
    });
  }

  test("shows an unresolved ZIP error instead of assuming another location", async ({
    frigateApp,
  }) => {
    await installSettings(frigateApp.page);
    await frigateApp.page.route("**/api/environment/weather", (route) =>
      route.fulfill({
        json: {
          ...DISABLED_WEATHER,
          enabled: true,
          state: "unavailable",
          error_code: "weather_location_not_found",
        },
      }),
    );
    await frigateApp.goto(WEATHER_URL);
    await expect(frigateApp.page.getByRole("alert")).toContainText(
      "The ZIP code could not be matched",
    );
  });

  test("supports the backend's null UI timezone default", async ({
    frigateApp,
  }) => {
    await frigateApp.goto(WEATHER_URL);
    await expect(
      frigateApp.page.getByRole("heading", {
        name: "Modeled weather",
        exact: true,
      }),
    ).toHaveCount(2);
    await expect(
      frigateApp.page.getByRole("heading", {
        name: "This page stopped working",
      }),
    ).toHaveCount(0);
  });

  test("shows modeled measurements, unknown visibility, and solar times", async ({
    frigateApp,
  }) => {
    await installSettings(frigateApp.page);
    await frigateApp.page.route("**/api/environment/weather", (route) =>
      route.fulfill({ json: modeledWeather() }),
    );
    await frigateApp.goto(WEATHER_URL);
    const status = frigateApp.page.getByRole("region", {
      name: "Modeled weather",
    });
    await expect(
      status.getByText("Model data available", { exact: true }),
    ).toBeVisible();
    await expect(status.getByText("20 °C", { exact: true })).toBeVisible();
    await expect(status.getByText("1.5 mm", { exact: true })).toBeVisible();
    await expect(status.getByText("0 cm", { exact: true })).toBeVisible();
    await expect(
      status
        .getByText("Visibility", { exact: true })
        .locator("..")
        .getByText("Unknown"),
    ).toBeVisible();
    await expect(
      status.getByText("Sunrise", { exact: true }).locator(".."),
    ).toContainText(/6:00|06:00/);
    await expect(
      status.getByText(
        "Open-Meteo model data, not a local weather-station observation.",
      ),
    ).toBeVisible();
  });

  test("saves weather without requesting a restart", async ({ frigateApp }) => {
    await installSettings(frigateApp.page);
    await frigateApp.goto(WEATHER_URL);
    await frigateApp.page
      .getByRole("button", { name: /Advanced Settings/ })
      .click();
    await frigateApp.page.locator("input[id$='_latitude']").fill("53.123456");
    const request = frigateApp.page.waitForRequest("**/api/config/set");
    await frigateApp.page
      .getByRole("button", { name: "Save", exact: true })
      .click();
    expect((await request).postDataJSON()).toMatchObject({
      requires_restart: 0,
      config_data: { weather: { latitude: 53.123456 } },
    });
  });

  test("switches weather units and preserves the display preference", async ({
    frigateApp,
  }) => {
    await installSettings(frigateApp.page);
    await frigateApp.page.route("**/api/environment/weather", (route) =>
      route.fulfill({ json: modeledWeather() }),
    );
    const writes: string[] = [];
    frigateApp.page.on("request", (request) => {
      if (
        request.method() !== "GET" &&
        request.url().includes("/api/config/")
      ) {
        writes.push(request.url());
      }
    });
    await frigateApp.goto(WEATHER_URL);
    const status = frigateApp.page.getByRole("region", {
      name: "Modeled weather",
    });
    const units = status.getByRole("combobox", {
      name: "Weather display units",
    });
    await expect(units).toBeEnabled();
    await expect(status.getByText("20 °C", { exact: true })).toBeVisible();
    await units.click();
    await frigateApp.page
      .getByRole("option", { name: "U.S. customary", exact: true })
      .click();
    await expect(status.getByText("68 °F", { exact: true })).toBeVisible();
    await expect(status.getByText("0.06 in", { exact: true })).toBeVisible();
    await expect(status.getByText("11.18 mph", { exact: true })).toBeVisible();
    await expect(status.getByText("22.37 mph", { exact: true })).toBeVisible();
    await expect(
      status.getByText("Visibility", { exact: true }).locator(".."),
    ).toContainText("Unknown");
    await frigateApp.goto(WEATHER_URL);
    await expect(units).toContainText("U.S. customary");
    await expect(status.getByText("68 °F", { exact: true })).toBeVisible();
    await units.click();
    await frigateApp.page
      .getByRole("option", { name: "Metric", exact: true })
      .click();
    await expect(status.getByText("20 °C", { exact: true })).toBeVisible();
    await expect(status.getByText("5 m/s", { exact: true })).toBeVisible();
    await expect(status.getByText("1.5 mm", { exact: true })).toBeVisible();
    await units.click();
    await frigateApp.page
      .getByRole("option", { name: "System default", exact: true })
      .click();
    await expect(status.getByText("20 °C", { exact: true })).toBeVisible();
    expect(writes).toEqual([]);
  });

  test("system default follows global imperial weather units", async ({
    frigateApp,
  }) => {
    await installSettings(frigateApp.page, null, "imperial");
    const weather = modeledWeather();
    if (!weather.data) {
      throw new Error("Expected the modeled weather fixture");
    }
    weather.data.visibility_m = 1609.344;
    weather.data.snowfall_cm = 2.54;
    await frigateApp.page.route("**/api/environment/weather", (route) =>
      route.fulfill({ json: weather }),
    );
    await frigateApp.goto(WEATHER_URL);
    const status = frigateApp.page.getByRole("region", {
      name: "Modeled weather",
    });
    const units = status.getByRole("combobox", {
      name: "Weather display units",
    });
    await expect(units).toBeEnabled();
    await expect(units).toContainText("System default");
    await expect(status.getByText("68 °F", { exact: true })).toBeVisible();
    await expect(status.getByText("1 mi", { exact: true })).toBeVisible();
    await expect(status.getByText("1 in", { exact: true })).toBeVisible();
    await units.click();
    await frigateApp.page
      .getByRole("option", { name: "Metric", exact: true })
      .click();
    await expect(status.getByText("20 °C", { exact: true })).toBeVisible();
    await expect(status.getByText("2.54 cm", { exact: true })).toBeVisible();
    await expect(status.getByText("1,609.34 m", { exact: true })).toBeVisible();
  });

  test("keeps literal empty profile names distinct from Automatic", async ({
    frigateApp,
  }) => {
    await installSettings(frigateApp.page);
    await frigateApp.goto(CAMERA_URL);
    const selector = frigateApp.page.locator("button[id$='_manual_profile']");
    await expect(selector).toContainText("Automatic");
    await selector.click();
    await frigateApp.page
      .getByRole("option", { name: "Empty named profile", exact: true })
      .click();
    const request = frigateApp.page.waitForRequest("**/api/config/set");
    await frigateApp.page
      .getByRole("button", { name: "Save", exact: true })
      .click();
    expect((await request).postDataJSON()).toMatchObject({
      requires_restart: 0,
      update_topic: "config/cameras/front_door/environment",
      config_data: {
        cameras: { front_door: { environment: { manual_profile: "empty" } } },
      },
    });
  });

  test("Save All keeps global weather updates live", async ({ frigateApp }) => {
    await installSettings(frigateApp.page);
    await frigateApp.goto("/settings");
    await frigateApp.page
      .getByRole("button", { name: "Picture and environment", exact: true })
      .click();
    await frigateApp.page.getByText("Weather", { exact: true }).click();
    await frigateApp.page
      .getByRole("button", { name: /Advanced Settings/ })
      .click();
    await frigateApp.page.locator("input[id$='_longitude']").fill("-14.654321");
    await leaveSectionForSaveAll(frigateApp.page, frigateApp.isMobile);
    const request = frigateApp.page.waitForRequest("**/api/config/set");
    await frigateApp.page
      .getByRole("button", { name: "Save All", exact: true })
      .click();
    expect((await request).postDataJSON()).toMatchObject({
      requires_restart: 0,
      config_data: { weather: { longitude: -14.654321 } },
    });
  });

  for (const saveButton of ["Save", "Save All"]) {
    test(`clears a manual override with ${saveButton}`, async ({
      frigateApp,
    }) => {
      await installSettings(frigateApp.page, "rain");
      if (frigateApp.isMobile && saveButton === "Save All") {
        await frigateApp.goto("/settings");
        await frigateApp.page
          .getByRole("button", { name: "Front Door", exact: true })
          .click();
        await frigateApp.page
          .getByRole("button", {
            name: "Front Door: Picture and environment",
            exact: true,
          })
          .click();
        await frigateApp.page
          .getByRole("button", {
            name: "Front Door: Environment and profiles",
            exact: true,
          })
          .click();
      } else {
        await frigateApp.goto(CAMERA_URL);
      }
      const selector = frigateApp.page.locator("button[id$='_manual_profile']");
      await expect(selector).toContainText("Rain");
      await selector.click();
      await frigateApp.page
        .getByRole("option", { name: "Automatic", exact: true })
        .click();
      if (saveButton === "Save All") {
        await leaveSectionForSaveAll(frigateApp.page, frigateApp.isMobile);
      }
      const request = frigateApp.page.waitForRequest("**/api/config/set");
      await frigateApp.page
        .getByRole("button", { name: saveButton, exact: true })
        .click();
      expect((await request).postDataJSON()).toMatchObject({
        requires_restart: 0,
        config_data: {
          cameras: { front_door: { environment: { manual_profile: "" } } },
        },
      });
    });
  }

  test("shows UTC and does not offer a null browser timezone for schedules", async ({
    frigateApp,
  }) => {
    await installSettings(frigateApp.page);
    await frigateApp.goto(CAMERA_URL);
    const selector = frigateApp.page.locator("button[id$='_timezone']");
    await expect(selector).toContainText("UTC");
    await selector.click();
    await expect(
      frigateApp.page.getByRole("option", { name: "UTC", exact: true }),
    ).toBeVisible();
    await expect(
      frigateApp.page
        .getByRole("option")
        .filter({ hasText: /browser|default/i }),
    ).toHaveCount(0);
  });

  test("distinguishes desired profiles from preserved off switches and stale weather", async ({
    frigateApp,
  }) => {
    await installSettings(frigateApp.page);
    await frigateApp.page.route(
      "**/api/environment/cameras/front_door",
      (route) =>
        route.fulfill({
          json: {
            ...DISABLED_CAMERA_ENVIRONMENT,
            phase: "applied",
            applied_profiles: ["rain"],
            blocked_fields: ["record.enabled"],
            desired: {
              ...DISABLED_CAMERA_ENVIRONMENT.desired,
              profiles: ["rain"],
              reason: "manual_override",
              warnings: ["weather_stale"],
            },
          },
        }),
    );
    await frigateApp.goto(CAMERA_URL);
    const status = frigateApp.page.getByRole("region", {
      name: "Camera environment and profiles",
    });
    await expect(
      status.getByText("Manual override", { exact: true }),
    ).toBeVisible();
    await expect(
      status.getByText("Explicit off settings were preserved: Recording."),
    ).toBeVisible();
    await expect(
      status.getByText(
        "Weather rules are suspended because model data is stale.",
      ),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText(
        /physical-camera image controls are still under development/,
      ),
    ).toBeVisible();
  });

  test("does not invent fair weather when the backend lacks status support", async ({
    frigateApp,
  }) => {
    await installSettings(frigateApp.page);
    await frigateApp.page.route("**/api/environment/weather", (route) =>
      route.fulfill({ json: {} }),
    );
    await frigateApp.goto(WEATHER_URL);
    const status = frigateApp.page.getByRole("region", {
      name: "Modeled weather",
    });
    await expect(status.getByRole("alert")).toHaveText(
      /Environmental status could not be loaded/,
    );
    await expect(
      status.getByText("Model data available", { exact: true }),
    ).toHaveCount(0);
    await expect(status.getByText("0 mm", { exact: true })).toHaveCount(0);
  });
});
