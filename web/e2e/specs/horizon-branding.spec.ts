import { test, expect } from "../fixtures/frigate-test";

test.describe("Horizon identity @mobile", () => {
  test("uses the Horizon mark instead of the upstream bird in the app shell", async ({
    frigateApp,
  }) => {
    await frigateApp.goto("/system#storage");
    const mark = frigateApp.page.locator('svg[data-brand="frigate-horizon"]');
    await expect(mark.first()).toBeVisible();
    await expect(mark.first()).toHaveClass(/text-selected/);
    await expect(
      mark.first().locator('[data-brand-detail="bird"]'),
    ).toHaveCount(2);
    const birdsAreAboveSun = await mark.first().evaluate((svg) => {
      const sun = svg.querySelector<SVGPathElement>(
        '[data-brand-detail="sun"]',
      );
      if (!sun) return false;
      const sunTop = sun.getBBox().y;
      return Array.from(
        svg.querySelectorAll<SVGPathElement>('[data-brand-detail="bird"]'),
      ).every((bird) => {
        const box = bird.getBBox();
        return box.y + box.height < sunTop;
      });
    });
    expect(birdsAreAboveSun).toBe(true);
    const reflectionWidths = await mark
      .first()
      .evaluate((svg) =>
        Array.from(
          svg.querySelectorAll<SVGPathElement>(
            '[data-brand-detail="reflection"]',
          ),
          (line) => Number.parseFloat(getComputedStyle(line).strokeWidth),
        ),
      );
    expect(reflectionWidths).toEqual([4, 3, 2]);
    if (!frigateApp.isMobile) {
      await expect(
        frigateApp.page.getByRole("link", {
          name: "Frigate Horizon home",
          exact: true,
        }),
      ).toBeVisible();
    }
    await expect(
      frigateApp.page.locator('link[rel="icon"][type="image/svg+xml"]'),
    ).toHaveAttribute("href", /horizon/);
  });

  test("identifies the fork on the login page", async ({ frigateApp }) => {
    await frigateApp.page.goto("/login.html");
    await expect(
      frigateApp.page.getByRole("heading", {
        name: "Frigate Horizon",
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      frigateApp.page.locator('svg[data-brand="frigate-horizon"]'),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText("An independent fork built on Frigate NVR.", {
        exact: true,
      }),
    ).toBeVisible();
  });

  test("serves the Horizon install icons from the manifest", async ({
    frigateApp,
  }) => {
    const response = await frigateApp.page.request.get("/site.webmanifest");
    expect(response.ok()).toBe(true);
    const manifest = await response.json();
    expect(manifest.name).toBe("Frigate Horizon");
    for (const icon of manifest.icons) {
      const asset = await frigateApp.page.request.get(`/${icon.src}`);
      expect(asset.ok()).toBe(true);
      expect(asset.headers()["content-type"]).toContain("image/png");
    }
  });
});
