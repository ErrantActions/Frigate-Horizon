import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Page } from "@playwright/test";
import { test, expect } from "../../fixtures/frigate-test";
import { configFactory } from "../../fixtures/mock-data/config";

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

async function install(page: Page) {
  const config = configFactory({
    cameras: { front_door: { onvif: { host: "camera.invalid" } } },
  });
  await page.route("**/api/config", (route) => route.fulfill({ json: config }));
  await page.route("**/api/config/raw_paths", (route) =>
    route.fulfill({ json: config }),
  );
  await page.route("**/api/config/schema.json", (route) =>
    route.fulfill({ json: schema }),
  );
  const updates: Record<string, unknown>[] = [];
  let integration = false;
  let enabled = false;
  await page.route("**/api/onvif/cameras/front_door/events", (route) => {
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON();
      updates.push(body);
      enabled = body.enabled;
      integration = body.integrate_events;
    }
    return route.fulfill({
      json: {
        state: enabled ? "subscribed" : "stopped",
        integration: integration ? "manual_events" : "status_history_only",
        events: [],
        failures: 0,
        signals: integration
          ? {
              active: 0,
              starts: 0,
              ends: 0,
              expiry_seconds: 30,
              error_code: null,
            }
          : null,
      },
    });
  });
  await page.route("**/api/onvif/cameras/front_door/events/sources", (route) =>
    route.fulfill({
      json: {
        profiles: [
          { token: "main", name: "Main", video_source_token: "sensor" },
        ],
      },
    }),
  );
  return updates;
}

test.describe("ONVIF external event opt-in @mobile", () => {
  test("native subscription remains status-only by default", async ({
    frigateApp,
  }) => {
    const updates = await install(frigateApp.page);
    await frigateApp.goto(SETTINGS);
    await expect(
      frigateApp.page.getByLabel("Opt in this runtime subscription", {
        exact: false,
      }),
    ).not.toBeChecked();
    await frigateApp.page
      .getByRole("button", { name: "Read subscription status", exact: true })
      .click();
    await expect(
      frigateApp.page.getByText(
        "Status/history only; NVR integration is disabled.",
        { exact: false },
      ),
    ).toBeVisible();
    await frigateApp.page
      .getByLabel("I authorize starting or stopping this camera", {
        exact: false,
      })
      .check();
    await frigateApp.page
      .getByRole("button", { name: "Start native events", exact: true })
      .click();
    await expect.poll(() => updates).toHaveLength(1);
    expect(updates[0]).toMatchObject({
      enabled: true,
      integrate_events: false,
    });
  });

  test("integration needs a separate opt-in and physical source, and clears on stop", async ({
    frigateApp,
  }) => {
    const updates = await install(frigateApp.page);
    await frigateApp.goto(SETTINGS);
    const optIn = frigateApp.page.getByLabel(
      "Opt in this runtime subscription",
      { exact: false },
    );
    await optIn.check();
    await expect(
      frigateApp.page.getByRole("button", {
        name: "Start native events",
        exact: true,
      }),
    ).toBeDisabled();
    await frigateApp.page
      .getByRole("button", {
        name: "Read native event source profiles",
        exact: true,
      })
      .click();
    await frigateApp.page
      .getByLabel("Native event physical source media profile", { exact: true })
      .selectOption("main");
    await frigateApp.page
      .getByLabel("I authorize starting or stopping this camera", {
        exact: false,
      })
      .check();
    await frigateApp.page
      .getByRole("button", { name: "Start native events", exact: true })
      .click();
    await expect.poll(() => updates).toHaveLength(1);
    expect(updates[0]).toMatchObject({
      enabled: true,
      integrate_events: true,
      source_profile_token: "main",
      event_expiry_seconds: 30,
    });
    await expect(
      frigateApp.page.getByText(
        "External ONVIF NVR integration is enabled for this subscription.",
        { exact: false },
      ),
    ).toBeVisible();
    await frigateApp.page
      .getByLabel("I authorize starting or stopping this camera", {
        exact: false,
      })
      .check();
    await frigateApp.page
      .getByRole("button", { name: "Stop native events", exact: true })
      .click();
    await expect.poll(() => updates).toHaveLength(2);
    expect(updates[1]).toMatchObject({
      enabled: false,
      integrate_events: false,
    });
    await expect(optIn).not.toBeChecked();
  });
});
