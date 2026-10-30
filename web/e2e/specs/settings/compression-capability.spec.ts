import { test, expect } from "../../fixtures/frigate-test";
import { BASE_CONFIG } from "../../fixtures/mock-data/config";

test.describe("Compression backend capability @mobile", () => {
  test("unsupported settings stay hidden and direct links wait for config", async ({
    frigateApp,
  }) => {
    const config = structuredClone(BASE_CONFIG);
    Reflect.deleteProperty(config.storage, "compression");
    let releaseConfig: () => void = () => {};
    const configReady = new Promise<void>((resolve) => {
      releaseConfig = resolve;
    });
    await frigateApp.page.route("**/api/config", async (route) => {
      await configReady;
      await route.fulfill({ json: config });
    });
    await frigateApp.page.goto("/settings?page=recordingCompression");
    await expect(frigateApp.page).toHaveURL(/page=recordingCompression/);
    releaseConfig();
    await expect(
      frigateApp.page.getByRole("heading", { name: "UI Settings", exact: true }),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText("Older recording compression", { exact: true }),
    ).toHaveCount(0);
    await expect(frigateApp.page).not.toHaveURL(/page=recordingCompression/);
  });
});
