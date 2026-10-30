import { test, expect, FrigateApp } from "../fixtures/frigate-test";
import type { Locator } from "@playwright/test";
import type { CompatibilityPlaybackStatus } from "../../src/types/playback";
import { ONVIF_VIDEO_ONLY_MP4 } from "../fixtures/mock-data/onvif-media";
import type { RecordingBookmark } from "../../src/components/player/RecordingBookmarks";

type PlaybackRequest = {
  after: number;
  before: number;
  start_timestamp: number;
  client_id: string;
  generation: number;
  stream: string;
};

async function installPlayback(
  app: FrigateApp,
  options: {
    malformedManifest?: boolean;
    videoCodec?: string;
    hevcSupported?: boolean;
    preparing?: boolean;
    storageTier?: "local" | "archive";
    storageBoundarySeconds?: number;
    holdSubsequentCoverage?: boolean;
    recordingOnly?: boolean;
    secondaryGap?: boolean;
    openTools?: boolean;
  } = {},
) {
  const {
    malformedManifest = false,
    videoCodec = "h264",
    hevcSupported = false,
    preparing = false,
  } = options;
  const playlists: string[] = [];
  const fragments: string[] = [];
  const creations: PlaybackRequest[] = [];
  const cancelled: string[] = [];
  const sessions = new Map<string, CompatibilityPlaybackStatus>();
  const coverageRequests: string[] = [];
  let coverageHeld = false;
  let releaseCoverage = () => {};
  const coverageGate = new Promise<void>((resolve) => {
    releaseCoverage = resolve;
  });
  const timestamp = Math.floor(Date.now() / 3600000) * 3600 - 3480;
  const secondaryCoverage = new Map<
    string,
    { after: number; before: number }
  >();
  if (options.recordingOnly) {
    await app.installDefaults({
      config: {
        cameras: {
          front_door: {
            capture_mode: "recording_only",
            record: { enabled: true },
          },
          backyard: {
            capture_mode: "recording_only",
            record: { enabled: true },
          },
          garage: { capture_mode: "recording_only", record: { enabled: true } },
        },
      },
    });
    await app.page.route("**/api/*/recordings/coverage?**", (route) => {
      const url = new URL(route.request().url());
      const camera = url.pathname.split("/")[2];
      const after = Number(url.searchParams.get("after"));
      const before = Number(url.searchParams.get("before"));
      secondaryCoverage.set(camera, { after, before });
      const spans = options.secondaryGap
        ? [
            { start_time: after, end_time: timestamp + 10 },
            { start_time: timestamp + 20, end_time: before },
          ]
        : [{ start_time: after, end_time: before }];
      const timeline = spans.map((span, index) => ({
        ...span,
        duration: (span.end_time - span.start_time) * 1000 + index * 2000,
        lead_in_ms: index * 2000,
      }));
      return route.fulfill({
        json: {
          spans: spans.map((span) => ({ ...span, streams: ["main"] })),
          streams: { main: { video_codec: "h264" } },
          timelines: { auto: timeline, main: timeline, sub: [] },
        },
      });
    });
  }
  if (options.storageTier) {
    await app.page.route("**/api/preview/*/start/*/end/*", (route) =>
      route.fulfill({
        json: [
          {
            camera: "front_door",
            start: Math.floor(timestamp / 3600) * 3600,
            end: Math.floor(timestamp / 3600) * 3600 + 3600,
            src: "/fixture-archive-preview.mp4",
            type: "video/mp4",
          },
        ],
      }),
    );
    await app.page.route("**/fixture-archive-preview.mp4", (route) =>
      route.fulfill({ contentType: "video/mp4", body: ONVIF_VIDEO_ONLY_MP4 }),
    );
  }
  if (videoCodec === "hevc" && !hevcSupported) {
    await app.page.addInitScript(() => {
      const supports = MediaSource.isTypeSupported.bind(MediaSource);
      MediaSource.isTypeSupported = (mime) =>
        /hvc1|hev1/i.test(mime) ? false : supports(mime);
      const native = HTMLMediaElement.prototype.canPlayType;
      HTMLMediaElement.prototype.canPlayType = function (mime) {
        return /hvc1|hev1/i.test(mime) ? "" : native.call(this, mime);
      };
    });
  }
  let releasePlaylists = () => {};
  let releaseFragments = () => {};
  const playlistGate = new Promise<void>((resolve) => {
    releasePlaylists = resolve;
  });
  const fragmentGate = new Promise<void>((resolve) => {
    releaseFragments = resolve;
  });

  await app.page.route("**/api/review/activity/motion?**", (route) =>
    route.fulfill({ json: [] }),
  );
  await app.page.route("**/api/recordings/unavailable?**", (route) =>
    route.fulfill({ json: [] }),
  );
  await app.page.route(
    "**/api/front_door/recordings/coverage?**",
    async (route) => {
      coverageRequests.push(route.request().url());
      if (options.holdSubsequentCoverage && coverageHeld) await coverageGate;
      const url = new URL(route.request().url());
      const start = Number(url.searchParams.get("after"));
      const end = Number(url.searchParams.get("before"));
      const timeline = [
        { start_time: start, end_time: end, duration: (end - start) * 1000 },
      ];
      return route.fulfill({
        json: {
          spans: [{ start_time: start, end_time: end, streams: ["main"] }],
          codecs_compatible: true,
          streams: {
            main: {
              video_codec: videoCodec,
              audio_codec: "aac",
              audio_rate: 16000,
              has_audio: true,
              bitrate: 512000,
            },
          },
          timelines: { auto: timeline, main: timeline, sub: [] },
          storage_spans: options.storageTier
            ? [
                {
                  start_time: start,
                  end_time: Math.min(
                    end,
                    options.storageBoundarySeconds != null
                      ? timestamp + options.storageBoundarySeconds
                      : end,
                  ),
                  tiers: { main: options.storageTier },
                },
                ...(options.storageBoundarySeconds != null
                  ? [
                      {
                        start_time: Math.max(
                          start,
                          timestamp + options.storageBoundarySeconds,
                        ),
                        end_time: end,
                        tiers: { main: "local" },
                      },
                    ]
                  : []),
              ].filter((span) => span.start_time < span.end_time)
            : undefined,
        },
      });
    },
  );

  const compatibleBase = "/api/front_door/recordings/playback-compatibility";
  await app.page.route(`**${compatibleBase}`, (route) => {
    const body: PlaybackRequest = route.request().postDataJSON();
    creations.push(body);
    const id = `compat-${creations.length}`;
    const status: CompatibilityPlaybackStatus = {
      id,
      camera: "front_door",
      client_id: body.client_id,
      generation: body.generation,
      state: preparing ? "preparing" : "complete",
      manifest_path: `front_door/recordings/playback-compatibility/${id}/media/index.m3u8`,
      timeline: preparing
        ? []
        : [
            {
              start_time: body.after,
              end_time: body.before,
              duration: body.before - body.after,
              measured: true,
              lead_in_ms: 0,
            },
          ],
      encoder: "libx264 (software)",
      error: null,
      after: body.after,
      before: body.before,
      start_timestamp: body.start_timestamp,
      ready_through: preparing ? null : body.before,
    };
    sessions.set(id, status);
    return route.fulfill({ status: 202, json: status });
  });
  await app.page.route(`**${compatibleBase}/**`, async (route) => {
    const path = new URL(route.request().url()).pathname;
    const id = path.slice(compatibleBase.length + 1).split("/")[0];
    if (!path.includes("/media/")) {
      if (route.request().method() === "DELETE") {
        cancelled.push(id);
        return route.fulfill({ status: 204 });
      }
      return route.fulfill({ json: sessions.get(id) });
    }
    if (path.endsWith(".m3u8")) {
      playlists.push(path);
      await playlistGate;
      return route.fulfill({
        contentType: "application/vnd.apple.mpegurl",
        body:
          "#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:10\n#EXT-X-PLAYLIST-TYPE:VOD\n" +
          Array.from(
            { length: 3 },
            (_, index) => `#EXTINF:10,\nseg-${index + 1}.m4s\n`,
          ).join("") +
          "#EXT-X-ENDLIST\n",
      });
    }
    fragments.push(path);
    await fragmentGate;
    return route.fulfill({ contentType: "video/mp4", body: Buffer.alloc(0) });
  });

  await app.page.route("**/vod/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith(".m3u8")) {
      playlists.push(path);
      if (path.endsWith("/master.m3u8")) {
        return route.fulfill({
          contentType: "application/vnd.apple.mpegurl",
          body: malformedManifest
            ? "Not an HLS playlist"
            : '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=512000,CODECS="avc1.42e01e,mp4a.40.2"\nindex-v1-a1.m3u8\n',
        });
      }
      await playlistGate;
      return route.fulfill({
        contentType: "application/vnd.apple.mpegurl",
        body:
          "#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:10\n#EXT-X-PLAYLIST-TYPE:VOD\n" +
          Array.from(
            { length: 12 },
            (_, index) => `#EXTINF:10,\nseg-${index + 1}.m4s\n`,
          ).join("") +
          "#EXT-X-ENDLIST\n",
      });
    }
    fragments.push(path);
    await fragmentGate;
    return route.fulfill({ contentType: "video/mp4", body: Buffer.alloc(0) });
  });

  // The fixture holds media responses, so they must not gate page readiness.
  await app.page.goto(`/review?timestamp=front_door_${timestamp}`, {
    waitUntil: "domcontentloaded",
  });
  const video = app.page.locator('video[src^="blob:"]').first();
  await expect(video).toBeAttached({ timeout: 15000 });
  if (options.openTools !== false) {
    await app.page.getByText("Investigation tools", { exact: true }).click();
  }
  return {
    video,
    playlists,
    fragments,
    creations,
    cancelled,
    timestamp,
    releasePlaylists,
    coverageRequests,
    secondaryCoverage,
    holdCoverage: () => {
      coverageHeld = true;
    },
    release: () => {
      releaseCoverage();
      releasePlaylists();
      releaseFragments();
    },
  };
}

