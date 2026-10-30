import { test, expect } from "../../fixtures/frigate-test";

test.describe("Configuration-only camera removal @medium @mobile", () => {
  test.beforeEach(async ({ frigateApp }) => {
    await frigateApp.api.install({
      config: {
        cameras: {
          front_door: {
            friendly_name: "Front yard North old",
            enabled: false,
            enabled_in_config: true,
          },
          garage: {
            friendly_name: "Retired garage",
            enabled: false,
            enabled_in_config: false,
          },
        },
      },
    });
    await frigateApp.goto("/settings?page=cameraManagement");
    await expect(
      frigateApp.page.getByRole("heading", { name: /Manage Cameras/i }),
    ).toBeVisible();
  });

  test("removes a switched-off camera through the configuration-only route", async ({
    frigateApp,
  }) => {
    const requests: URL[] = [];
    await frigateApp.page.route(
      "**/api/cameras/*/configuration",
      async (route) => {
        if (route.request().method() !== "DELETE") return route.fallback();
        requests.push(new URL(route.request().url()));
        return route.fulfill({
          status: 200,
          json: { success: true, data_deleted: false, cleanup: {} },
        });
      },
    );
    await frigateApp.page
      .getByRole("button", { name: "Remove Front yard North old", exact: true })
      .click();
    const dialog = frigateApp.page.getByRole("dialog");
    await expect(
      dialog.getByText(/normal retention and access rules still apply/i),
    ).toBeVisible();
    await expect(dialog.getByRole("switch")).toHaveCount(0);
    await expect(
      dialog.getByText(/Front yard North old \(front_door\)/),
    ).toBeVisible();
    await dialog
      .getByRole("button", { name: "Remove camera, keep footage" })
      .click();
    await expect(dialog).not.toBeVisible();
    expect(requests).toHaveLength(1);
    expect(requests[0].pathname).toBe("/api/cameras/front_door/configuration");
    expect(requests[0].searchParams.has("delete_data")).toBe(false);
    expect(requests[0].searchParams.has("delete_exports")).toBe(false);
  });

  test("an older backend cannot fall back to destructive camera deletion", async ({
    frigateApp,
  }) => {
    const deletes: string[] = [];
    await frigateApp.page.route("**/api/cameras/**", async (route) => {
      if (route.request().method() !== "DELETE") return route.fallback();
      deletes.push(new URL(route.request().url()).pathname);
      return route.fulfill({
        status: 404,
        json: {
          message: "Configuration-only removal is not available on this server",
        },
      });
    });
    await frigateApp.page
      .getByRole("button", { name: "Remove Front yard North old", exact: true })
      .click();
    const dialog = frigateApp.page.getByRole("dialog");
    await dialog
      .getByRole("button", { name: "Remove camera, keep footage" })
      .click();
    await expect(
      frigateApp.page.getByText(
        "Configuration-only removal is not available on this server",
        { exact: true },
      ),
    ).toBeVisible();
    await expect(dialog).toBeVisible();
    await expect(
      dialog.getByRole("button", { name: "Remove camera, keep footage" }),
    ).toBeEnabled();
    expect(deletes).toEqual(["/api/cameras/front_door/configuration"]);
  });

  test("offers removal for a camera disabled in configuration", async ({
    frigateApp,
  }) => {
    await frigateApp.page
      .getByRole("button", { name: "Remove Retired garage", exact: true })
      .click();
    const dialog = frigateApp.page.getByRole("dialog");
    await expect(dialog.getByText(/Retired garage \(garage\)/)).toBeVisible();
    await expect(
      dialog.getByRole("button", { name: "Remove camera, keep footage" }),
    ).toBeEnabled();
    await dialog.getByRole("button", { name: "Close", exact: true }).click();
    await expect(dialog).not.toBeVisible();
  });

  test("top-level removal picker includes friendly names and inactive IDs", async ({
    frigateApp,
  }) => {
    await frigateApp.page
      .getByRole("button", { name: "Remove camera", exact: true })
      .click();
    const dialog = frigateApp.page.getByRole("dialog");
    await dialog.getByRole("combobox").click();
    await expect(
      frigateApp.page.getByRole("option", { name: /Retired garage.*garage/ }),
    ).toBeVisible();
    await frigateApp.page
      .getByRole("option", { name: /Front yard North old.*front_door/ })
      .click();
    await dialog
      .getByRole("button", { name: "Remove camera", exact: true })
      .click();
    await expect(
      dialog.getByText(/normal retention and access rules still apply/i),
    ).toBeVisible();
  });
});
