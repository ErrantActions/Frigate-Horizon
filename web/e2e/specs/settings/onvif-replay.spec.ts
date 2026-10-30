import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Page } from "@playwright/test";
import { test, expect } from "../../fixtures/frigate-test";
import { configFactory } from "../../fixtures/mock-data/config";
import { mockHlsMedia } from "../../helpers/hls-media";

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

async function installRecordings(page: Page) {
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
  await page.route("**/api/onvif/cameras/front_door/recordings*", (route) =>
    route.fulfill({
      json: {
        recordings: [
          {
            token: "sd-main",
            start: "2026-10-18T06:00:00Z",
            end: "2026-10-18T06:10:00Z",
          },
        ],
        search_state: "Completed",
        limit: 100,
      },
    }),
  );
}

async function search(page: Page) {
  await page
    .getByLabel("Recording search start", { exact: true })
    .fill("2026-10-18T00:00");
  await page
    .getByLabel("Recording search end", { exact: true })
    .fill("2026-10-18T00:10");
  await page
    .getByRole("button", { name: "Search camera recordings", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Play camera recording", exact: true }),
  ).toBeVisible();
}

test.describe("ONVIF browser replay @mobile", () => {
  // Unsupported-camera requests deliberately return a camera-scoped 422.
  test.use({
    expectedErrors: [
      /(?:422.*\/api\/onvif\/cameras\/front_door\/replay\/sessions|Failed to load resource: the server responded with a status of 422)/,
    ],
  });

  for (const withAudio of [true, false]) {
    test(`reports verified ${withAudio ? "AAC-LC 48k audio" : "no-recorded-audio status"}`, async ({
      frigateApp,
    }) => {
      await installRecordings(frigateApp.page);
      const identifier = withAudio ? "audio-session" : "silent-session";
      const status = {
        id: identifier,
        state: "streaming",
        start: "2026-10-18T06:00:00Z",
        end: "2026-10-18T06:01:00Z",
        expires_in: 60,
        max_duration_seconds: 300,
        audio: withAudio,
        audio_status: withAudio ? "available" : "not_recorded",
        audio_codec: withAudio ? "aac_lc" : null,
        audio_sample_rate: withAudio ? 48000 : null,
      };
      await frigateApp.page.route(
        "**/api/onvif/cameras/front_door/replay/sessions",
        (route) =>
          route.fulfill({
            json: {
              ...status,
              state: "ready",
              audio: withAudio ? null : false,
              audio_status: withAudio ? "pending" : "not_recorded",
            },
          }),
      );
      await frigateApp.page.route(
        `**/api/onvif/cameras/front_door/replay/sessions/${identifier}`,
        (route) =>
          route.fulfill({
            json:
              route.request().method() === "DELETE"
                ? { state: "closed" }
                : status,
          }),
      );
      await mockHlsMedia(
        frigateApp.page,
        `**/api/onvif/cameras/front_door/replay/sessions/${identifier}/hls/**`,
        { audio: withAudio },
      );
      await frigateApp.goto(SETTINGS);
      await search(frigateApp.page);
      const verified = frigateApp.page.waitForRequest(
        (request) =>
          request.method() === "GET" &&
          request.url().endsWith(`/replay/sessions/${identifier}`),
      );
      const ended = frigateApp.page.waitForRequest(
        (request) =>
          request.method() === "DELETE" &&
          request.url().endsWith(`/replay/sessions/${identifier}`),
      );
      await frigateApp.page
        .getByRole("button", { name: "Play camera recording", exact: true })
        .click();
      await verified;
      await expect(
        frigateApp.page.getByText(
          withAudio
            ? "Recorded audio available: AAC-LC at 48 kHz"
            : "This recording reports no recorded audio",
          { exact: true },
        ),
      ).toBeVisible();
      await frigateApp.page
        .locator("section video")
        .evaluate((element) => element.dispatchEvent(new Event("ended")));
      await ended;
      await expect(
        frigateApp.page.getByText("Replay ended.", { exact: false }),
      ).toBeVisible();
    });
  }

  test("uses opaque same-origin media and releases a failed media session without disclosing camera secrets", async ({
    frigateApp,
  }) => {
    await installRecordings(frigateApp.page);
    const identifier = "opaque-replay-session";
    await frigateApp.page.route(
      "**/api/onvif/cameras/front_door/replay/sessions",
      async (route) => {
        const body = route.request().postDataJSON();
        expect(body).toMatchObject({
          recording_token: "sd-main",
          start: "2026-10-18T06:00:00Z",
          end: "2026-10-18T06:01:00.000Z",
        });
        expect(body).not.toHaveProperty("url");
        expect(body).not.toHaveProperty("password");
        await route.fulfill({
          json: {
            id: identifier,
            state: "ready",
            start: body.start,
            end: body.end,
            expires_in: 20,
            max_duration_seconds: 300,
            audio: false,
          },
        });
      },
    );
    await frigateApp.page.route(
      `**/api/onvif/cameras/front_door/replay/sessions/${identifier}`,
      (route) => route.fulfill({ json: { state: "closed" } }),
    );
    await mockHlsMedia(
      frigateApp.page,
      `**/api/onvif/cameras/front_door/replay/sessions/${identifier}/hls/**`,
      { broken: true },
    );
    await frigateApp.goto(SETTINGS);
    await search(frigateApp.page);
    const stream = frigateApp.page.waitForRequest((request) =>
      request.url().endsWith(`${identifier}/hls/index.m3u8`),
    );
    const closed = frigateApp.page.waitForRequest(
      (request) =>
        request.method() === "DELETE" &&
        request.url().endsWith(`/replay/sessions/${identifier}`),
    );
    await frigateApp.page
      .getByRole("button", { name: "Play camera recording", exact: true })
      .click();
    expect((await stream).url()).toContain(
      `/api/onvif/cameras/front_door/replay/sessions/${identifier}/hls/index.m3u8`,
    );
    await closed;
    await expect(
      frigateApp.page.getByRole("alert").filter({
        hasText:
          "The camera could not provide supported ONVIF clock-range replay",
      }),
    ).toBeVisible();
    await expect(
      frigateApp.page.getByText("rtsp://", { exact: false }),
    ).toHaveCount(0);
    await frigateApp.page
      .getByRole("button", { name: "Close camera replay", exact: true })
      .click();
    await expect(
      frigateApp.page.getByRole("heading", {
        name: "Camera SD browser replay",
        exact: true,
      }),
    ).toHaveCount(0);
  });

  test("shows unsupported-camera status without a live or external URL fallback", async ({
    frigateApp,
  }) => {
    await installRecordings(frigateApp.page);
    const mediaRequests: string[] = [];
    frigateApp.page.on("request", (request) => {
      if (request.url().includes("/hls/")) mediaRequests.push(request.url());
    });
    await frigateApp.page.route(
      "**/api/onvif/cameras/front_door/replay/sessions",
      (route) =>
        route.fulfill({
          status: 422,
          json: { detail: "Camera replay is unsupported" },
        }),
    );
    await frigateApp.goto(SETTINGS);
    await search(frigateApp.page);
    await frigateApp.page
      .getByRole("button", { name: "Play camera recording", exact: true })
      .click();
    await expect(
      frigateApp.page.getByRole("alert").filter({
        hasText:
          "The camera could not provide supported ONVIF clock-range replay",
      }),
    ).toBeVisible();
    expect(mediaRequests).toEqual([]);
    await expect(
      frigateApp.page.getByText("rtsp://", { exact: false }),
    ).toHaveCount(0);
  });

  test.describe("replay lifecycle failures", () => {
    test.use({
      expectedErrors: [
        /(?:503.*\/api\/onvif\/cameras\/front_door\/replay\/sessions|Failed to load resource: the server responded with a status of 503)/,
      ],
    });

    for (const failure of ["metadata", "cleanup"] as const) {
      test(`surfaces ${failure} failure instead of silently succeeding`, async ({
        frigateApp,
      }) => {
        await installRecordings(frigateApp.page);
        const identifier = `${failure}-failure`;
        const status = {
          id: identifier,
          state: "ready",
          start: "2026-10-18T06:00:00Z",
          end: "2026-10-18T06:01:00Z",
          expires_in: 60,
          max_duration_seconds: 300,
          audio: false,
          audio_status: "not_recorded",
        };
        await frigateApp.page.route(
          "**/api/onvif/cameras/front_door/replay/sessions",
          (route) => route.fulfill({ json: status }),
        );
        await frigateApp.page.route(
          `**/api/onvif/cameras/front_door/replay/sessions/${identifier}`,
          (route) => {
            const deleting = route.request().method() === "DELETE";
            const fails = failure === "cleanup" ? deleting : !deleting;
            return route.fulfill({
              status: fails ? 503 : 200,
              json: fails ? { detail: "Temporarily unavailable" } : status,
            });
          },
        );
        await mockHlsMedia(
          frigateApp.page,
          `**/api/onvif/cameras/front_door/replay/sessions/${identifier}/hls/**`,
          { audio: false },
        );
        await frigateApp.goto(SETTINGS);
        await search(frigateApp.page);
        await frigateApp.page
          .getByRole("button", { name: "Play camera recording", exact: true })
          .click();
        if (failure === "cleanup") {
          await expect(frigateApp.page.locator("section video")).toBeVisible();
          await frigateApp.page
            .getByRole("button", { name: "Close camera replay", exact: true })
            .click();
          await expect(
            frigateApp.page.getByText(
              "Replay cleanup could not be confirmed. The temporary session has a bounded expiry.",
              { exact: true },
            ),
          ).toBeVisible();
        } else {
          await expect(
            frigateApp.page.getByRole("alert").filter({
              hasText: "Replay metadata could not be verified.",
            }),
          ).toBeVisible();
        }
      });
    }
  });
});
