import { test, expect } from "../fixtures/frigate-test";
import { viewerProfile } from "../fixtures/mock-data/profile";
import { waitForBodyInteractive } from "../helpers/overlay-interaction";
import { readFileSync } from "node:fs";
import type {
  ProtectedMediaItem,
  ProtectedMediaResponse,
} from "../../src/types/protectedMedia";

const events = JSON.parse(
  readFileSync(
    new URL("../fixtures/mock-data/events.json", import.meta.url),
    "utf8",
  ),
) as Record<string, unknown>[];

const saved: ProtectedMediaItem = {
  id: "saved-front",
  camera: "front_door",
  start_time: 1700000000,
  end_time: 1700000010,
  label: "person",
  reason: "saved_event",
  protection: "permanent",
  thumbnail_url: "events/saved-front/thumbnail.jpg",
  snapshot_url: "events/saved-front/snapshot.jpg",
  original_url: "front_door/start/1700000000/end/1700000010/clip.mp4",
  availability: "indexed_local",
  tiers: ["local"],
  recordings: 1,
  has_snapshot: true,
};

function library(
  items: ProtectedMediaItem[] = [
    saved,
    { ...saved, id: "same-physical-file" },
    {
      ...saved,
      id: "old-archive",
      camera: "retired_camera",
      availability: "archive_unavailable",
      tiers: ["archive"],
      original_url: null,
    },
  ],
): ProtectedMediaResponse {
  return {
    items,
    total: items.length,
    limit: 24,
    offset: 0,
    cameras: ["front_door", "retired_camera"],
    storage: {
      local: {
        recordings: 1,
        known_bytes: 10485760,
        unknown_size_recordings: 0,
        total_bytes: 10485760,
      },
      archive: {
        recordings: 1,
        known_bytes: 0,
        unknown_size_recordings: 1,
        total_bytes: null,
      },
    },
    archive_state: "blocked",
    archive_updated_at: 1700000100,
  };
}

