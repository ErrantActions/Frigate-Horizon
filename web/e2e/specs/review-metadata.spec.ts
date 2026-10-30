import type { Route } from "@playwright/test";
import { test, expect, FrigateApp } from "../fixtures/frigate-test";
import type { ReviewSegment } from "../../src/types/review";
import { adminProfile } from "../fixtures/mock-data/profile";
import { ONVIF_VIDEO_ONLY_MP4 } from "../fixtures/mock-data/onvif-media";

const timestamp = Math.floor(Date.now() / 3600000) * 3600 - 3480;
const fragmentStart = ONVIF_VIDEO_ONLY_MP4.indexOf("moof") - 4;
const mediaStart = ONVIF_VIDEO_ONLY_MP4.indexOf("mdat") - 4;
const mediaEnd = mediaStart + ONVIF_VIDEO_ONLY_MP4.readUInt32BE(mediaStart);
const initialization = ONVIF_VIDEO_ONLY_MP4.subarray(0, fragmentStart);
const fragment = ONVIF_VIDEO_ONLY_MP4.subarray(fragmentStart, mediaEnd);
const fragmentCount = 120;
const fragmentSeconds = 10;
// The synthetic fixture has one H.264 keyframe and a 10,240 Hz video track.
// Retiming that sample creates continuous, genuinely decodable fMP4 playback.
const fragmentTicks = fragmentSeconds * 10240;
const mediaPlaylist =
  "#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:10\n" +
  '#EXT-X-PLAYLIST-TYPE:VOD\n#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-MAP:URI="init.mp4"\n' +
  Array.from(
    { length: fragmentCount },
    (_, index) => `#EXTINF:${fragmentSeconds},\nsegment-${index}.m4s\n`,
  ).join("") +
  "#EXT-X-ENDLIST\n";

function videoFragment(index: number) {
  const result = Buffer.from(fragment);
  result.writeUInt32BE(fragmentTicks, result.indexOf("tfhd") - 4 + 16);
  result.writeBigUInt64BE(
    BigInt(index * fragmentTicks),
    result.indexOf("tfdt") - 4 + 12,
  );
  result.writeUInt32BE(index + 1, result.indexOf("mfhd") - 4 + 12);
  return result;
}

const review = (
  camera = "front_door",
  detections = ["retained-person", "missing-person"],
): ReviewSegment => ({
  id: `${camera}-metadata-review`,
  camera,
  severity: "alert",
  start_time: timestamp - 30,
  end_time: timestamp + 900,
  has_been_reviewed: false,
  thumb_path: `/clips/${camera}/review-thumb.jpg`,
  recording_storage: "mixed",
  data: {
    detections,
    objects: ["person"],
    audio: [],
    sub_labels: ["Fixture identity"],
    significant_motion_areas: [],
    zones: ["front_yard"],
    metadata: {
      title: "Stored fixture summary",
      scene: "Person near the door",
      confidence: 0.9,
    },
  },
});
const event = (id = "retained-person", camera = "front_door") => ({
  id,
  camera,
  label: "person",
  sub_label: "Fixture identity",
  start_time: timestamp - 20,
  end_time: timestamp + 20,
  zones: ["front_yard"],
  model_hash: "fixture-model-hash",
  detector_type: "fixture-detector",
  model_type: "fixture-model-type",
  thumbnail: "SECRET_THUMBNAIL",
  arbitrary_private_field: "SECRET_RAW_EVENT",
  data: {
    type: "object",
    top_score: 0.925,
    sub_label_score: 0.875,
    box: [0.1, 0.2, 0.3, 0.4],
    region: [0, 0, 1, 1],
    area: 0.04,
    recognized_license_plate: "FIXTURE123",
    arbitrary_private_attribute: "SECRET_RAW_ATTRIBUTE",
  },
});