async function makePlayable(video: Locator, bufferEnd = 60, preview = false) {
  await video.evaluate((element, end) => {
    if (!(element instanceof HTMLVideoElement)) throw Error("Expected video");
    let position = 0,
      paused = true,
      ready = 2;
    Object.defineProperties(element, {
      videoWidth: { configurable: true, value: 1280 },
      videoHeight: { configurable: true, value: 720 },
      readyState: { configurable: true, get: () => ready },
      duration: { configurable: true, value: 120 },
      buffered: {
        configurable: true,
        value: { length: 1, start: () => 0, end: () => end },
      },
      paused: { configurable: true, get: () => paused },
      currentTime: {
        configurable: true,
        get: () => position,
        set: (value: number) => {
          position = value;
          ready = 1;
          element.dispatchEvent(new Event("seeking"));
          setTimeout(() => {
            ready = 2;
            element.dispatchEvent(new Event("seeked"));
            element.dispatchEvent(new Event("timeupdate"));
          }, 0);
        },
      },
      play: {
        configurable: true,
        value: async () => {
          paused = false;
          element.dispatchEvent(new Event("play"));
          element.dispatchEvent(new Event("playing"));
          element.dispatchEvent(new Event("timeupdate"));
        },
      },
      pause: {
        configurable: true,
        value: () => {
          if (paused) return;
          paused = true;
          element.dispatchEvent(new Event("pause"));
        },
      },
      requestVideoFrameCallback: {
        configurable: true,
        value: (callback: VideoFrameRequestCallback) =>
          window.setTimeout(
            () =>
              callback(performance.now(), {
                mediaTime: position,
                presentationTime: performance.now(),
                expectedDisplayTime: performance.now(),
                width: 1280,
                height: 720,
                presentedFrames: 1,
              }),
            0,
          ),
      },
      cancelVideoFrameCallback: {
        configurable: true,
        value: (id: number) => window.clearTimeout(id),
      },
    });
    element.dispatchEvent(new Event("loadeddata"));
  }, bufferEnd);
  if (preview) {
    await expect(video).toHaveJSProperty("paused", true);
    return;
  }
  await expect
    .poll(() =>
      video.evaluate(
        (element) => element instanceof HTMLVideoElement && !element.paused,
      ),
    )
    .toBe(true);
  await video.evaluate((element) => {
    if (!(element instanceof HTMLVideoElement)) throw Error("Expected video");
    element.currentTime = 2;
    element.dispatchEvent(new Event("timeupdate"));
  });
}

