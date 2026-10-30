import { readFileSync } from "node:fs";
import { test, expect } from "../fixtures/frigate-test";

const events: { data: Record<string, unknown> }[] = JSON.parse(
  readFileSync(
    new URL("../fixtures/mock-data/events.json", import.meta.url),
    "utf8",
  ),
);

test.describe("Native Reolink events @mobile", () => {
  test("does not invent confidence on the thumbnail or detail view", async ({
    frigateApp,
  }) => {
    const native = {
      ...events[0],
      data: {
        ...events[0].data,
        type: "reolink",
        score: null,
        top_score: null,
      },
    };
    await frigateApp.installDefaults({
      config: { plus: { enabled: false } },
      events: [native],
    });
    await frigateApp.page.route("**/api/review/event/**", (route) =>
      route.fulfill({ json: null }),
    );
    await frigateApp.goto("/explore?labels=person");
    const first = frigateApp.page.locator("[data-start]").first();
    await expect(first).toBeVisible();
    await expect(first).not.toContainText("%");
    await first.click();
    await expect(
      frigateApp.page.getByRole("heading", { name: "Tracked Object Details" }),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText("Top Score", { exact: true }),
    ).toHaveCount(0);
    await expect(
      frigateApp.page.getByText("Snapshot Score", { exact: true }),
    ).toHaveCount(0);
  });
});