async function installReview(
  app: FrigateApp,
  reviews = [review(), review("backyard", ["backyard-person"])],
) {
  const manifests: string[] = [];
  await app.installDefaults({
    reviews,
    config: {
      cameras: {
        front_door: { review: { genai: { enabled_in_config: false } } },
        backyard: { review: { genai: { enabled_in_config: false } } },
      },
    },
  });
  await app.page.route("**/api/review/activity/motion?**", (route) =>
    route.fulfill({ json: [] }),
  );
  await app.page.route("**/api/recordings/unavailable?**", (route) =>
    route.fulfill({ json: [] }),
  );
  await app.page.route("**/api/*/recordings/coverage?**", (route) => {
    const url = new URL(route.request().url());
    const start = Number(url.searchParams.get("after"));
    const end = Number(url.searchParams.get("before"));
    return route.fulfill({
      json: {
        spans: [{ start_time: start, end_time: end, streams: ["main"] }],
        codecs_compatible: true,
        streams: {
          main: {
            video_codec: "h264",
            audio_codec: null,
            audio_rate: 0,
            has_audio: false,
          },
        },
        timelines: {
          auto: [
            {
              start_time: start,
              end_time: end,
              duration: (end - start) * 1000,
            },
          ],
          main: [
            {
              start_time: start,
              end_time: end,
              duration: (end - start) * 1000,
            },
          ],
          sub: [],
        },
      },
    });
  });
  await app.page.route("**/api/preview/*/start/*/end/*", (route) =>
    route.fulfill({
      json: ["front_door", "backyard"].map((camera) => ({
        camera,
        start: Math.floor(timestamp / 3600) * 3600,
        end: Math.floor(timestamp / 3600) * 3600 + 3600,
        src: "/metadata-preview.mp4",
        type: "video/mp4",
      })),
    }),
  );
  await app.page.route("**/metadata-preview.mp4", (route) =>
    route.fulfill({ contentType: "video/mp4", body: ONVIF_VIDEO_ONLY_MP4 }),
  );
  await app.page.route("**/vod/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/master.m3u8")) {
      manifests.push(path);
      return route.fulfill({
        contentType: "application/vnd.apple.mpegurl",
        body:
          '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=64000,CODECS="avc1.42c00a"\n' +
          "index.m3u8\n",
      });
    }
    if (path.endsWith("/index.m3u8")) {
      return route.fulfill({
        contentType: "application/vnd.apple.mpegurl",
        body: mediaPlaylist,
      });
    }
    if (path.endsWith("/init.mp4")) {
      return route.fulfill({
        contentType: "video/mp4",
        body: initialization,
      });
    }
    const match = path.match(/\/segment-(\d+)\.m4s$/);
    if (match && Number(match[1]) < fragmentCount) {
      return route.fulfill({
        contentType: "video/mp4",
        body: videoFragment(Number(match[1])),
      });
    }
    return route.abort("failed");
  });
  return { manifests };
}

const toggle = (app: FrigateApp) =>
  app.page.getByRole("button", { name: "Review metadata", exact: true });
const panel = (app: FrigateApp) =>
  app.page.getByRole("region", { name: "Review metadata", exact: true });
const go = async (app: FrigateApp) => {
  await app.goto(`/review?timestamp=front_door_${timestamp}`);
  await app.page.getByText("Investigation tools", { exact: true }).click();
};

async function switchCamera(app: FrigateApp) {
  if (app.isMobile) {
    await app.page
      .getByRole("button", { name: "Cameras", exact: true })
      .click();
    await app.page
      .getByRole("dialog")
      .getByText("Backyard", { exact: true })
      .click();
  } else {
    await app.page.locator('[data-camera="backyard"]').click();
  }
}