test.describe("Protected media library @mobile", () => {
  test("shows saved history, distinct storage, unknown sizes and offline archive", async ({
    frigateApp,
  }) => {
    await frigateApp.page.route("**/api/protected-media?*", (route) =>
      route.fulfill({ json: library() }),
    );
    await frigateApp.goto("/protected");
    const page = frigateApp.page;
    await expect(
      page.getByRole("heading", { name: "Protected media", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("3 protected events", { exact: true }),
    ).toBeVisible();
    await expect(page.getByRole("article")).toHaveCount(3);
    await expect(
      page.getByText("1 physical recording segment", { exact: true }),
    ).toHaveCount(2);
    await expect(
      page.getByText("1 recording has unknown size. The total is incomplete."),
    ).toBeVisible();
    const archive = page.getByRole("article", {
      name: "Protected evidence from retired_camera",
    });
    await expect(
      archive.getByRole("button", { name: "Play original" }),
    ).toBeDisabled();
    await expect(
      archive.getByRole("link", { name: "Export original" }),
    ).toHaveCount(0);
    await expect(
      archive.getByText(/Evidence has not been marked deleted/),
    ).toBeVisible();
    await expect(
      page.getByText(/Temporary playback and export leases/),
    ).toBeVisible();
    await expect
      .poll(() =>
        page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
      )
      .toBe(true);
  });

  test("original export preserves the existing original-media URL", async ({
    frigateApp,
  }) => {
    await frigateApp.page.route("**/api/protected-media?*", (route) =>
      route.fulfill({ json: library([saved]) }),
    );
    await frigateApp.goto("/protected");
    const item = frigateApp.page.getByRole("article");
    await expect(
      item.getByRole("button", { name: "Play original" }),
    ).toBeEnabled();
    await expect(
      item.getByRole("link", { name: "Export original" }),
    ).toHaveAttribute(
      "href",
      /\/api\/front_door\/start\/1700000000\/end\/1700000010\/clip\.mp4$/,
    );
    await expect(
      item.getByRole("link", { name: "Export original" }),
    ).toHaveAttribute("download", "saved-front.mp4");
  });

  test("camera and date filters reset pagination and invalid ranges do not fetch", async ({
    frigateApp,
  }) => {
    const requests: URL[] = [];
    await frigateApp.page.route("**/api/protected-media?*", (route) => {
      const url = new URL(route.request().url());
      requests.push(url);
      return route.fulfill({
        json: library(url.searchParams.has("camera") ? [saved] : undefined),
      });
    });
    await frigateApp.goto("/protected");
    const page = frigateApp.page;
    await page.getByLabel("Camera", { exact: true }).selectOption("front_door");
    await expect
      .poll(() => requests.at(-1)?.searchParams.get("camera"))
      .toBe("front_door");
    await page.getByLabel("From date", { exact: true }).fill("2023-11-01");
    await expect
      .poll(() => requests.at(-1)?.searchParams.has("after"))
      .toBe(true);
    await page.getByLabel("Through date", { exact: true }).fill("2023-11-30");
    await expect
      .poll(() => requests.at(-1)?.searchParams.has("before"))
      .toBe(true);
    await page.getByLabel("Through date", { exact: true }).fill("2023-10-01");
    await expect(page.getByRole("alert")).toHaveText(
      "The start date must not be after the end date.",
    );
    await expect(
      page.getByRole("button", { name: "Refresh", exact: true }),
    ).toBeDisabled();
  });

  test("pagination requests the next page without changing full-result totals", async ({
    frigateApp,
  }) => {
    await frigateApp.page.route("**/api/protected-media?*", (route) => {
      const offset = Number(
        new URL(route.request().url()).searchParams.get("offset"),
      );
      return route.fulfill({
        json: {
          ...library([{ ...saved, id: offset ? "last-page" : saved.id }]),
          total: 25,
          offset,
        },
      });
    });
    await frigateApp.goto("/protected");
    const page = frigateApp.page;
    await page.getByRole("button", { name: "Next", exact: true }).click();
    await expect(page.getByText("Page 2", { exact: true })).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Next", exact: true }),
    ).toBeDisabled();
    await expect(
      page.getByText("25 protected events", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("link", { name: "Export original" }),
    ).toHaveAttribute("download", "last-page.mp4");
  });

  test("unprotect requires explicit confirmation and never sends a media delete", async ({
    frigateApp,
  }) => {
    let released = false;
    const bodies: unknown[] = [];
    await frigateApp.page.route("**/api/protected-media?*", (route) =>
      route.fulfill({ json: library(released ? [] : [saved]) }),
    );
    await frigateApp.page.route(
      "**/api/events/saved-front/retain",
      async (route) => {
        expect(route.request().method()).toBe("DELETE");
        bodies.push(route.request().postDataJSON());
        released = true;
        await route.fulfill({
          json: { success: true, message: "Event un-retained" },
        });
      },
    );
    await frigateApp.goto("/protected");
    const page = frigateApp.page;
    await page
      .getByRole("button", { name: "Remove protection", exact: true })
      .click();
    const dialog = page.getByRole("alertdialog");
    await expect(
      dialog.getByText(
        /Normal retention, quota eviction and future lossy compression/,
      ),
    ).toBeVisible();
    await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
    expect(bodies).toEqual([]);
    await page
      .getByRole("button", { name: "Remove protection", exact: true })
      .click();
    await page
      .getByRole("button", {
        name: "Confirm removal of protection",
        exact: true,
      })
      .click();
    await expect(
      page.getByText("No protected media matches these filters."),
    ).toBeVisible();
    expect(bodies).toEqual([{ confirm: true, acknowledge_retention: true }]);
  });

  test("viewers can browse permitted media but cannot remove protection", async ({
    frigateApp,
  }) => {
    await frigateApp.api.install({ profile: viewerProfile() });
    await frigateApp.page.route("**/api/protected-media?*", (route) =>
      route.fulfill({ json: library([saved]) }),
    );
    await frigateApp.goto("/protected");
    await expect(frigateApp.page.getByRole("article")).toHaveCount(1);
    await expect(
      frigateApp.page.getByRole("button", {
        name: "Remove protection",
        exact: true,
      }),
    ).toHaveCount(0);
  });

  test("saved Explore actions share confirmation and send the required unprotect body", async ({
    frigateApp,
  }) => {
    test.setTimeout(60000);
    let protectedEvent = true;
    const bodies: unknown[] = [];
    await frigateApp.page.route("**/api/events?*", (route) =>
      route.fulfill({
        json: [{ ...events[0], retain_indefinitely: protectedEvent }],
      }),
    );
    await frigateApp.page.route(
      "**/api/events/event-person-001/retain",
      async (route) => {
        expect(route.request().method()).toBe("DELETE");
        bodies.push(route.request().postDataJSON());
        protectedEvent = false;
        await route.fulfill({ json: { success: true } });
      },
    );
    await frigateApp.goto("/explore?labels=person");
    const page = frigateApp.page;
    await page.getByLabel("More", { exact: true }).first().click();
    await expect(
      page.getByRole("menuitem", { name: "Delete this tracked object" }),
    ).toHaveAttribute("aria-disabled", "true");
    await page
      .getByRole("menuitem", { name: "Remove protection", exact: true })
      .click();
    await expect(page.getByRole("alertdialog")).toContainText(
      "Normal retention, quota eviction and future lossy compression",
    );
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(page.getByRole("alertdialog")).toHaveCount(0);
    await waitForBodyInteractive(page);
    expect(bodies).toEqual([]);
    await page.getByLabel("More", { exact: true }).first().click();
    await page
      .getByRole("menuitem", { name: "Remove protection", exact: true })
      .click();
    await page
      .getByRole("button", {
        name: "Confirm removal of protection",
        exact: true,
      })
      .click();
    await expect(page.getByRole("alertdialog")).toHaveCount(0);
    await waitForBodyInteractive(page);
    expect(bodies).toEqual([{ confirm: true, acknowledge_retention: true }]);
    await page.getByLabel("More", { exact: true }).first().click();
    await expect(
      page.getByRole("menuitem", { name: "Remove protection", exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("menuitem", { name: "Delete this tracked object" }),
    ).not.toHaveAttribute("aria-disabled", "true");
  });
});

test.describe("Protected media errors @mobile", () => {
  test.use({ expectedErrors: [/protected-media|retain|500|409/] });
  test("failed requests show an error instead of zero usage or empty history", async ({
    frigateApp,
  }) => {
    await frigateApp.page.route("**/api/protected-media?*", (route) =>
      route.fulfill({ status: 500, json: { success: false } }),
    );
    await frigateApp.goto("/protected");
    await expect(frigateApp.page.getByRole("alert")).toHaveText(
      "Protected media could not be loaded. Try refreshing.",
    );
    await expect(
      frigateApp.page.getByText("No protected media matches these filters."),
    ).toHaveCount(0);
    await expect(
      frigateApp.page.getByText("0 physical recording segments", {
        exact: true,
      }),
    ).toHaveCount(0);
  });

  test("failed unprotect keeps the evidence and confirmation visible", async ({
    frigateApp,
  }) => {
    await frigateApp.page.route("**/api/protected-media?*", (route) =>
      route.fulfill({ json: library([saved]) }),
    );
    await frigateApp.page.route("**/api/events/saved-front/retain", (route) =>
      route.fulfill({ status: 409, json: { success: false } }),
    );
    await frigateApp.goto("/protected");
    const page = frigateApp.page;
    await page
      .getByRole("button", { name: "Remove protection", exact: true })
      .click();
    await page
      .getByRole("button", {
        name: "Confirm removal of protection",
        exact: true,
      })
      .click();
    await expect(page.getByRole("alertdialog")).toBeVisible();
    await expect(page.getByRole("alert")).toHaveText(
      "Protection could not be removed. Your permissions or the event may have changed. Refresh and try again.",
    );
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(page.getByRole("article")).toHaveCount(1);
  });
});