async function failDecoder(video: Locator, audio: boolean, duplicate = false) {
  await video.evaluate(
    (element, { audio, duplicate }) => {
      if (!(element instanceof HTMLVideoElement)) {
        throw new Error("Expected the recording video element");
      }
      Object.defineProperty(element, "error", {
        configurable: true,
        value: {
          code: MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED,
          message: audio
            ? "PipelineStatus::DECODER_ERROR_NOT_SUPPORTED: audio decoder initialization failed with DecoderStatus::Codes::kUnsupportedConfig"
            : "PipelineStatus::DECODER_ERROR_NOT_SUPPORTED: video decoder initialization failed",
        },
      });
      element.dispatchEvent(new Event("error"));
      if (duplicate) {
        element.dispatchEvent(new Event("error"));
      }
      Reflect.deleteProperty(element, "error");
    },
    { audio, duplicate },
  );
}

test.describe("Compact Review investigation tools @mobile", () => {
  test("daily playback stays prominent and collapsing tools preserves drafts and selections", async ({
    frigateApp,
  }) => {
    let bookmarkReads = 0;
    await frigateApp.page.route(
      "**/api/front_door/recordings/bookmarks**",
      (route) => {
        bookmarkReads += 1;
        return route.fulfill({ json: { items: [], has_more: false } });
      },
    );
    const playback = await installPlayback(frigateApp, { openTools: false });
    try {
      await makePlayable(playback.video);
      const { page } = frigateApp;
      const toolbar = page.getByRole("region", {
        name: "Recording investigation controls",
      });
      await expect(toolbar.locator("time").first()).toBeVisible();
      await expect(
        toolbar.getByRole("button", { name: "Copy timestamp link" }),
      ).toBeVisible();
      await expect(
        toolbar.getByRole("button", { name: "Mark In", exact: true }),
      ).toHaveCount(0);
      expect(bookmarkReads).toBe(0);
      const summary = toolbar
        .getByText("Investigation tools", { exact: true })
        .locator("..");
      await summary.focus();
      await summary.press("Enter");
      expect(bookmarkReads).toBe(0);
      await toolbar
        .getByRole("button", { name: "Mark In", exact: true })
        .click();
      await toolbar
        .getByText("Bookmarks and incident notes", { exact: true })
        .click();
      await expect.poll(() => bookmarkReads).toBeGreaterThan(0);
      await toolbar
        .getByRole("button", { name: "Add bookmark", exact: true })
        .click();
      await toolbar
        .getByLabel("Bookmark title", { exact: true })
        .fill("Keep this draft");
      await summary.press("Enter");
      await expect(
        toolbar.getByRole("button", { name: "Mark In", exact: true }),
      ).toHaveCount(0);
      await expect(toolbar.getByText("Active investigation")).toBeVisible();
      await expect(toolbar.getByText(/^In point:/)).toBeVisible();
      await summary.press("Enter");
      await expect(
        toolbar.getByLabel("Bookmark title", { exact: true }),
      ).toHaveValue("Keep this draft");
    } finally {
      playback.release();
    }
  });
});

