import { readFileSync } from "node:fs";
import { test, expect, type FrigateApp } from "../fixtures/frigate-test";
import type { RecordedHTTPSession } from "../../src/types/recordedHttp";

// Generated 20-second testsrc2 video, 320x180 H264 at 10 FPS, faststart MP4.
const MP4 = readFileSync(
  new URL("../fixtures/mock-data/https-review.mp4", import.meta.url),
);

async function installHTTP(
  app: FrigateApp,
  unavailable = false,
  preparePolls = 0,
  maxViewers = Infinity,
  options: {
    resolveTarget?: (target: number) => number | null;
    timelineTail?: number;
    searchLimitOnce?: boolean;
    nearHourEnd?: boolean;
    directFile?: boolean;
  } = {},
) {
  if (options.resolveTarget || options.searchLimitOnce) {
    await app.page.addInitScript(() => {
      Object.defineProperty(navigator, "connection", {
        configurable: true,
        value: { saveData: true },
      });
    });
  }
  await app.installDefaults({
    config: {
      ui: { recorded_http: true },
      live_webrtc: { enabled: true, grid_height: 360, max_height: 1080 },
      cameras: {
        front_door: {
          capture_mode: "recording_only",
          record: { enabled: true },
        },
        backyard: { enabled: false, ui: { review: false } },
        garage: { enabled: false, ui: { review: false } },
      },
    },
  });
  const timestamp =
    Math.floor(Date.now() / 3600000) * 3600 - (options.nearHourEnd ? 5 : 3480);
  const calls: {
    method: string;
    path: string;
    body: Record<string, unknown>;
  }[] = [];
  const forbidden: string[] = [];
  const ranges: string[] = [];
  const sessions = new Map<string, RecordedHTTPSession>();
  const activeSessions = new Set<string>();
  const polls = new Map<string, number>();
  const timing = { firstSessionRequest: 0 };
  let resolveCount = 0;
  let createBarrier: Promise<void> | undefined;
  let deleteBarrier: Promise<void> | undefined;
  const holdNextDelete = () => {
    let resume = () => {};
    deleteBarrier = new Promise<void>((resolve) => {
      resume = resolve;
    });
    return resume;
  };
  const holdNextCreate = () => {
    let resume = () => {};
    createBarrier = new Promise<void>((resolve) => {
      resume = resolve;
    });
    return resume;
  };
  await app.page.route("**/*.m3u8*", (route) => {
    forbidden.push(route.request().url());
    return route.fulfill({ status: 503 });
  });
  await app.page.route("**/api/*/recordings/webrtc/**", (route) => {
    forbidden.push(route.request().url());
    return route.fulfill({ status: 503 });
  });
  await app.page.route("**/api/review/activity/motion?**", (route) =>
    route.fulfill({ json: [] }),
  );
  await app.page.route("**/api/recordings/unavailable?**", (route) =>
    route.fulfill({ json: [] }),
  );
  await app.page.route("**/api/*/recordings/coverage?**", (route) => {
    const url = new URL(route.request().url());
    return route.fulfill({
      json: {
        spans: [
          {
            start_time: Number(url.searchParams.get("after")),
            end_time: Number(url.searchParams.get("before")),
            streams: ["main"],
          },
        ],
        streams: { main: { video_codec: "h264" } },
        codecs_compatible: true,
      },
    });
  });
  await app.page.route(
    "**/api/front_door/recordings/http/**",
    async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname.split(
        "/recordings/http/",
      )[1];
      const method = request.method();
      const body = request.postData() ? request.postDataJSON() : {};
      calls.push({ method, path, body });
      if (path === "status") {
        return route.fulfill({
          json: {
            version: 1,
            enabled: true,
            available: !unavailable,
            transport: "https_mp4",
            seek: "http_byte_range",
            max_window_seconds: 120,
            session_url: "front_door/recordings/http/sessions",
            resolve_url: "front_door/recordings/http/resolve",
            qualities: ["auto", "720", "1080"],
          },
        });
      }
      if (path === "resolve" && method === "POST") {
        const target = Number(body.start_timestamp);
        resolveCount += 1;
        if (options.searchLimitOnce && resolveCount === 1)
          return route.fulfill({
            json: {
              version: 1,
              effective_start_timestamp: null,
              exhausted: false,
              searched_before: target + 1,
              reason: "search_limit",
            },
          });
        const effective = options.resolveTarget
          ? options.resolveTarget(target)
          : target;
        return route.fulfill({
          json: {
            version: 1,
            effective_start_timestamp: effective,
            exhausted: effective === null,
            searched_before: Number(body.before),
            reason: effective === null ? "no_frame_in_range" : "frame_found",
          },
        });
      }
      if (path === "sessions" && method === "POST") {
        if (timing.firstSessionRequest === 0)
          timing.firstSessionRequest = Date.now();
        if (activeSessions.size >= maxViewers)
          return route.fulfill({
            status: 503,
            json: { detail: "fixture_viewer_capacity" },
          });
        const id = String(sessions.size + 1);
        const base = `front_door/recordings/http/sessions/${id}`;
        const after = Number(body.after);
        const before = Number(body.before);
        const playableBefore = options.directFile
          ? Math.min(before, after + 10)
          : before;
        const session: RecordedHTTPSession = {
          version: 1,
          session_id: id,
          camera: "front_door",
          state: preparePolls ? "preparing" : "ready",
          after,
          before,
          start_timestamp: Number(body.start_timestamp),
          effective_start_timestamp: Number(body.start_timestamp),
          next_timestamp: playableBefore,
          delivery: options.directFile ? "original_file" : "prepared",
          quality: body.quality,
          audio: false,
          status_url: base,
          close_url: base,
          media_url: base + "/media.mp4",
          hard_expires_at: Date.now() / 1000 + 300,
          timeline: [
            {
              source_start: after,
              source_end: playableBefore - (options.timelineTail ?? 0),
              media_start: 0,
              media_end: playableBefore - after,
            },
          ],
          error: null,
          width: 320,
          height: 180,
        };
        sessions.set(id, session);
        activeSessions.add(id);
        const barrier = createBarrier;
        createBarrier = undefined;
        if (barrier) await barrier;
        return route.fulfill({ status: 201, json: session });
      }
      if (method === "DELETE") {
        const barrier = deleteBarrier;
        deleteBarrier = undefined;
        if (barrier) await barrier;
        activeSessions.delete(path.split("/")[1]);
        return route.fulfill({ status: 204 });
      }
      if (path.endsWith("/media.mp4")) {
        const range = request.headers().range;
        const match = range?.match(/^bytes=(\d+)-(\d*)$/);
        const start = match ? Number(match[1]) : 0;
        const end = match?.[2]
          ? Math.min(Number(match[2]), MP4.length - 1)
          : MP4.length - 1;
        if (range) ranges.push(range);
        return route.fulfill({
          status: range ? 206 : 200,
          contentType: "video/mp4",
          headers: {
            "Accept-Ranges": "bytes",
            "Content-Length": String(end - start + 1),
            ...(range
              ? { "Content-Range": `bytes ${start}-${end}/${MP4.length}` }
              : {}),
          },
          body: MP4.subarray(start, end + 1),
        });
      }
      const current = sessions.get(path.split("/")[1]);
      if (current && method === "GET") {
        const count = (polls.get(current.session_id) ?? 0) + 1;
        polls.set(current.session_id, count);
        if (count >= preparePolls) current.state = "ready";
        return route.fulfill({ json: current });
      }
      return route.fulfill({ status: 404 });
    },
  );
  await app.goto(`/review?timestamp=front_door_${timestamp}`);
  return {
    timestamp,
    calls,
    forbidden,
    ranges,
    sessions,
    holdNextCreate,
    holdNextDelete,
    timing,
  };
}

