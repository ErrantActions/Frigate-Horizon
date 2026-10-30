import { test, expect } from "../fixtures/frigate-test";
import { readFileSync } from "node:fs";
import type { ReviewSegment } from "../../src/types/review";

const reviews: ReviewSegment[] = JSON.parse(
  readFileSync(
    new URL("../fixtures/mock-data/reviews.json", import.meta.url),
    "utf-8",
  ),
);

test.describe("Review storage badges @mobile", () => {
  for (const [storage, label] of [
    ["local", "Local"],
    ["archive", "Archived"],
    ["mixed", "Local + archive"],
  ]) {
    test(`shows ${storage} from indexed recording metadata`, async ({
      frigateApp,
    }) => {
      await frigateApp.installDefaults({
        reviews: reviews.map((review) => ({
          ...review,
          recording_storage: storage,
        })),
      });
      await frigateApp.goto("/review");
      const badge = frigateApp.page.getByTestId("review-storage-badge").first();
      await expect(badge).toBeVisible();
      await expect(badge).toHaveText(label);
      await expect(badge).toHaveAttribute(
        "title",
        /does not guarantee complete coverage/,
      );
    });
  }

  test("does not invent a tier when backend metadata is absent", async ({
    frigateApp,
  }) => {
    await frigateApp.goto("/review");
    await expect(
      frigateApp.page.getByTestId("review-storage-badge"),
    ).toHaveCount(0);
  });
});