test.describe("Recording-only synchronized camera tiles", () => {
  test.use({ expectedErrors: [/net::ERR_ABORTED.*\/vod\//] });
  test("secondary recordings follow the main timestamp without preview frames", async ({
    frigateApp,
  }, testInfo) => {
    test.skip(
      testInfo.project.name !== "desktop",
      "camera strip is desktop-only",
    );
    const playback = await installPlayback(frigateApp, { recordingOnly: true });
    try {
      await expect(
        frigateApp.page.locator('[data-testid="recording-preview"] video'),
      ).toHaveCount(0);
      expect(
        playback.playlists.some((path) => !path.includes("/vod/front_door/")),
      ).toBe(false);
      await makePlayable(playback.video);
      const tile = frigateApp.page.locator(
        '[data-testid="recording-preview"][data-camera="backyard"]',
      );
      const video = tile.locator("video");
      await expect(video).toBeAttached();
      await makePlayable(video, 60, true);
      const initialWindow = playback.secondaryCoverage.get("backyard")!;
      expect(initialWindow.before - initialWindow.after).toBeLessThanOrEqual(
        60,
      );
      expect(playback.timestamp - initialWindow.after).toBeLessThan(2);
      await expect(video).toHaveJSProperty("muted", true);
      await expect(video).toHaveJSProperty("controls", false);
      await expect
        .poll(() =>
          playback.playlists.some(
            (path) =>
              path.includes("/vod/backyard/") &&
              path.endsWith("/index-v1.m3u8"),
          ),
        )
        .toBe(true);
      expect(
        playback.playlists
          .filter((path) => path.includes("/vod/backyard/"))
          .every((path) => path.endsWith("/index-v1.m3u8")),
      ).toBe(true);
      expect(
        playback.playlists.some(
          (path) =>
            path.includes("/vod/front_door/") && path.endsWith("/master.m3u8"),
        ),
      ).toBe(true);
      for (const position of [12, 12.25, 35, 4]) {
        await playback.video.evaluate((element: HTMLVideoElement, time) => {
          element.currentTime = time;
          element.dispatchEvent(new Event("timeupdate"));
        }, position);
        await expect(tile).toHaveAttribute(
          "data-timestamp",
          String(playback.timestamp + position),
        );
        await expect(video).toHaveJSProperty(
          "currentTime",
          playback.timestamp -
            playback.secondaryCoverage.get("backyard")!.after +
            position,
        );
        await expect(video).toHaveJSProperty("paused", true);
      }
      await expect(
        frigateApp.page.getByText(/No Preview Found for/),
      ).toHaveCount(0);
      await playback.video.evaluate((element: HTMLVideoElement) => {
        element.currentTime = 75;
        element.dispatchEvent(new Event("timeupdate"));
      });
      await expect
        .poll(() => playback.secondaryCoverage.get("backyard")?.after)
        .toBe(Math.floor((playback.timestamp + 75) / 2) * 2);
      await makePlayable(video, 60, true);
      await expect(video).toHaveJSProperty("currentTime", 1);
      await playback.video.evaluate((element: HTMLVideoElement) => {
        element.currentTime = 4;
        element.dispatchEvent(new Event("timeupdate"));
      });
      await expect
        .poll(() => playback.secondaryCoverage.get("backyard")?.after)
        .toBe(playback.timestamp + 4);
      await makePlayable(video, 60, true);
      await expect(video).toHaveJSProperty("currentTime", 0);
      await tile.evaluate((element) => {
        element.style.transform = "translateY(200vh)";
      });
      await expect(video).toHaveCount(0);
      await tile.evaluate((element) => {
        element.style.transform = "";
      });
      await expect(video).toBeAttached();
    } finally {
      playback.release();
    }
  });

  test("recording gaps hide stale frames and later media accounts for lead-in", async ({
    frigateApp,
  }, testInfo) => {
    test.skip(
      testInfo.project.name !== "desktop",
      "camera strip is desktop-only",
    );
    const playback = await installPlayback(frigateApp, {
      recordingOnly: true,
      secondaryGap: true,
    });
    try {
      await makePlayable(playback.video);
      const tile = frigateApp.page.locator(
        '[data-testid="recording-preview"][data-camera="backyard"]',
      );
      const video = tile.locator("video");
      await expect(video).toBeAttached();
      await makePlayable(video, 60, true);
      await playback.video.evaluate((element: HTMLVideoElement) => {
        element.currentTime = 15;
        element.dispatchEvent(new Event("timeupdate"));
      });
      await expect(tile.getByRole("status")).toHaveText(
        "No recordings found for this time",
      );
      await playback.video.evaluate((element: HTMLVideoElement) => {
        element.currentTime = 25;
        element.dispatchEvent(new Event("timeupdate"));
      });
      await expect(tile.getByRole("status")).toHaveCount(0);
      await expect(video).toHaveJSProperty(
        "currentTime",
        playback.timestamp -
          playback.secondaryCoverage.get("backyard")!.after +
          17,
      );
    } finally {
      playback.release();
    }
  });
});

test.describe("Recording bookmarks and notes @mobile", () => {
  test.use({ expectedErrors: [/409|Conflict/] });

  test("deleting a bookmark requires confirmation and its current revision", async ({
    frigateApp,
  }) => {
    let deleted = false;
    let revision: string | null = null;
    await frigateApp.page.route(
      "**/api/front_door/recordings/bookmarks**",
      (route) => {
        if (route.request().method() === "DELETE") {
          revision = new URL(route.request().url()).searchParams.get(
            "revision",
          );
          deleted = true;
          return route.fulfill({ status: 204 });
        }
        return route.fulfill({
          json: {
            items: deleted
              ? []
              : [
                  {
                    id: "owned-note",
                    camera: "front_door",
                    timestamp: 100,
                    end_time: null,
                    title: "Delete this annotation",
                    notes: "Not the recording",
                    owner: "admin",
                    revision: 3,
                    can_edit: true,
                    available_at_anchor: false,
                    protects_footage: false,
                  },
                ],
            has_more: false,
          },
        });
      },
    );
    const playback = await installPlayback(frigateApp);
    try {
      await makePlayable(playback.video);
      await frigateApp.page
        .getByText("Bookmarks and incident notes", { exact: true })
        .click();
      await frigateApp.page
        .getByRole("button", { name: "Delete bookmark", exact: true })
        .click();
      expect(deleted).toBe(false);
      await frigateApp.page
        .getByRole("button", { name: "Cancel", exact: true })
        .click();
      expect(deleted).toBe(false);
      await frigateApp.page
        .getByRole("button", { name: "Delete bookmark", exact: true })
        .click();
      await frigateApp.page
        .getByRole("button", { name: "Confirm bookmark deletion", exact: true })
        .click();
      await expect(
        frigateApp.page.getByText(
          /No bookmarks in this camera's selected range/,
        ),
      ).toBeVisible();
      expect(deleted).toBe(true);
      expect(revision).toBe("3");
    } finally {
      playback.release();
    }
  });

  test("saves a fixed capture anchor while playback advances", async ({
    frigateApp,
  }) => {
    const rows: RecordingBookmark[] = [];
    let created:
      { timestamp: number; title: string; notes: string } | undefined;
    await frigateApp.page.route(
      "**/api/front_door/recordings/bookmarks**",
      async (route) => {
        if (route.request().method() === "POST") {
          created = route.request().postDataJSON();
          const row = {
            ...created!,
            id: "saved-note",
            camera: "front_door",
            end_time: null,
            owner: "admin",
            revision: 1,
            can_edit: true,
            available_at_anchor: true,
            protects_footage: false,
          };
          rows.push(row);
          return route.fulfill({ status: 201, json: row });
        }
        return route.fulfill({ json: { items: rows, has_more: false } });
      },
    );
    const playback = await installPlayback(frigateApp, {
      storageTier: "archive",
    });
    try {
      await makePlayable(playback.video);
      await frigateApp.page
        .getByText("Bookmarks and incident notes", { exact: true })
        .click();
      await frigateApp.page
        .getByRole("button", { name: "Add bookmark", exact: true })
        .click();
      await playback.video.evaluate((video) => {
        if (!(video instanceof HTMLVideoElement)) throw Error("Expected video");
        video.currentTime = 12;
        video.dispatchEvent(new Event("timeupdate"));
      });
      await frigateApp.page
        .getByLabel("Bookmark title", { exact: true })
        .fill("Porch delivery");
      await frigateApp.page
        .getByLabel("Incident notes", { exact: true })
        .fill("Package left beside door");
      await frigateApp.page
        .getByRole("button", { name: "Save bookmark", exact: true })
        .click();
      await expect(
        frigateApp.page.getByText("Porch delivery", { exact: true }),
      ).toBeVisible();
      expect(created?.timestamp).toBe(playback.timestamp + 2);
      expect(created?.notes).toBe("Package left beside door");
      await expect(
        frigateApp.page.getByText(
          /Bookmarks do not protect video from retention/,
        ),
      ).toBeVisible();
    } finally {
      playback.release();
    }
  });

  test("shared readers see expired notes without owner-only actions", async ({
    frigateApp,
  }) => {
    await frigateApp.page.route(
      "**/api/front_door/recordings/bookmarks**",
      (route) =>
        route.fulfill({
          json: {
            items: [
              {
                id: "shared-note",
                camera: "front_door",
                timestamp: 100,
                end_time: null,
                title: "Historical note",
                notes: "Video has expired",
                owner: "alice",
                revision: 1,
                can_edit: false,
                available_at_anchor: false,
                protects_footage: false,
              },
            ],
            has_more: false,
          },
        }),
    );
    const playback = await installPlayback(frigateApp);
    try {
      await makePlayable(playback.video);
      await frigateApp.page
        .getByText("Bookmarks and incident notes", { exact: true })
        .click();
      await expect(
        frigateApp.page.getByText("Historical note", { exact: true }),
      ).toBeVisible();
      await expect(
        frigateApp.page.getByText(
          "No retained recording metadata at this bookmark time.",
          { exact: true },
        ),
      ).toBeVisible();
      await expect(
        frigateApp.page.getByRole("button", {
          name: "Edit bookmark",
          exact: true,
        }),
      ).toHaveCount(0);
      await expect(
        frigateApp.page.getByRole("button", {
          name: "Delete bookmark",
          exact: true,
        }),
      ).toHaveCount(0);
    } finally {
      playback.release();
    }
  });

  test("conflicting edits keep the note draft and send its original revision", async ({
    frigateApp,
  }) => {
    let revision: number | undefined;
    await frigateApp.page.route(
      "**/api/front_door/recordings/bookmarks**",
      (route) => {
        if (route.request().method() === "PATCH") {
          revision = route.request().postDataJSON().revision;
          return route.fulfill({
            status: 409,
            json: { detail: "Bookmark changed" },
          });
        }
        return route.fulfill({
          json: {
            items: [
              {
                id: "owned-note",
                camera: "front_door",
                timestamp: 100,
                end_time: null,
                title: "Original title",
                notes: "Original note",
                owner: "admin",
                revision: 2,
                can_edit: true,
                available_at_anchor: false,
                protects_footage: false,
              },
            ],
            has_more: false,
          },
        });
      },
    );
    const playback = await installPlayback(frigateApp);
    try {
      await makePlayable(playback.video);
      await frigateApp.page
        .getByText("Bookmarks and incident notes", { exact: true })
        .click();
      await frigateApp.page
        .getByRole("button", { name: "Edit bookmark", exact: true })
        .click();
      await frigateApp.page
        .getByLabel("Incident notes", { exact: true })
        .fill("My pending correction");
      const notes = frigateApp.page.getByLabel("Incident notes", {
        exact: true,
      });
      await notes.press("End");
      await notes.press("Space");
      await expect
        .poll(() =>
          playback.video.evaluate(
            (video) => video instanceof HTMLVideoElement && !video.paused,
          ),
        )
        .toBe(true);
      await frigateApp.page
        .getByRole("button", { name: "Save bookmark", exact: true })
        .click();
      await expect(
        frigateApp.page.getByText(
          /This bookmark changed or was deleted elsewhere/,
        ),
      ).toBeVisible();
      await expect(
        frigateApp.page.getByLabel("Incident notes", { exact: true }),
      ).toHaveValue("My pending correction ");
      expect(revision).toBe(2);
    } finally {
      playback.release();
    }
  });
});

test.describe("Recording playback recovery @critical @mobile", () => {
  test.use({
    // Recovery and terminal errors intentionally cancel the outgoing media load.
    expectedErrors: [
      /failed: net::ERR_ABORTED .*\/(?:vod\/front_door\/.*(?:master\.m3u8|index-v1(?:-a1)?\.m3u8|seg-\d+\.m4s)|api\/front_door\/recordings\/playback-compatibility\/[^/]+\/media\/(?:index\.m3u8|seg-\d+\.m4s)|api\/front_door\/recordings\/coverage\?[^ ]*)$/,
    ],
  });
  test("In/Out selection opens timestamp and metadata-aware exports", async ({
    frigateApp,
  }) => {
    const playback = await installPlayback(frigateApp, {
      storageTier: "archive",
    });
    try {
      await makePlayable(playback.video);
      await frigateApp.page
        .getByRole("button", { name: "Mark In", exact: true })
        .click();
      await playback.video.evaluate((video) => {
        if (!(video instanceof HTMLVideoElement)) throw Error("Expected video");
        video.currentTime = 12;
        video.dispatchEvent(new Event("timeupdate"));
      });
      await frigateApp.page
        .getByRole("button", { name: "Mark Out", exact: true })
        .click();
      await expect(
        frigateApp.page.getByRole("button", {
          name: "Loop selection",
          exact: true,
        }),
      ).toBeEnabled();
      await frigateApp.page
        .getByRole("button", { name: "Export selection", exact: true })
        .click();
      const timestamps = frigateApp.page.getByRole("combobox", {
        name: "Export timestamps",
        exact: true,
      });
      await expect(timestamps).toBeVisible();
      await timestamps.click();
      await frigateApp.page
        .getByRole("option", {
          name: "Optional timestamp subtitle track",
          exact: true,
        })
        .click();
      await frigateApp.page
        .getByText("Include investigation metadata (optional)", { exact: true })
        .click();
      await expect(
        frigateApp.page.getByText("Selected for export: None", { exact: true }),
      ).toBeVisible();
      await frigateApp.page
        .getByLabel("Export provenance", { exact: true })
        .check();
      await expect(
        frigateApp.page.getByText("Selected for export: Export provenance", {
          exact: true,
        }),
      ).toBeVisible();
      await expect(
        frigateApp.page.getByLabel("Recognized names and plates (sensitive)", {
          exact: true,
        }),
      ).not.toBeChecked();
    } finally {
      playback.release();
    }
  });

  test("loop selection seeks back without changing original footage", async ({
    frigateApp,
  }) => {
    const playback = await installPlayback(frigateApp, {
      storageTier: "archive",
    });
    try {
      await makePlayable(playback.video);
      await frigateApp.page
        .getByRole("button", { name: "Mark In", exact: true })
        .click();
      await playback.video.evaluate((video) => {
        if (!(video instanceof HTMLVideoElement)) throw Error("Expected video");
        video.currentTime = 12;
        video.dispatchEvent(new Event("timeupdate"));
      });
      await frigateApp.page
        .getByRole("button", { name: "Mark Out", exact: true })
        .click();
      await frigateApp.page
        .getByRole("button", { name: "Loop selection", exact: true })
        .click();
      await expect(
        frigateApp.page.getByRole("button", {
          name: "Loop selection",
          exact: true,
        }),
      ).toHaveAttribute("aria-pressed", "true");
      await playback.video.evaluate((video) => {
        if (!(video instanceof HTMLVideoElement)) throw Error("Expected video");
        video.currentTime = 13;
        video.dispatchEvent(new Event("timeupdate"));
      });
      await expect
        .poll(() =>
          playback.video.evaluate((video) =>
            video instanceof HTMLVideoElement ? video.currentTime : -1,
          ),
        )
        .toBeLessThan(5);
    } finally {
      playback.release();
    }
  });

  test("buffered seeks reuse the source and expose measured diagnostics", async ({
    frigateApp,
  }) => {
    const playback = await installPlayback(frigateApp, {
      storageTier: "archive",
    });
    try {
      await makePlayable(playback.video);
      const source = await playback.video.getAttribute("src");
      const requests = playback.coverageRequests.length;
      await frigateApp.page
        .getByText("Playback diagnostics", { exact: true })
        .click();
      await expect(
        frigateApp.page.getByText(/Source to first frame: \d+ ms/),
      ).toBeVisible();
      await expect(
        frigateApp.page.getByText("Recording location: Archive", {
          exact: true,
        }),
      ).toBeVisible();
      const panel = frigateApp.page.locator("details").filter({
        has: frigateApp.page.getByText("Playback diagnostics", { exact: true }),
      });
      const panelBox = await panel.boundingBox();
      const playerHeight = await panel.evaluate((element) =>
        element instanceof HTMLElement
          ? element.offsetParent?.getBoundingClientRect().height
          : undefined,
      );
      if (!panelBox || !playerHeight)
        throw new Error("Playback diagnostics must have a visible layout");
      expect(panelBox.height).toBeLessThan(playerHeight * 0.75);
      expect(panelBox.height).toBeLessThanOrEqual(playerHeight - 75);
      await frigateApp.page.keyboard.press("ArrowRight");
      await expect
        .poll(() =>
          playback.video.evaluate((video) =>
            video instanceof HTMLVideoElement ? video.currentTime : -1,
          ),
        )
        .toBeGreaterThan(10);
      await expect(
        frigateApp.page.getByText(/Latest seek to frame: \d+ ms/),
      ).toBeVisible();
      await expect(playback.video).toHaveAttribute("src", source!);
      expect(playback.coverageRequests.length).toBe(requests);
      await expect(
        frigateApp.page.getByText("1 source load", { exact: true }),
      ).toBeVisible();
    } finally {
      playback.release();
    }
  });

  test("focused play controls handle keyboard activation exactly once", async ({
    frigateApp,
  }) => {
    const playback = await installPlayback(frigateApp);
    try {
      await makePlayable(playback.video);
      await playback.video.hover({ force: true });
      await frigateApp.page
        .getByRole("button", { name: "Pause recording", exact: true })
        .focus();
      await frigateApp.page.keyboard.press("Space");
      await expect
        .poll(() =>
          playback.video.evaluate(
            (video) => video instanceof HTMLVideoElement && video.paused,
          ),
        )
        .toBe(true);
      await frigateApp.page
        .getByRole("button", { name: "Play recording", exact: true })
        .focus();
      await frigateApp.page.keyboard.press("Space");
      await expect
        .poll(() =>
          playback.video.evaluate(
            (video) => video instanceof HTMLVideoElement && !video.paused,
          ),
        )
        .toBe(true);
    } finally {
      playback.release();
    }
  });

  test("hidden playback suspends loading and foreground playback resumes", async ({
    frigateApp,
  }) => {
    const playback = await installPlayback(frigateApp);
    try {
      await makePlayable(playback.video);
      await frigateApp.page.evaluate(() => {
        Object.defineProperty(document, "visibilityState", {
          configurable: true,
          value: "hidden",
        });
        document.dispatchEvent(new Event("visibilitychange"));
      });
      await expect(playback.video).toHaveAttribute(
        "data-playback-suspended",
        "true",
      );
      await expect
        .poll(() =>
          playback.video.evaluate(
            (video) => video instanceof HTMLVideoElement && video.paused,
          ),
        )
        .toBe(true);
      await frigateApp.page.evaluate(() => {
        Object.defineProperty(document, "visibilityState", {
          configurable: true,
          value: "visible",
        });
        document.dispatchEvent(new Event("visibilitychange"));
      });
      await expect(playback.video).toHaveAttribute(
        "data-playback-suspended",
        "false",
      );
      await expect
        .poll(() =>
          playback.video.evaluate(
            (video) => video instanceof HTMLVideoElement && !video.paused,
          ),
        )
        .toBe(true);
    } finally {
      playback.release();
    }
  });

  test("picture-in-picture and explicit background permission keep playback active", async ({
    frigateApp,
  }) => {
    const playback = await installPlayback(frigateApp);
    try {
      await makePlayable(playback.video);
      await playback.video.evaluate((video) => {
        Object.defineProperty(document, "pictureInPictureElement", {
          configurable: true,
          value: video,
        });
        Object.defineProperty(document, "visibilityState", {
          configurable: true,
          value: "hidden",
        });
        document.dispatchEvent(new Event("visibilitychange"));
      });
      await expect(playback.video).toHaveAttribute(
        "data-playback-suspended",
        "false",
      );
      await frigateApp.page.evaluate(() => {
        Object.defineProperty(document, "visibilityState", {
          configurable: true,
          value: "visible",
        });
        Object.defineProperty(document, "pictureInPictureElement", {
          configurable: true,
          value: null,
        });
        document.dispatchEvent(new Event("visibilitychange"));
      });
      await frigateApp.page
        .getByText("Playback diagnostics", { exact: true })
        .click();
      await frigateApp.page
        .getByLabel("Keep recorded video playing in background", {
          exact: true,
        })
        .check();
      await frigateApp.page.evaluate(() => {
        Object.defineProperty(document, "visibilityState", {
          configurable: true,
          value: "hidden",
        });
        document.dispatchEvent(new Event("visibilitychange"));
      });
      await expect(playback.video).toHaveAttribute(
        "data-playback-suspended",
        "false",
      );
      await expect
        .poll(() =>
          playback.video.evaluate(
            (video) => video instanceof HTMLVideoElement && !video.paused,
          ),
        )
        .toBe(true);
    } finally {
      playback.release();
    }
  });

  test("rapid unbuffered seeks abort obsolete coverage work", async ({
    frigateApp,
  }) => {
    const playback = await installPlayback(frigateApp, {
      holdSubsequentCoverage: true,
    });
    const aborted: string[] = [];
    frigateApp.page.on("requestfailed", (request) => {
      if (request.url().includes("/recordings/coverage?"))
        aborted.push(request.url());
    });
    try {
      await makePlayable(playback.video, 5);
      const before = playback.coverageRequests.length;
      playback.holdCoverage();
      await frigateApp.page.keyboard.press("ArrowRight");
      await expect
        .poll(() => playback.coverageRequests.length)
        .toBeGreaterThan(before);
      const superseded = playback.coverageRequests.at(-1);
      await frigateApp.page.keyboard.press("ArrowRight");
      await expect
        .poll(() => playback.coverageRequests.length)
        .toBeGreaterThan(before + 1);
      await expect.poll(() => aborted.includes(superseded!)).toBe(true);
      await frigateApp.page.goto("/system#storage");
      await expect.poll(() => aborted.length).toBeGreaterThan(0);
      expect(playback.creations).toHaveLength(0);
    } finally {
      playback.release();
    }
  });

  test("Review labels archived playback and removes the tag across a local boundary", async ({
    frigateApp,
  }) => {
    const playback = await installPlayback(frigateApp, {
      storageTier: "archive",
      storageBoundarySeconds: 15,
    });
    try {
      await playback.video.evaluate((video) => {
        Object.defineProperties(video, {
          videoWidth: { configurable: true, value: 1280 },
          videoHeight: { configurable: true, value: 720 },
          readyState: { configurable: true, value: 2 },
          play: { configurable: true, value: () => Promise.resolve() },
        });
        video.dispatchEvent(new Event("loadeddata"));
      });
      await expect(async () => {
        await playback.video.evaluate((video) => {
          Object.defineProperty(video, "currentTime", {
            configurable: true,
            writable: true,
            value: 5,
          });
          video.dispatchEvent(new Event("playing"));
          video.dispatchEvent(new Event("timeupdate"));
        });
        await expect(
          frigateApp.page.getByRole("status").filter({ hasText: /^Archived$/ }),
        ).toBeVisible();
      }).toPass({ timeout: 8000 });
      await playback.video.evaluate((video) => {
        Object.defineProperty(video, "currentTime", {
          configurable: true,
          writable: true,
          value: 20,
        });
        video.dispatchEvent(new Event("timeupdate"));
      });
      await expect(
        frigateApp.page.getByRole("status").filter({ hasText: /^Archived$/ }),
      ).toHaveCount(0);
    } finally {
      playback.release();
    }
  });

  test("global Review switch prevents compatibility sessions and explains original-only failure", async ({
    frigateApp,
  }) => {
    await frigateApp.installDefaults({
      config: { ui: { recording_compatibility: false } },
    });
    const playback = await installPlayback(frigateApp, {
      videoCodec: "hevc",
      hevcSupported: false,
    });
    try {
      await failDecoder(playback.video, false);
      await expect(
        frigateApp.page.getByText(
          "Review compatibility playback is disabled by an administrator. This recording must play in its original format.",
          { exact: true },
        ),
      ).toBeVisible();
      expect(playback.creations).toHaveLength(0);
    } finally {
      playback.release();
    }
  });

  test("unsupported audio keeps supported original video without H264 conversion", async ({
    frigateApp,
  }) => {
    const playback = await installPlayback(frigateApp);
    try {
      await expect
        .poll(() =>
          playback.playlists.some((p) => p.endsWith("index-v1-a1.m3u8")),
        )
        .toBe(true);
      await failDecoder(playback.video, true, true);
      await expect
        .poll(() =>
          playback.playlists.some((path) => path.endsWith("/index-v1.m3u8")),
        )
        .toBe(true);
      await expect(
        frigateApp.page.getByRole("status").filter({
          hasText: "Playing the original video without sound",
        }),
      ).toBeVisible();
      playback.releasePlaylists();
      await expect
        .poll(() =>
          playback.fragments.some(
            (p) => p.includes("/vod/") && p.endsWith("/seg-1.m4s"),
          ),
        )
        .toBe(true);

      expect(playback.creations).toHaveLength(0);
    } finally {
      playback.release();
    }
  });

  test("supported HEVC remains original when only its audio fails", async ({
    frigateApp,
  }) => {
    await frigateApp.page.addInitScript(() => {
      const supports = MediaSource.isTypeSupported.bind(MediaSource);
      MediaSource.isTypeSupported = (mime) =>
        /hvc1|hev1/i.test(mime) || supports(mime);
    });
    const playback = await installPlayback(frigateApp, {
      videoCodec: "hevc",
      hevcSupported: true,
    });
    try {
      await failDecoder(playback.video, true, true);
      await expect
        .poll(() =>
          playback.playlists.some((path) => path.endsWith("/index-v1.m3u8")),
        )
        .toBe(true);
      expect(playback.creations).toHaveLength(0);
      await expect(
        frigateApp.page.getByRole("status").filter({
          hasText: "Playing the original video without sound",
        }),
      ).toBeVisible();
    } finally {
      playback.release();
    }
  });

  test("a video codec failure uses compatible audio/video rather than dropping audio", async ({
    frigateApp,
  }) => {
    const playback = await installPlayback(frigateApp);
    try {
      await failDecoder(playback.video, false);
      await expect.poll(() => playback.creations.length).toBe(1);
      await expect
        .poll(() =>
          playback.playlists.some((path) => path.includes("/compat-1/media/")),
        )
        .toBe(true);
      expect(
        playback.playlists.filter((p) => p.endsWith("/index-v1.m3u8")),
      ).toHaveLength(0);
    } finally {
      playback.release();
    }
  });

  test("known unsupported HEVC starts compatible playback without waiting for decoder retries", async ({
    frigateApp,
  }) => {
    const playback = await installPlayback(frigateApp, { videoCodec: "hevc" });
    try {
      await expect.poll(() => playback.creations.length).toBe(1);
      await expect(
        frigateApp.page
          .getByRole("status")
          .filter({ hasText: "Compatible playback: H.264/AAC" }),
      ).toBeVisible();
      expect(playback.creations[0].start_timestamp).toBe(playback.timestamp);
    } finally {
      playback.release();
    }
  });

  test("a compatibility window continues at its boundary instead of skipping the hour", async ({
    frigateApp,
  }) => {
    const playback = await installPlayback(frigateApp, { videoCodec: "hevc" });
    try {
      await expect
        .poll(() =>
          playback.playlists.some((path) => path.includes("/compat-1/media/")),
        )
        .toBe(true);
      await playback.video.evaluate((element) =>
        element.dispatchEvent(new Event("ended")),
      );
      await expect.poll(() => playback.creations.length).toBe(2);
      expect(playback.creations[1].after).toBe(playback.creations[0].before);
      expect(playback.creations[1].after - playback.creations[0].after).toBe(
        30,
      );
      expect(playback.creations[1].generation).toBeGreaterThan(
        playback.creations[0].generation,
      );
    } finally {
      playback.release();
    }
  });

  test("a stalled preparation has a finite client startup deadline", async ({
    frigateApp,
  }) => {
    await frigateApp.page.clock.install();
    const playback = await installPlayback(frigateApp, { preparing: true });
    try {
      await failDecoder(playback.video, false);
      await expect.poll(() => playback.creations.length).toBe(1);
      await frigateApp.page.clock.fastForward(31000);
      await expect(
        frigateApp.page.getByRole("alert").filter({
          hasText: "Recording playback failed.",
        }),
      ).toBeVisible();
      await expect(
        frigateApp.page.locator('[aria-label="Loading…"].absolute'),
      ).toHaveCount(0);
      await expect
        .poll(() => playback.cancelled.includes("compat-1"))
        .toBe(true);
      expect(playback.creations).toHaveLength(1);
    } finally {
      playback.release();
    }
  });

  test("a failed compatibility decoder ends loading and retries only on explicit request", async ({
    frigateApp,
  }) => {
    const playback = await installPlayback(frigateApp);
    try {
      await failDecoder(playback.video, false);
      await expect
        .poll(() =>
          playback.playlists.some((path) => path.includes("/compat-1/media/")),
        )
        .toBe(true);
      await failDecoder(playback.video, false);
      await expect(
        frigateApp.page
          .getByRole("alert")
          .filter({ hasText: "Recording playback failed." }),
      ).toBeVisible();
      expect(playback.creations).toHaveLength(1);
      await frigateApp.page
        .getByRole("button", { name: "Retry playback", exact: true })
        .click();
      await expect.poll(() => playback.creations.length).toBe(2);
      expect(playback.creations[1].generation).toBeGreaterThan(
        playback.creations[0].generation,
      );
      expect(playback.creations[1].client_id).toBe(
        playback.creations[0].client_id,
      );
      await expect
        .poll(() =>
          playback.playlists.some((path) => path.includes("/compat-2/media/")),
        )
        .toBe(true);
      await expect
        .poll(() => playback.cancelled.includes("compat-1"))
        .toBe(true);
    } finally {
      playback.release();
    }
  });

  test("an invalid manifest ends loading instead of leaving an endless spinner", async ({
    frigateApp,
  }) => {
    const playback = await installPlayback(frigateApp, {
      malformedManifest: true,
    });
    try {
      await expect(
        frigateApp.page.getByRole("alert").filter({
          hasText: "Recording playback failed.",
        }),
      ).toBeVisible();
      await expect(
        frigateApp.page.locator('[aria-label="Loading…"].absolute'),
      ).toHaveCount(0);
      expect(playback.playlists).toHaveLength(1);
    } finally {
      playback.release();
    }
  });
});