test.describe("Recorded HTTPS MP4 @mobile", () => {
  test("decodes MP4, seeks in the same window and replaces quality while paused", async ({
    frigateApp,
  }) => {
    const state = await installHTTP(frigateApp);
    const video = frigateApp.page.locator(
      "[data-recorded-transport=https_mp4] video",
    );
    await expect
      .poll(() =>
        video.evaluate((element: HTMLVideoElement) => element.videoHeight),
      )
      .toBe(180);
    await expect
      .poll(() =>
        video.evaluate(
          (element: HTMLVideoElement) =>
            element.getVideoPlaybackQuality().totalVideoFrames,
        ),
      )
      .toBeGreaterThan(0);
    expect(state.timing.firstSessionRequest).toBeGreaterThan(0);
    expect(Date.now() - state.timing.firstSessionRequest).toBeLessThan(10000);
    await frigateApp.page
      .getByRole("button", { name: "Pause recording", exact: true })
      .click();
    await expect
      .poll(() => video.evaluate((element: HTMLVideoElement) => element.paused))
      .toBe(true);
    const before = await video.evaluate(
      (element: HTMLVideoElement) => element.currentTime,
    );
    const source = await video.getAttribute("src");
    const seekStarted = Date.now();
    const decodedBefore = await video.evaluate(
      (element: HTMLVideoElement) =>
        element.getVideoPlaybackQuality().totalVideoFrames,
    );
    await frigateApp.page
      .getByRole("button", { name: "Forward 10 seconds", exact: true })
      .click();
    await expect
      .poll(() =>
        video.evaluate((element: HTMLVideoElement) => element.currentTime),
      )
      .toBeGreaterThan(before + 9);
    await expect
      .poll(() =>
        video.evaluate(
          (element: HTMLVideoElement, decodedBefore: number) =>
            !element.seeking &&
            element.getVideoPlaybackQuality().totalVideoFrames > decodedBefore,
          decodedBefore,
        ),
      )
      .toBe(true);
    expect(Date.now() - seekStarted).toBeLessThan(2000);
    expect(await video.getAttribute("src")).toBe(source);
    expect(state.calls.filter((call) => call.path === "sessions")).toHaveLength(
      1,
    );
    await frigateApp.page
      .getByRole("combobox", { name: "Recording viewing quality" })
      .click();
    await frigateApp.page
      .getByRole("option", { name: "720p", exact: true })
      .click();
    await expect
      .poll(() => state.calls.filter((call) => call.path === "sessions").length)
      .toBe(2);
    await expect
      .poll(() =>
        video.evaluate((element: HTMLVideoElement) => element.readyState),
      )
      .toBeGreaterThanOrEqual(2);
    expect(
      await video.evaluate((element: HTMLVideoElement) => element.paused),
    ).toBe(true);
    expect(
      state.calls.filter((call) => call.path === "sessions")[1].body.quality,
    ).toBe("720");
    expect(state.ranges.length).toBeGreaterThan(0);
    expect(state.forbidden).toEqual([]);
    await frigateApp.page
      .getByRole("button", { name: "Go back", exact: true })
      .click();
    await expect
      .poll(() => state.calls.filter((call) => call.method === "DELETE").length)
      .toBe(2);
  });

  test("unavailable HTTPS is explicit and never falls back", async ({
    frigateApp,
  }) => {
    const state = await installHTTP(frigateApp, true);
    await expect(frigateApp.page.getByRole("alert")).toContainText(
      "Recorded video could not be prepared or played",
    );
    expect(state.calls.filter((call) => call.path === "sessions")).toHaveLength(
      0,
    );
    expect(state.forbidden).toEqual([]);
  });

  test("hidden viewer releases its session and visible paused viewer stays paused", async ({
    frigateApp,
  }) => {
    const state = await installHTTP(frigateApp);
    const video = frigateApp.page.locator(
      "[data-recorded-transport=https_mp4] video",
    );
    await expect
      .poll(() =>
        video.evaluate((element: HTMLVideoElement) => element.videoHeight),
      )
      .toBe(180);
    await frigateApp.page
      .getByRole("button", { name: "Pause recording", exact: true })
      .click();
    const visibility = (value: string) =>
      frigateApp.page.evaluate((value) => {
        Object.defineProperty(document, "visibilityState", {
          configurable: true,
          value,
        });
        document.dispatchEvent(new Event("visibilitychange"));
      }, value);
    await visibility("hidden");
    await expect
      .poll(() => state.calls.filter((call) => call.method === "DELETE").length)
      .toBe(1);
    await visibility("visible");
    await expect
      .poll(() => state.calls.filter((call) => call.path === "sessions").length)
      .toBe(2);
    await expect
      .poll(() =>
        video.evaluate((element: HTMLVideoElement) => element.readyState),
      )
      .toBeGreaterThanOrEqual(2);
    expect(
      await video.evaluate((element: HTMLVideoElement) => element.paused),
    ).toBe(true);
    expect(state.forbidden).toEqual([]);
  });

  test("late quality creation after navigation releases both owned sessions", async ({
    frigateApp,
  }) => {
    const state = await installHTTP(frigateApp);
    const video = frigateApp.page.locator(
      "[data-recorded-transport=https_mp4] video",
    );
    await expect
      .poll(() =>
        video.evaluate((element: HTMLVideoElement) => element.videoHeight),
      )
      .toBe(180);
    const resume = state.holdNextCreate();
    await frigateApp.page
      .getByRole("combobox", { name: "Recording viewing quality" })
      .click();
    await frigateApp.page
      .getByRole("option", { name: "720p", exact: true })
      .click();
    await expect.poll(() => state.sessions.size).toBe(2);
    await frigateApp.page
      .getByRole("button", { name: "Go back", exact: true })
      .click();
    resume();
    await expect
      .poll(() => state.calls.filter((call) => call.method === "DELETE").length)
      .toBe(2);
    expect(state.forbidden).toEqual([]);
  });

  test("Data Saver caps Auto and hides higher quality choices", async ({
    frigateApp,
  }) => {
    await frigateApp.page.addInitScript(() => {
      Object.defineProperty(navigator, "connection", {
        configurable: true,
        value: { saveData: true },
      });
    });
    const state = await installHTTP(frigateApp);
    await expect.poll(() => state.sessions.size).toBe(1);
    expect(
      state.calls.find((call) => call.path === "sessions")?.body.quality,
    ).toBe("720");
    await frigateApp.page
      .getByRole("combobox", { name: "Recording viewing quality" })
      .click();
    await expect(
      frigateApp.page.getByRole("option", { name: "1080p", exact: true }),
    ).toHaveCount(0);
    expect(state.forbidden).toEqual([]);
  });

  test("polls preparation before opening immutable MP4", async ({
    frigateApp,
  }) => {
    const state = await installHTTP(frigateApp, false, 3);
    const video = frigateApp.page.locator(
      "[data-recorded-transport=https_mp4] video",
    );
    await expect
      .poll(() =>
        video.evaluate((element: HTMLVideoElement) => element.videoHeight),
      )
      .toBe(180);
    const mediaIndex = state.calls.findIndex((call) =>
      call.path.endsWith("/media.mp4"),
    );
    expect(mediaIndex).toBeGreaterThan(0);
    expect(
      state.calls
        .slice(0, mediaIndex)
        .filter(
          (call) => call.method === "GET" && call.path.startsWith("sessions/"),
        ),
    ).toHaveLength(3);
    expect(state.forbidden).toEqual([]);
  });

  test("replacement waits for joined cleanup at the shared viewer limit", async ({
    frigateApp,
  }) => {
    const state = await installHTTP(frigateApp, false, 0, 1);
    const video = frigateApp.page.locator(
      "[data-recorded-transport=https_mp4] video",
    );
    await expect
      .poll(() =>
        video.evaluate((element: HTMLVideoElement) => element.videoHeight),
      )
      .toBe(180);
    const resume = state.holdNextDelete();
    await frigateApp.page
      .getByRole("combobox", { name: "Recording viewing quality" })
      .click();
    await frigateApp.page
      .getByRole("option", { name: "720p", exact: true })
      .click();
    await expect
      .poll(() => state.calls.filter((call) => call.method === "DELETE").length)
      .toBe(1);
    expect(state.sessions.size).toBe(1);
    resume();
    await expect.poll(() => state.sessions.size).toBe(2);
    await expect
      .poll(() =>
        video.evaluate((element: HTMLVideoElement) => element.readyState),
      )
      .toBeGreaterThanOrEqual(2);
    expect(state.forbidden).toEqual([]);
  });

  test("visible playback prepares one continuation and decodes it within two seconds of the boundary", async ({
    frigateApp,
  }) => {
    const state = await installHTTP(frigateApp);
    const video = frigateApp.page.locator(
      "[data-recorded-transport=https_mp4] video",
    );
    await expect
      .poll(() =>
        video.evaluate((element: HTMLVideoElement) => element.videoHeight),
      )
      .toBe(180);
    const oldSource = await video.getAttribute("src");
    await video.evaluate((element: HTMLVideoElement) => {
      element.currentTime = 16;
    });
    await expect.poll(() => state.sessions.size).toBe(2);
    expect(
      state.calls.filter((call) => call.path.endsWith("/media.mp4")),
    ).toHaveLength(1);
    await video.evaluate((element: HTMLVideoElement) => {
      const source = element.currentSrc;
      element.addEventListener(
        "ended",
        () => {
          element.dataset.continuationEnded = String(performance.now());
        },
        { once: true },
      );
      const sample = () =>
        element.requestVideoFrameCallback(() => {
          if (element.currentSrc !== source)
            element.dataset.continuationDecoded = String(performance.now());
          else {
            element.dataset.continuationEnded = String(performance.now());
            sample();
          }
        });
      sample();
      element.currentTime = 19.5;
    });
    await expect.poll(() => video.getAttribute("src")).not.toBe(oldSource);
    await expect
      .poll(() =>
        video.evaluate((element: HTMLVideoElement) =>
          Boolean(element.dataset.continuationDecoded),
        ),
      )
      .toBe(true);
    const delay = await video.evaluate(
      (element: HTMLVideoElement) =>
        Number(element.dataset.continuationDecoded) -
        Number(element.dataset.continuationEnded),
    );
    expect(delay).toBeGreaterThanOrEqual(0);
    expect(delay).toBeLessThan(2000);
    expect(state.sessions.size).toBe(2);
    expect(state.forbidden).toEqual([]);
  });

  test("pausing cancels an upcoming preparation without discarding the current MP4", async ({
    frigateApp,
  }) => {
    const state = await installHTTP(frigateApp, false, 3);
    const video = frigateApp.page.locator(
      "[data-recorded-transport=https_mp4] video",
    );
    await expect
      .poll(() =>
        video.evaluate((element: HTMLVideoElement) => element.videoHeight),
      )
      .toBe(180);
    const source = await video.getAttribute("src");
    await video.evaluate((element: HTMLVideoElement) => {
      element.currentTime = 16;
    });
    await expect.poll(() => state.sessions.size).toBe(2);
    await frigateApp.page
      .getByRole("button", { name: "Pause recording", exact: true })
      .click();
    await expect
      .poll(
        () =>
          state.calls.filter(
            (call) => call.method === "DELETE" && call.path === "sessions/2",
          ).length,
      )
      .toBe(1);
    expect(await video.getAttribute("src")).toBe(source);
    expect(
      await video.evaluate((element: HTMLVideoElement) => element.paused),
    ).toBe(true);
    expect(state.forbidden).toEqual([]);
  });

  test("resolves a requested gap to an actual next frame before creating the MP4", async ({
    frigateApp,
  }) => {
    const state = await installHTTP(frigateApp, false, 0, Infinity, {
      resolveTarget: (target) => target + 2.125,
    });
    const video = frigateApp.page.locator(
      "[data-recorded-transport=https_mp4] video",
    );
    await expect
      .poll(() =>
        video.evaluate((element: HTMLVideoElement) => element.videoHeight),
      )
      .toBe(180);
    const resolution = state.calls.find((call) => call.path === "resolve");
    const creation = state.calls.find((call) => call.path === "sessions");
    expect(creation?.body.start_timestamp).toBe(
      Number(resolution?.body.start_timestamp) + 2.125,
    );
    expect(creation?.body.start_policy).toBe("next");
    expect(state.forbidden).toEqual([]);
  });

  test("fractional media tails use the canonical requested boundary for continuation", async ({
    frigateApp,
  }) => {
    const state = await installHTTP(frigateApp, false, 0, Infinity, {
      timelineTail: 0.000763,
    });
    const video = frigateApp.page.locator(
      "[data-recorded-transport=https_mp4] video",
    );
    await expect
      .poll(() =>
        video.evaluate((element: HTMLVideoElement) => element.videoHeight),
      )
      .toBe(180);
    await video.evaluate((element: HTMLVideoElement) => {
      element.currentTime = 16;
    });
    await expect.poll(() => state.sessions.size).toBe(2);
    const creates = state.calls.filter((call) => call.path === "sessions");
    expect(creates[1].body.start_timestamp).toBe(creates[0].body.before);
    expect(state.forbidden).toEqual([]);
  });

  test("exhausted resolution shows missing footage and never creates an empty viewer", async ({
    frigateApp,
  }) => {
    const state = await installHTTP(frigateApp, false, 0, Infinity, {
      resolveTarget: () => null,
    });
    await expect(frigateApp.page.getByRole("alert")).toContainText(
      "No playable recorded video remains",
    );
    expect(state.sessions.size).toBe(0);
    expect(state.forbidden).toEqual([]);
  });

  test("a bounded partial search resumes from searched_before rather than claiming missing footage", async ({
    frigateApp,
  }) => {
    const state = await installHTTP(frigateApp, false, 0, Infinity, {
      searchLimitOnce: true,
    });
    const video = frigateApp.page.locator(
      "[data-recorded-transport=https_mp4] video",
    );
    await expect
      .poll(() =>
        video.evaluate((element: HTMLVideoElement) => element.videoHeight),
      )
      .toBe(180);
    const resolutions = state.calls.filter((call) => call.path === "resolve");
    expect(resolutions).toHaveLength(2);
    expect(resolutions[1].body.after).toBe(
      Number(resolutions[0].body.start_timestamp) + 1,
    );
    expect(state.sessions.size).toBe(1);
    expect(state.forbidden).toEqual([]);
  });

  test("an hour cutoff advances the view before resolving the next recording window", async ({
    frigateApp,
  }) => {
    const state = await installHTTP(frigateApp, false, 0, Infinity, {
      nearHourEnd: true,
      timelineTail: 0.000763,
    });
    const video = frigateApp.page.locator(
      "[data-recorded-transport=https_mp4] video",
    );
    await expect
      .poll(() =>
        video.evaluate((element: HTMLVideoElement) => element.videoHeight),
      )
      .toBe(180);
    await video.evaluate((element: HTMLVideoElement) => {
      element.currentTime = element.duration - 0.05;
    });
    await expect.poll(() => state.sessions.size).toBeGreaterThanOrEqual(2);
    const creates = state.calls.filter((call) => call.path === "sessions");
    expect(creates[1].body.after).toBeGreaterThanOrEqual(state.timestamp + 5);
    for (const resolution of state.calls.filter(
      (call) => call.path === "resolve",
    )) {
      expect(Number(resolution.body.start_timestamp)).toBeLessThan(
        Number(resolution.body.before),
      );
      expect(Number(resolution.body.start_timestamp)).toBeGreaterThanOrEqual(
        Number(resolution.body.after),
      );
    }
    expect(state.forbidden).toEqual([]);
  });

  test("normal Auto opens original media without resolver round trips or scaled preparation", async ({
    frigateApp,
  }) => {
    const state = await installHTTP(frigateApp, false, 0, Infinity, {
      directFile: true,
    });
    const video = frigateApp.page.locator(
      "[data-recorded-transport=https_mp4] video",
    );
    await expect
      .poll(() =>
        video.evaluate((element: HTMLVideoElement) => element.videoHeight),
      )
      .toBe(180);
    expect(state.calls.filter((call) => call.path === "resolve")).toHaveLength(
      0,
    );
    const creation = state.calls.find((call) => call.path === "sessions");
    expect(creation?.body).toMatchObject({ quality: "auto", delivery: "auto" });
    expect(creation?.body.video_codecs).toContain("h264");
    await video.evaluate((element: HTMLVideoElement) => {
      element.currentTime = 9.9;
    });
    await expect.poll(() => state.sessions.size).toBeGreaterThanOrEqual(2);
    expect(
      state.calls.filter((call) => call.path === "sessions")[1].body
        .start_timestamp,
    ).toBe(Number(creation?.body.after) + 10);
    expect(state.forbidden).toEqual([]);
  });
});
