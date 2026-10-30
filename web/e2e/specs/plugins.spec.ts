import { test, expect } from "../fixtures/frigate-test";
import { viewerProfile } from "../fixtures/mock-data/profile";

const ringManifest = {
  id: "ring",
  name: "Ring",
  version: "0.1.0",
  actions: ["authenticate", "disconnect"],
};

test.describe("Plugins @medium", () => {
  test("bundled install stays separate from developer mode", async ({
    frigateApp,
  }) => {
    let installed = false;
    await frigateApp.page.route("**/api/plugins", (route) =>
      route.fulfill({
        json: {
          developer_mode: false,
          catalog: [ringManifest],
          installed: installed
            ? [
                {
                  manifest: ringManifest,
                  enabled: false,
                  developer: false,
                  state: "disabled",
                  bindings: {},
                  devices: [],
                },
              ]
            : [],
        },
      }),
    );
    await frigateApp.page.route("**/api/plugins/install", async (route) => {
      expect(route.request().postDataJSON()).toEqual({ bundled_ring: true });
      installed = true;
      await route.fulfill({ json: { success: true } });
    });
    await frigateApp.goto("/settings?page=plugins");
    await expect(
      frigateApp.page
        .getByRole("region", { name: "Plugins", exact: true })
        .getByRole("heading", { name: "Plugins", exact: true }),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByRole("button", {
        name: "Enable developer mode",
        exact: true,
      }),
    ).toBeDisabled();
    await frigateApp.page
      .getByRole("button", { name: "Install", exact: true })
      .click();
    await expect(
      frigateApp.page.getByRole("button", { name: "Enable", exact: true }),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByRole("button", { name: "Install", exact: true }),
    ).toBeDisabled();
  });

  test("developer packages require explicit trust and a checksum", async ({
    frigateApp,
  }) => {
    let developer = false;
    await frigateApp.page.route("**/api/plugins", (route) =>
      route.fulfill({
        json: {
          developer_mode: developer,
          catalog: [ringManifest],
          installed: [],
        },
      }),
    );
    await frigateApp.page.route(
      "**/api/plugins/developer-mode",
      async (route) => {
        expect(route.request().postDataJSON()).toEqual({
          enabled: true,
          acknowledge_code_execution: true,
        });
        developer = true;
        await route.fulfill({ json: { success: true } });
      },
    );
    await frigateApp.goto("/settings?page=plugins");
    await frigateApp.page
      .getByRole("checkbox", {
        name: "I understand and accept that this plugin can execute code inside Frigate.",
      })
      .check();
    await frigateApp.page
      .getByRole("button", { name: "Enable developer mode", exact: true })
      .click();
    await expect(
      frigateApp.page.getByLabel("Public HTTPS ZIP package URL"),
    ).toBeVisible();
    const install = frigateApp.page.getByRole("button", {
      name: "Install developer plugin",
    });
    await expect(install).toBeDisabled();
    await frigateApp.page
      .getByLabel("Public HTTPS ZIP package URL")
      .fill("https://example.com/plugin.zip");
    await frigateApp.page.getByLabel("Expected SHA-256").fill("a".repeat(64));
    await expect(install).toBeEnabled();
  });

  test("Ring authentication requests 2FA without reporting a recording as healthy", async ({
    frigateApp,
  }) => {
    await frigateApp.page.route("**/api/plugins", (route) =>
      route.fulfill({
        json: {
          developer_mode: false,
          catalog: [ringManifest],
          installed: [
            {
              manifest: ringManifest,
              enabled: true,
              developer: false,
              state: "auth_required",
              bindings: {},
              devices: [],
            },
          ],
        },
      }),
    );
    await frigateApp.page.route(
      "**/api/plugins/ring/actions",
      async (route) => {
        expect(route.request().postDataJSON()).toEqual({
          method: "authenticate",
          params: {
            email: "test@example.com",
            password: "test-password",
            code: "",
          },
        });
        await route.fulfill({ json: { state: "two_factor_required" } });
      },
    );
    await frigateApp.goto("/settings?page=plugins");
    await frigateApp.page.getByLabel("Ring email").fill("test@example.com");
    await frigateApp.page.getByLabel("Ring password").fill("test-password");
    await frigateApp.page
      .getByRole("button", { name: "Connect Ring", exact: true })
      .click();
    await expect(
      frigateApp.page.getByText(
        "Enter the verification code sent by Ring, then connect again.",
      ),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText("Recording segments are fresh."),
    ).toHaveCount(0);
  });

  test("creating a camera requires cloud-gap acknowledgement", async ({
    frigateApp,
  }) => {
    await frigateApp.page.route("**/api/plugins", (route) =>
      route.fulfill({
        json: {
          developer_mode: false,
          catalog: [ringManifest],
          installed: [
            {
              manifest: ringManifest,
              enabled: true,
              developer: false,
              state: "running",
              bindings: {},
              devices: [{ id: "123", name: "Front Door", two_way_audio: true }],
            },
          ],
        },
      }),
    );
    await frigateApp.goto("/settings?page=plugins");
    await frigateApp.page
      .getByLabel("New camera identifier")
      .fill("ring_front");
    const add = frigateApp.page.getByRole("button", {
      name: "Add camera and record continuously",
    });
    await expect(add).toBeDisabled();
    await frigateApp.page
      .getByRole("checkbox", {
        name: "I accept best-effort cloud recording and the possibility of gaps.",
      })
      .check();
    await expect(add).toBeEnabled();
    await expect(
      frigateApp.page.getByText(/Ring can end sessions or become unavailable/),
    ).toBeVisible();
  });

  test("motion AI saves its window without claiming inference has started", async ({
    frigateApp,
  }) => {
    let enabled = false;
    await frigateApp.page.route("**/api/plugins", (route) =>
      route.fulfill({
        json: {
          developer_mode: false,
          catalog: [ringManifest],
          installed: [
            {
              manifest: ringManifest,
              enabled: true,
              developer: false,
              state: "running",
              bindings: { "123": "ring_front" },
              devices: [{ id: "123", name: "Front Door", two_way_audio: true }],
              event_inference: {
                ring_front: {
                  enabled,
                  window_seconds: 30,
                  active: false,
                  remaining_seconds: 0,
                },
              },
            },
          ],
        },
      }),
    );
    await frigateApp.page.route(
      "**/api/plugins/ring/cameras/ring_front/event-inference",
      async (route) => {
        expect(route.request().method()).toBe("PUT");
        expect(route.request().postDataJSON()).toEqual({
          enabled: true,
          window_seconds: 30,
          triggers: ["motion"],
        });
        enabled = true;
        await route.fulfill({ json: { success: true } });
      },
    );
    await frigateApp.goto("/settings?page=plugins");
    const checkbox = frigateApp.page.getByRole("checkbox", {
      name: "Run object detection for 30 seconds after Ring motion",
    });
    await expect(checkbox).not.toBeChecked();
    await checkbox.click();
    await expect(checkbox).toBeChecked();
    await expect(
      frigateApp.page.getByText(
        "AI decoder is idle. Recording continues independently.",
      ),
    ).toBeVisible();
    await expect(frigateApp.page.getByText(/AI window active:/)).toHaveCount(0);
  });

  test("viewers cannot open plugin settings", async ({ frigateApp }) => {
    await frigateApp.installDefaults({ profile: viewerProfile() });
    await frigateApp.goto("/settings?page=plugins");
    await expect(
      frigateApp.page.getByRole("button", {
        name: "Enable developer mode",
        exact: true,
      }),
    ).toHaveCount(0);
    await expect(
      frigateApp.page.getByRole("button", { name: "Install", exact: true }),
    ).toHaveCount(0);
  });
});
