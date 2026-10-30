/**
 * Navigation tests -- CRITICAL tier.
 *
 * Covers sidebar (desktop) / bottombar (mobile) link set, conditional
 * nav items (faces, chat, classification), settings menu navigation,
 * unknown-route redirect to /, and mobile-specific nav behaviors.
 */

import { test, expect } from "../fixtures/frigate-test";
import { BasePage } from "../pages/base.page";
import { viewerProfile } from "../fixtures/mock-data/profile";

const PRIMARY_ROUTES = ["/review", "/explore", "/export"] as const;

test.describe("Navigation — primary links @critical", () => {
  test("every primary link is visible and navigates", async ({
    frigateApp,
  }) => {
    await frigateApp.goto("/");
    for (const route of PRIMARY_ROUTES) {
      await expect(
        frigateApp.page.locator(`a[href="${route}"]`).first(),
      ).toBeVisible();
    }
    const base = new BasePage(frigateApp.page, !frigateApp.isMobile);
    for (const route of PRIMARY_ROUTES) {
      await base.navigateTo(route);
      await expect(frigateApp.page).toHaveURL(new RegExp(route));
      await expect(frigateApp.page.locator("#pageRoot")).toBeVisible();
    }
  });

  test("logo links home on desktop", async ({ frigateApp }) => {
    test.skip(frigateApp.isMobile, "Sidebar logo is desktop-only");
    await frigateApp.goto("/review");
    await frigateApp.page.locator("aside a[href='/']").first().click();
    await expect(frigateApp.page).toHaveURL(/\/$/);
  });

  test("desktop sidebar has usable targets and visible icons", async ({
    frigateApp,
  }, testInfo) => {
    test.skip(frigateApp.isMobile, "Desktop sidebar");
    await frigateApp.goto("/");
    const sidebar = frigateApp.page.locator("aside");
    for (const route of PRIMARY_ROUTES) {
      const link = sidebar.locator(`a[href="${route}"]`);
      const bounds = await link.boundingBox();
      expect(bounds?.width).toBeGreaterThanOrEqual(44);
      expect(bounds?.height).toBeGreaterThanOrEqual(44);
      const icon = await link.locator("svg").boundingBox();
      expect(icon?.width).toBeGreaterThanOrEqual(24);
      await link.click();
      await expect(frigateApp.page).toHaveURL(new RegExp(route));
    }
    await frigateApp.goto("/");
    const allCameras = sidebar.getByRole("button", {
      name: "All Cameras",
      exact: true,
    });
    const bounds = await allCameras.boundingBox();
    expect(bounds?.width).toBeGreaterThanOrEqual(40);
    expect(bounds?.height).toBeGreaterThanOrEqual(40);
    await sidebar.screenshot({ path: testInfo.outputPath("sidebar.png") });
    await sidebar
      .getByRole("button", { name: "Settings", exact: true })
      .click();
    await expect(frigateApp.page.getByRole("menu")).toBeVisible();
  });

  test("unknown route redirects to /", async ({ frigateApp }) => {
    await frigateApp.page.goto("/nonexistent-route");
    await frigateApp.page.waitForSelector("#pageRoot", { timeout: 10_000 });
    await expect(frigateApp.page).toHaveURL(/\/$/);
    await expect(
      frigateApp.page.locator("[data-camera='front_door']"),
    ).toBeVisible({ timeout: 10_000 });
  });
});