test.describe("Review metadata @mobile", () => {
  test.use({
    expectedErrors: [
      [
        /503|Service Unavailable/,
        /failed: net::ERR_ABORTED .*\/api\/event_ids\?/,
      ],
      { scope: "test" },
    ],
  });

  test("GenAI-disabled Review lazily shows selected stored fields without touching playback", async ({
    frigateApp: app,
  }, testInfo) => {
    const playback = await installReview(app);
    const requests: string[] = [];
    await app.page.route("**/api/event_ids?**", (route) => {
      requests.push(route.request().url());
      return route.fulfill({
        json: [
          event(),
          event("unrequested-person"),
          event("missing-person", "backyard"),
        ],
      });
    });
    await go(app);
    await expect(toggle(app)).toHaveAttribute("aria-expanded", "false");
    expect(requests).toHaveLength(0);
    const video = app.page.locator('video[src^="blob:"]').first();
    await expect(video).toHaveJSProperty("videoWidth", 16);
    await expect
      .poll(() =>
        video.evaluate((element) => (element as HTMLVideoElement).readyState),
      )
      .toBeGreaterThanOrEqual(2);
    await expect(video).toHaveJSProperty("error", null);
    const mainLoads = () =>
      playback.manifests.filter((path) => path.startsWith("/vod/front_door/"));
    const sourceLoads = mainLoads().length;
    expect(sourceLoads).toBeGreaterThan(0);
    const mainElement = await video.elementHandle();
    if (!mainElement) throw new Error("Main recording player was not found");
    const media = await mainElement.evaluate((element) => {
      if (!(element instanceof HTMLVideoElement))
        throw new Error("Expected the main recording video");
      return {
        src: element.getAttribute("src"),
        paused: element.paused,
        connected: element.isConnected,
      };
    });
    await toggle(app).click();
    await expect(panel(app).getByText("92.5%", { exact: true })).toBeVisible();
    expect(requests).toHaveLength(1);
    expect(new URL(requests[0]).searchParams.get("ids")).toBe(
      "retained-person,missing-person",
    );
    await expect(
      panel(app).getByText("front_door-metadata-review"),
    ).toBeVisible();
    await expect(
      panel(app).getByText("Local + archive", { exact: true }),
    ).toBeVisible();
    await expect(panel(app).getByText(/Stored fixture summary/)).toBeVisible();
    await expect(panel(app).getByText("fixture-model-hash")).toBeVisible();
    await expect(panel(app).getByText("0.1, 0.2, 0.3, 0.4")).toBeVisible();
    await expect(
      panel(app).getByText(/1 requested event is not available/),
    ).toBeVisible();
    await expect(
      panel(app).getByText(/not a verified camera clock/),
    ).toBeVisible();
    await expect(
      panel(app).getByText(/historical context is not yet recorded/),
    ).toHaveCount(2);
    await expect(panel(app)).not.toContainText("SECRET_");
    await expect(panel(app)).not.toContainText("unrequested-person");
    expect(
      await mainElement.evaluate((element) => {
        if (!(element instanceof HTMLVideoElement))
          throw new Error("Expected the main recording video");
        return {
          src: element.getAttribute("src"),
          paused: element.paused,
          connected: element.isConnected,
        };
      }),
    ).toEqual(media);
    expect(mainLoads()).toHaveLength(sourceLoads);
    await expect(video).toHaveJSProperty("error", null);
    await expect(
      app.page.getByText(/Recording playback failed|manifestParsingError/),
    ).toHaveCount(0);
    const screenshotPath = testInfo.outputPath(
      `review-metadata-${testInfo.project.name}.png`,
    );
    await app.page.screenshot({ path: screenshotPath, fullPage: true });
    await testInfo.attach("Review metadata", {
      path: screenshotPath,
      contentType: "image/png",
    });
  });

  test("limits unique requested IDs to 50 and explicitly reports missing retained events", async ({
    frigateApp: app,
  }) => {
    const ids = Array.from({ length: 55 }, (_, index) => `event-${index}`);
    await installReview(app, [review("front_door", [...ids, ids[0]])]);
    const requests: string[][] = [];
    await app.page.route("**/api/event_ids?**", (route) => {
      requests.push(
        new URL(route.request().url()).searchParams.get("ids")!.split(","),
      );
      return route.fulfill({ json: [] });
    });
    await go(app);
    await toggle(app).click();
    await expect(
      panel(app).getByText(/55 linked event IDs; requesting 50 \(limit 50\)/),
    ).toBeVisible();
    await expect(
      panel(app).getByText(/50 requested events are not available/),
    ).toBeVisible();
    expect(requests).toEqual([ids.slice(0, 50)]);
  });

  test("no linked IDs and no overlapping Review have explicit clear states and never fetch", async ({
    frigateApp: app,
  }) => {
    await installReview(app, [review("front_door", [])]);
    const requests: string[] = [];
    await app.page.route("**/api/event_ids?**", (route) => {
      requests.push(route.request().url());
      return route.fulfill({ json: [] });
    });
    await go(app);
    await toggle(app).click();
    await expect(
      panel(app).getByText("This review has no linked event IDs."),
    ).toBeVisible();
    await switchCamera(app);
    await expect(toggle(app)).toHaveAttribute("aria-expanded", "false");
    await toggle(app).click();
    await expect(
      panel(app).getByText(/No loaded review event overlaps/),
    ).toBeVisible();
    expect(requests).toHaveLength(0);
  });

  test("errors are explicit and retry requests only the current review", async ({
    frigateApp: app,
  }) => {
    await installReview(app);
    let attempts = 0;
    await app.page.route("**/api/event_ids?**", (route) =>
      ++attempts === 1
        ? route.fulfill({ status: 503, json: {} })
        : route.fulfill({ json: [event()] }),
    );
    await go(app);
    await toggle(app).click();
    await expect(panel(app).getByRole("alert")).toContainText(
      /could not be loaded/,
    );
    await panel(app).getByRole("button", { name: "Retry metadata" }).click();
    await expect(panel(app).getByText("92.5%", { exact: true })).toBeVisible();
    expect(attempts).toBe(2);
  });

  test("changing playback time cancels superseded Review metadata without seeking from the panel", async ({
    frigateApp: app,
  }) => {
    await installReview(app, [
      { ...review(), end_time: timestamp + 2 },
      {
        ...review("front_door", ["later-person"]),
        id: "later-review",
        start_time: timestamp + 3,
        end_time: timestamp + 900,
      },
    ]);
    const held: Route[] = [];
    let aborted = false;
    app.page.on("requestfailed", (request) => {
      if (request.url().includes("/event_ids?")) aborted = true;
    });
    await app.page.route("**/api/event_ids?**", (route) => {
      held.push(route);
    });
    try {
      await go(app);
      await toggle(app).click();
      await expect.poll(() => held.length).toBe(1);
      await app.page
        .getByText("Activity navigation and exact time", { exact: true })
        .click();
      await app.page
        .getByRole("textbox", { name: "Go to capture date/time" })
        .fill(new Date((timestamp + 5) * 1000).toISOString());
      await app.page.getByRole("button", { name: "Go", exact: true }).click();
      await expect.poll(() => aborted).toBe(true);
      await expect(toggle(app)).toHaveAttribute("aria-expanded", "false");
      await toggle(app).click();
      await expect.poll(() => held.length).toBe(2);
      expect(new URL(held[1].request().url()).searchParams.get("ids")).toBe(
        "later-person",
      );
      await held[1].fulfill({ json: [event("later-person")] });
      await expect(
        panel(app).getByText("later-review", { exact: true }),
      ).toBeVisible();
      await expect(panel(app)).not.toContainText("front_door-metadata-review");
    } finally {
      for (const route of held)
        await route.fulfill({ json: [event()] }).catch(() => {});
    }
  });

  test("unknown or malformed selected fields are unavailable, never raw payloads", async ({
    frigateApp: app,
  }) => {
    await installReview(app, [{ ...review(), recording_storage: undefined }]);
    await app.page.route("**/api/event_ids?**", (route) =>
      route.fulfill({
        json: [
          {
            ...event(),
            sub_label: { secret: "SECRET_IDENTITY" },
            model_hash: null,
            data: {
              top_score: 9,
              box: [1, 2],
              region: "SECRET_GEOMETRY",
              unknown: "SECRET_DATA",
            },
          },
        ],
      }),
    );
    await go(app);
    await toggle(app).click();
    const retained = panel(app).getByRole("article");
    await expect(retained).toHaveCount(1);
    await expect(retained).toContainText("Not available");
    await expect(panel(app)).not.toContainText("SECRET_");
    await expect(panel(app)).not.toContainText("900%");
    await expect(panel(app)).not.toContainText("Local + archive");
  });

  test("closing, switching cameras and unmounting abort requests and discard late results", async ({
    frigateApp: app,
  }) => {
    await installReview(app);
    const held: Route[] = [];
    const aborted: string[] = [];
    app.page.on("requestfailed", (request) => {
      if (request.url().includes("/event_ids?")) aborted.push(request.url());
    });
    await app.page.route("**/api/event_ids?**", (route) => {
      held.push(route);
    });
    try {
      await go(app);
      await toggle(app).click();
      await expect(
        panel(app).getByText("Loading retained event metadata..."),
      ).toBeVisible();
      await expect.poll(() => held.length).toBe(1);
      await toggle(app).click();
      await expect.poll(() => aborted.length).toBe(1);
      await toggle(app).click();
      await expect.poll(() => held.length).toBe(2);
      await switchCamera(app);
      await expect.poll(() => aborted.length).toBe(2);
      await expect(toggle(app)).toHaveAttribute("aria-expanded", "false");
      await toggle(app).click();
      await expect.poll(() => held.length).toBe(3);
      expect(new URL(held[2].request().url()).searchParams.get("ids")).toBe(
        "backyard-person",
      );
      await held[2].fulfill({ json: [event("backyard-person", "backyard")] });
      await expect(
        panel(app).getByText("backyard-person", { exact: true }),
      ).toBeVisible();
      await expect(panel(app)).not.toContainText("front_door-metadata-review");
      await toggle(app).click();
      await toggle(app).click();
      await expect.poll(() => held.length).toBe(4);
      await app.page
        .getByRole("button", { name: "Go back", exact: true })
        .click();
      await expect.poll(() => aborted.length).toBe(3);
    } finally {
      for (const route of held) {
        await route.fulfill({ json: [event()] }).catch(() => {});
      }
    }
  });

  test("does not query retained events before the profile identity is ready", async ({
    frigateApp: app,
  }) => {
    await installReview(app);
    let release = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    let profileStarted = false;
    await app.page.route("**/profile", async (route) => {
      profileStarted = true;
      await gate;
      return route.fulfill({ json: adminProfile() });
    });
    const requests: string[] = [];
    await app.page.route("**/api/event_ids?**", (route) => {
      requests.push(route.request().url());
      return route.fulfill({ json: [] });
    });
    try {
      await app.page.goto(`/review?timestamp=front_door_${timestamp}`);
      await expect.poll(() => profileStarted).toBe(true);
      await expect(toggle(app)).toHaveCount(0);
      expect(requests).toHaveLength(0);
      release();
      await app.page.getByText("Investigation tools", { exact: true }).click();
      await expect(toggle(app)).toBeVisible();
      expect(requests).toHaveLength(0);
      await toggle(app).click();
      await expect(
        panel(app).getByText(/requested events are not available/),
      ).toBeVisible();
      expect(requests).toHaveLength(1);
    } finally {
      release();
    }
  });

  test("profile username and role changes discard account-scoped results and expansion", async ({
    frigateApp: app,
  }) => {
    await installReview(app);
    let profile = adminProfile();
    let refreshedIdentity = false;
    await app.page.route("**/profile", (route) => {
      if (profile.username === "other-account") refreshedIdentity = true;
      return route.fulfill({ json: profile });
    });
    const requests: string[] = [];
    await app.page.route("**/api/event_ids?**", (route) => {
      requests.push(profile.username);
      return route.fulfill({
        json: [{ ...event(), sub_label: `${profile.username}-only-identity` }],
      });
    });
    await go(app);
    await toggle(app).click();
    await expect(
      panel(app).getByText("admin-only-identity", { exact: true }),
    ).toBeVisible();
    profile = adminProfile({ username: "other-account", role: "viewer" });
    await expect(async () => {
      await app.page.evaluate(() => {
        window.dispatchEvent(new Event("offline"));
        window.dispatchEvent(new Event("online"));
      });
      expect(refreshedIdentity).toBe(true);
    }).toPass();
    await expect(toggle(app)).toHaveAttribute("aria-expanded", "false");
    await expect(panel(app)).toHaveCount(0);
    expect(requests).toEqual(["admin"]);
    await toggle(app).click();
    await expect(
      panel(app).getByText("other-account-only-identity", { exact: true }),
    ).toBeVisible();
    await expect(panel(app)).not.toContainText("admin-only-identity");
    expect(requests).toEqual(["admin", "other-account"]);
  });
});