test.describe("Navigation — conditional items @critical", () => {
  test("/faces is hidden when face_recognition.enabled is false", async ({
    frigateApp,
  }) => {
    await frigateApp.goto("/");
    await frigateApp.page
      .getByRole("button", { name: "Tools", exact: true })
      .click();
    await expect(
      frigateApp.page.locator('a[href="/faces"]').first(),
    ).toHaveCount(0);
  });

  test("/faces is visible when face_recognition.enabled is true (desktop)", async ({
    frigateApp,
  }) => {
    test.skip(frigateApp.isMobile, "Desktop sidebar");
    await frigateApp.installDefaults({
      config: { face_recognition: { enabled: true } },
    });
    await frigateApp.goto("/");
    await frigateApp.page
      .getByRole("button", { name: "Tools", exact: true })
      .click();
    await expect(
      frigateApp.page.locator('a[href="/faces"]').first(),
    ).toBeVisible();
  });

  test("/chat is hidden when no agent has the chat role (desktop)", async ({
    frigateApp,
  }) => {
    test.skip(frigateApp.isMobile, "Desktop sidebar");
    await frigateApp.installDefaults({
      config: {
        genai: {
          descriptions_only: {
            provider: "ollama",
            model: "llava",
            roles: ["descriptions"],
          },
        },
      },
    });
    await frigateApp.goto("/");
    await frigateApp.page
      .getByRole("button", { name: "Tools", exact: true })
      .click();
    await expect(
      frigateApp.page.locator('a[href="/chat"]').first(),
    ).toHaveCount(0);
  });

  test("/chat is visible when an agent has the chat role (desktop)", async ({
    frigateApp,
  }) => {
    test.skip(frigateApp.isMobile, "Desktop sidebar");
    await frigateApp.installDefaults({
      config: {
        genai: {
          chat_agent: {
            provider: "ollama",
            model: "llava",
            roles: ["chat"],
          },
        },
      },
    });
    await frigateApp.goto("/");
    await frigateApp.page
      .getByRole("button", { name: "Tools", exact: true })
      .click();
    await expect(
      frigateApp.page.locator('a[href="/chat"]').first(),
    ).toBeVisible();
  });

  test("/classification is visible for admin on desktop", async ({
    frigateApp,
  }) => {
    test.skip(frigateApp.isMobile, "Desktop sidebar");
    await frigateApp.goto("/");
    await frigateApp.page
      .getByRole("button", { name: "Tools", exact: true })
      .click();
    await expect(
      frigateApp.page.locator('a[href="/classification"]').first(),
    ).toBeVisible();
  });

  test("keeps core navigation uncluttered and saved evidence reachable through Tools", async ({
    frigateApp,
  }) => {
    await frigateApp.goto("/");
    await expect(frigateApp.page.locator('a[href="/protected"]')).toHaveCount(
      0,
    );
    await expect(
      frigateApp.page.locator('a[href="/classification"]'),
    ).toHaveCount(0);
    const tools = frigateApp.page.getByRole("button", {
      name: "Tools",
      exact: true,
    });
    await tools.focus();
    await tools.press("Enter");
    const saved = frigateApp.page.getByRole("menuitem", {
      name: "Protected media",
      exact: true,
    });
    await expect(saved).toBeVisible();
    await saved.click();
    await expect(frigateApp.page).toHaveURL(/\/protected/);
    await expect(frigateApp.page.getByRole("menu")).toHaveCount(0);
  });

  test("does not expose administrator tools to viewers", async ({
    frigateApp,
  }) => {
    await frigateApp.installDefaults({ profile: viewerProfile() });
    await frigateApp.goto("/");
    await frigateApp.page
      .getByRole("button", { name: "Tools", exact: true })
      .click();
    await expect(
      frigateApp.page.getByRole("menuitem", {
        name: "Protected media",
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      frigateApp.page.locator(
        'a[href="/classification"], a[href="/chat"], a[href="/faces"]',
      ),
    ).toHaveCount(0);
  });
});

test.describe("Navigation — settings menu (desktop) @critical", () => {
  test.skip(
    ({ frigateApp }) => frigateApp.isMobile,
    "Sidebar settings menu is desktop-only",
  );

  const TARGETS = [
    { label: "Settings", url: /\/settings/ },
    { label: "Health and Metrics", url: /\/system/ },
    { label: "Logs", url: /\/logs/ },
    { label: "Configuration Editor", url: /\/config/ },
  ];

  for (const target of TARGETS) {
    test(`menu → ${target.label} navigates`, async ({ frigateApp }) => {
      await frigateApp.goto("/");
      const gear = frigateApp.page
        .locator("aside")
        .getByRole("button", { name: "Settings", exact: true });
      await gear.click();
      await frigateApp.page
        .getByRole("menuitem", { name: target.label, exact: true })
        .click();
      await expect(frigateApp.page).toHaveURL(target.url);
    });
  }
});

test.describe("Navigation — mobile @critical @mobile", () => {
  test("mobile bottombar visible, sidebar not rendered", async ({
    frigateApp,
  }) => {
    test.skip(!frigateApp.isMobile, "Mobile-only");
    await frigateApp.goto("/");
    await expect(frigateApp.page.locator("aside")).toHaveCount(0);
    for (const route of PRIMARY_ROUTES) {
      await expect(
        frigateApp.page.locator(`a[href="${route}"]`).first(),
      ).toBeVisible();
    }
  });

  test("mobile nav survives route change", async ({ frigateApp }) => {
    test.skip(!frigateApp.isMobile, "Mobile-only");
    await frigateApp.goto("/");
    const reviewLink = frigateApp.page.locator('a[href="/review"]').first();
    await reviewLink.click();
    await expect(frigateApp.page).toHaveURL(/\/review/);
    await expect(
      frigateApp.page.locator('a[href="/review"]').first(),
    ).toBeVisible();
  });
});
