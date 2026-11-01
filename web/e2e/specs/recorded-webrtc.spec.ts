import { test, expect, type FrigateApp } from "../fixtures/frigate-test";
import { h264Offer, closeH264Peer } from "../fixtures/webrtc-peer";
import { PLACEHOLDER_PNG } from "../helpers/api-mocker";
import baseEvents from "../fixtures/mock-data/events.json" with { type: "json" };
import { ONVIF_VIDEO_ONLY_MP4 } from "../fixtures/mock-data/onvif-media";
import type {
  RecordedWebRTCSession,
  RecordedWebRTCWindow,
} from "../../src/types/recordedWebrtc";

type Payload = Partial<RecordedWebRTCWindow> & {
  generation?: number;
  action?: string;
  decoded_ready?: boolean;
  window?: RecordedWebRTCWindow;
  sdp?: string;
  candidates?: RTCIceCandidateInit[];
  max_height?: number;
  height?: number | null;
};

async function installRecorded(
  app: FrigateApp,
  unavailable = false,
  secondary = false,
  previewFile = false,
) {
  await app.installDefaults({
    config: {
      live_webrtc: { enabled: true, grid_height: 360, max_height: 1080 },
      cameras: {
        front_door: {
          capture_mode: "recording_only",
          record: { enabled: true },
        },
        backyard: {
          enabled: secondary,
          ui: { review: secondary },
          capture_mode: "recording_only",
          record: { enabled: true },
        },
        garage: { enabled: false, ui: { review: false } },
      },
    },
  });
  const timestamp = Math.floor(Date.now() / 3600000) * 3600 - 3480;
  await app.page.route("**/api/review/activity/motion?**", (route) =>
    route.fulfill({ json: [] }),
  );
  await app.page.route("**/api/recordings/unavailable?**", (route) =>
    route.fulfill({ json: [] }),
  );
  const calls: { method: string; path: string; body: Payload }[] = [];
  const sessions = new Map<string, RecordedWebRTCSession>();
  const forbidden: string[] = [];
  const snapshots: string[] = [];
  let seekBarrier: Promise<void> | undefined;
  const holdNextSeek = () => {
    let release = () => {};
    seekBarrier = new Promise<void>((resolve) => {
      release = resolve;
    });
    return release;
  };
  await app.page.route("**/api/*/recordings/*/snapshot.jpg*", (route) => {
    snapshots.push(route.request().url());
    return route.fulfill({ contentType: "image/png", body: PLACEHOLDER_PNG });
  });
  let sequence = 0;
  if (previewFile) {
    await app.page.route("**/api/preview/*/start/*/end/*", (route) =>
      route.fulfill({
        json: [
          {
            camera: "backyard",
            start: timestamp,
            end: timestamp + 60,
            src: "/fixture-recorded-preview.mp4",
            type: "video/mp4",
          },
        ],
      }),
    );
    await app.page.route("**/fixture-recorded-preview.mp4", (route) =>
      route.fulfill({ contentType: "video/mp4", body: ONVIF_VIDEO_ONLY_MP4 }),
    );
  }
  await app.page.route("**/*.m3u8*", (route) => {
    forbidden.push(route.request().url());
    return route.fulfill({ status: 503, json: { detail: "unexpected_hls" } });
  });
  await app.page.route("**/api/*/recordings/coverage?**", (route) => {
    const url = new URL(route.request().url());
    const after = Number(url.searchParams.get("after"));
    const before = Number(url.searchParams.get("before"));
    return route.fulfill({
      json: {
        spans: [{ start_time: after, end_time: before, streams: ["main"] }],
        streams: { main: { video_codec: "hevc" } },
        codecs_compatible: false,
      },
    });
  });
  await app.page.route(
    "**/api/front_door/recordings/webrtc/**",
    async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname.split(
        "/recordings/webrtc/",
      )[1];
      const method = request.method();
      const body = (
        request.postData() ? request.postDataJSON() : {}
      ) as Payload;
      calls.push({ method, path, body });
      expect(request.headers()["x-csrf-token"]).toBe("1");
      if (path === "sessions" && method === "POST") {
        if (unavailable) {
          await route.fulfill({
            status: 503,
            json: { detail: "synthetic_unavailable" },
          });
          return;
        }
        expect(body.start_timestamp).toBeGreaterThanOrEqual(timestamp);
        expect(Number(body.before) - Number(body.after)).toBeLessThanOrEqual(
          30,
        );
        const id = `00000000-0000-4000-8000-${String(++sequence).padStart(12, "0")}`;
        const base = `front_door/recordings/webrtc/sessions/${id}`;
        const window: RecordedWebRTCWindow = {
          after: Number(body.after),
          before: Number(body.before),
          start_timestamp: Number(body.start_timestamp),
          stream: body.stream ?? "auto",
        };
        const offer = await h264Offer(app.page, id + "_1", 720);
        const session: RecordedWebRTCSession = {
          version: 1,
          protocol: "webrtc",
          session_id: id,
          generation: 1,
          state: "warming",
          warmup_expires_at: Date.now() / 1000 + 20,
          expires_at: Date.now() / 1000 + 30,
          heartbeat_seconds: 10,
          answer_url: base + "/answer",
          candidates_url: base + "/candidates",
          heartbeat_url: base,
          close_url: base,
          quality_url: base + "/quality",
          control_url: base + "/control",
          ice_servers: [],
          ice_transport_policy: "all",
          renditions: [720, 1080]
            .filter((height) => height <= (body.max_height ?? 1080))
            .map((height) => ({
              height,
              bitrate: height === 720 ? 1200 : 2500,
            })),
          initial_height: null,
          window,
          timeline: [
            {
              source_start: window.after,
              source_end: window.before,
              media_start: 0,
              media_end: window.before - window.after,
            },
          ],
          position_timestamp: window.start_timestamp,
          position_kind: "input_read_cursor",
          gap_policy: "skip",
          audio: false,
          audio_reason: "source_has_no_audio",
          renegotiate: true,
          offer,
          ice_candidates: [],
        };
        sessions.set(id, session);
        await route.fulfill({ status: 201, json: session });
        return;
      }
      const id = path.split("/")[1];
      const current = sessions.get(id);
      if (!current)
        throw new Error("Synthetic recorded session was not created");
      const peerKey = id + "_" + current.generation;
      if (path.endsWith("/answer")) {
        expect(body.generation).toBe(current.generation);
        await app.page.evaluate(
          async ({ key, sdp }) => {
            const peer = window.liveTestPeers.get(key)?.peer;
            if (!peer) throw new Error("Missing synthetic recorded peer");
            await peer.setRemoteDescription({ type: "answer", sdp });
          },
          { key: peerKey, sdp: body.sdp },
        );
      } else if (path.endsWith("/candidates")) {
        expect(body.generation).toBe(current.generation);
        await app.page.evaluate(
          async ({ key, candidates }) => {
            const peer = window.liveTestPeers.get(key)?.peer;
            if (!peer) throw new Error("Missing synthetic recorded peer");
            for (const candidate of candidates)
              await peer.addIceCandidate(candidate);
          },
          { key: peerKey, candidates: body.candidates ?? [] },
        );
      } else if (path.endsWith("/control")) {
        expect(body.generation).toBe(current.generation);
        if (body.action === "seek" && seekBarrier) {
          const barrier = seekBarrier;
          seekBarrier = undefined;
          await barrier;
        }
        if (body.action === "activate") {
          expect(body.decoded_ready).toBe(true);
          const frames = await app.page
            .locator("[data-recorded-transport=webrtc] video")
            .evaluate(
              (video: HTMLVideoElement) =>
                video.getVideoPlaybackQuality().totalVideoFrames,
            );
          expect(frames).toBeGreaterThan(0);
          current.state = "playing";
          current.warmup_expires_at = null;
          current.renegotiate = false;
          current.offer = null;
        } else {
          await closeH264Peer(app.page, peerKey);
          current.generation += 1;
          if (body.action === "pause") {
            current.state = "paused";
            current.renegotiate = false;
            current.offer = null;
          } else {
            if (body.window) current.window = body.window;
            current.state = "warming";
            current.renegotiate = true;
            current.offer = await h264Offer(
              app.page,
              id + "_" + current.generation,
              720,
            );
          }
        }
        await route.fulfill({ json: current });
        return;
      } else if (path.endsWith("/quality")) {
        expect(body.generation).toBe(current.generation);
        await app.page.evaluate(
          ({ key, height }) => {
            const source = window.liveTestPeers.get(key);
            if (!source) throw new Error("Missing synthetic recorded peer");
            source.canvas.width = Math.round((height * 16) / 9);
            source.canvas.height = height;
          },
          { key: peerKey, height: body.height ?? 720 },
        );
      } else if (method === "PATCH") {
        current.position_timestamp =
          (current.position_timestamp ?? current.window.start_timestamp) + 0.1;
        current.renegotiate = false;
        await route.fulfill({ json: current });
        return;
      } else if (method === "DELETE") {
        await closeH264Peer(app.page, peerKey);
      }
      await route.fulfill({ status: 204 });
    },
  );
  await app.goto(`/review?timestamp=front_door_${timestamp}`);
  return { calls, sessions, forbidden, timestamp, snapshots, holdNextSeek };
}

test.describe("Recorded adaptive WebRTC @mobile", () => {
  test("decodes before activation, pauses and resumes with a new generation and no HLS", async ({
    frigateApp,
  }) => {
    test.setTimeout(60000);
    const state = await installRecorded(frigateApp);
    const video = frigateApp.page.locator(
      "[data-recorded-transport=webrtc] video",
    );
    await expect
      .poll(
        () =>
          video.evaluate((element: HTMLVideoElement) => element.videoHeight),
        { timeout: 20000 },
      )
      .toBe(720);
    await expect
      .poll(
        () =>
          state.calls.filter((call) => call.body.action === "activate").length,
      )
      .toBe(1);
    expect(state.forbidden).toEqual([]);
    await frigateApp.page
      .getByRole("button", { name: "Pause recording", exact: true })
      .click();
    await expect
      .poll(() => state.calls.some((call) => call.body.action === "pause"))
      .toBe(true);
    await frigateApp.page
      .getByRole("button", { name: "Play recording", exact: true })
      .click();
    await expect
      .poll(() => state.calls.some((call) => call.body.action === "resume"))
      .toBe(true);
    await expect
      .poll(
        () =>
          state.calls.filter((call) => call.body.action === "activate").length,
      )
      .toBe(2);
    await frigateApp.page
      .getByRole("button", { name: "Forward 10 seconds", exact: true })
      .click();
    await expect
      .poll(
        () =>
          state.calls.filter((call) => call.body.action === "activate").length,
      )
      .toBe(3);
    const seek = state.calls.find((call) => call.body.action === "seek");
    expect(seek?.body.window?.start_timestamp).toBeGreaterThanOrEqual(
      state.timestamp + 10,
    );
    expect(state.calls.filter((call) => call.path === "sessions")).toHaveLength(
      1,
    );
    const generation = [...state.sessions.values()][0].generation;
    await frigateApp.page
      .getByRole("combobox", { name: "Adaptive recording viewing quality" })
      .click();
    await frigateApp.page
      .getByRole("option", { name: "1080p", exact: true })
      .click();
    await expect
      .poll(
        () =>
          video.evaluate((element: HTMLVideoElement) => element.videoHeight),
        { timeout: 15000 },
      )
      .toBe(1080);
    expect([...state.sessions.values()][0].generation).toBe(generation);
    expect(state.calls.filter((call) => call.path === "sessions")).toHaveLength(
      1,
    );
    await frigateApp.page
      .getByRole("button", { name: "Go back", exact: true })
      .click();
    await expect
      .poll(() => state.calls.some((call) => call.method === "DELETE"))
      .toBe(true);
    expect(state.forbidden).toEqual([]);
  });

  test("hidden recording stops encoding and resumes only when playback is still wanted", async ({
    frigateApp,
  }) => {
    const state = await installRecorded(frigateApp);
    await expect
      .poll(
        () =>
          state.calls.filter((call) => call.body.action === "activate").length,
      )
      .toBe(1);
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
      .poll(() => [...state.sessions.values()][0].state)
      .toBe("paused");
    await visibility("visible");
    await expect
      .poll(
        () =>
          state.calls.filter((call) => call.body.action === "activate").length,
      )
      .toBe(2);
    await frigateApp.page
      .getByRole("button", { name: "Pause recording", exact: true })
      .click();
    await expect
      .poll(() => [...state.sessions.values()][0].state)
      .toBe("paused");
    await visibility("hidden");
    await visibility("visible");
    expect(
      state.calls.filter((call) => call.body.action === "resume"),
    ).toHaveLength(1);
    expect(state.forbidden).toEqual([]);
  });

  test("recording Data Saver stays within 720p and does not expose a higher pin", async ({
    frigateApp,
  }) => {
    await frigateApp.page.addInitScript(() => {
      Object.defineProperty(navigator, "connection", {
        configurable: true,
        value: { saveData: true },
      });
    });
    const state = await installRecorded(frigateApp);
    await expect
      .poll(
        () =>
          state.calls.filter((call) => call.body.action === "activate").length,
      )
      .toBe(1);
    const creation = state.calls.find((call) => call.path === "sessions");
    expect(creation?.body).toMatchObject({ height: 720, max_height: 720 });
    await frigateApp.page
      .getByRole("combobox", { name: "Adaptive recording viewing quality" })
      .click();
    await expect(
      frigateApp.page.getByRole("option", { name: "1080p", exact: true }),
    ).toHaveCount(0);
    expect(state.forbidden).toEqual([]);
  });

  test("an initially hidden recording creates no encoder until it becomes visible", async ({
    frigateApp,
  }) => {
    await frigateApp.page.addInitScript(() => {
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        value: "hidden",
      });
    });
    const state = await installRecorded(frigateApp);
    await expect(
      frigateApp.page.locator("[data-recorded-transport=webrtc]"),
    ).toBeVisible();
    expect(state.calls.filter((call) => call.path === "sessions")).toHaveLength(
      0,
    );
    await frigateApp.page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        value: "visible",
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await expect
      .poll(
        () =>
          state.calls.filter((call) => call.body.action === "activate").length,
      )
      .toBe(1);
    await expect
      .poll(
        () =>
          state.calls.filter(
            (call) =>
              call.method === "PATCH" && !call.path.endsWith("/quality"),
          ).length,
      )
      .toBeGreaterThan(0);
    expect(state.forbidden).toEqual([]);
  });

  test("a finite window continues through a new peer generation, never a hidden preload", async ({
    frigateApp,
  }) => {
    const state = await installRecorded(frigateApp);
    await expect
      .poll(
        () =>
          state.calls.filter((call) => call.body.action === "activate").length,
      )
      .toBe(1);
    const session = [...state.sessions.values()][0];
    const nextTimestamp = session.window.before;
    await closeH264Peer(
      frigateApp.page,
      session.session_id + "_" + session.generation,
    );
    session.state = "ended";
    await expect
      .poll(
        () =>
          state.calls.filter((call) => call.body.action === "activate").length,
      )
      .toBe(2);
    const seek = state.calls.find((call) => call.body.action === "seek");
    expect(seek?.body.window?.start_timestamp).toBe(nextTimestamp);
    expect(state.calls.filter((call) => call.path === "sessions")).toHaveLength(
      1,
    );
    expect(state.forbidden).toEqual([]);
  });

  test("visible recording-only camera previews use bounded still images, not HLS video", async ({
    frigateApp,
  }) => {
    test.skip(
      frigateApp.isMobile,
      "Secondary recording previews are desktop-only",
    );
    const state = await installRecorded(frigateApp, false, true);
    await expect
      .poll(
        () =>
          state.calls.filter((call) => call.body.action === "activate").length,
      )
      .toBe(1);
    const image = frigateApp.page.locator(
      '[data-testid="recording-preview"][data-camera="backyard"] img',
    );
    await expect(image).toBeVisible();
    await expect
      .poll(() =>
        image.evaluate((element: HTMLImageElement) => element.naturalWidth),
      )
      .toBeGreaterThan(0);
    expect(
      state.snapshots.some(
        (url) => url.includes("/backyard/") && url.includes("height=180"),
      ),
    ).toBe(true);
    expect(state.forbidden).toEqual([]);
    expect(state.calls.filter((call) => call.path === "sessions")).toHaveLength(
      1,
    );
  });

  test("event tracking detail uses the same recorded WebRTC controls without a VOD playlist", async ({
    frigateApp,
  }) => {
    const state = await installRecorded(frigateApp);
    const event = {
      ...baseEvents[0],
      start_time: state.timestamp + 5,
      end_time: state.timestamp + 35,
    };
    await frigateApp.page.route("**/api/events?**", (route) =>
      route.fulfill({ json: [event] }),
    );
    await frigateApp.page.route("**/api/events/event-person-001", (route) =>
      route.fulfill({ json: event }),
    );
    await frigateApp.page.route("**/api/timeline?**", (route) =>
      route.fulfill({ json: [] }),
    );
    await frigateApp.page.route(
      "**/api/review/event/event-person-001",
      (route) => route.fulfill({ json: null }),
    );
    await frigateApp.goto("/explore?labels=person");
    await frigateApp.page
      .locator(`[data-start="${event.start_time}"] img`)
      .first()
      .click();
    await frigateApp.page
      .getByLabel("Select tracking_details", { exact: true })
      .click();
    const video = frigateApp.page.locator(
      '[data-recorded-transport="webrtc"] video',
    );
    await expect
      .poll(
        () =>
          video.evaluate((element: HTMLVideoElement) => element.videoHeight),
        { timeout: 15000 },
      )
      .toBe(720);
    expect(state.forbidden).toEqual([]);
  });

  test("pre-generated preview files stay file-based without an HLS wrapper", async ({
    frigateApp,
  }) => {
    test.skip(
      frigateApp.isMobile,
      "Secondary recording previews are desktop-only",
    );
    const state = await installRecorded(frigateApp, false, true, true);
    await expect
      .poll(
        () =>
          state.calls.filter((call) => call.body.action === "activate").length,
      )
      .toBe(1);
    const video = frigateApp.page.locator(
      'video[src*="fixture-recorded-preview"]',
    );
    await expect(video).toBeVisible();
    await expect
      .poll(() =>
        video.evaluate((element: HTMLVideoElement) => element.videoHeight),
      )
      .toBeGreaterThan(0);
    expect(state.forbidden).toEqual([]);
    expect(state.calls.filter((call) => call.path === "sessions")).toHaveLength(
      1,
    );
  });

  test("rapid recording seeks discard obsolete queued source generations", async ({
    frigateApp,
  }) => {
    const state = await installRecorded(frigateApp);
    await expect
      .poll(
        () =>
          state.calls.filter((call) => call.body.action === "activate").length,
      )
      .toBe(1);
    const release = state.holdNextSeek();
    const forward = frigateApp.page.getByRole("button", {
      name: "Forward 10 seconds",
      exact: true,
    });
    try {
      await forward.click();
      await expect
        .poll(
          () =>
            state.calls.filter((call) => call.body.action === "seek").length,
        )
        .toBe(1);
      await forward.click();
      await forward.click();
      await forward.click();
    } finally {
      release();
    }
    await expect
      .poll(
        () =>
          state.calls.filter((call) => call.body.action === "activate").length,
      )
      .toBe(2);
    const seeks = state.calls.filter((call) => call.body.action === "seek");
    expect(seeks).toHaveLength(2);
    expect(seeks.at(-1)?.body.window?.start_timestamp).toBeGreaterThanOrEqual(
      state.timestamp + 40,
    );
    expect(state.forbidden).toEqual([]);
  });

  test("offscreen recording releases its encoder until the player re-enters the viewport", async ({
    frigateApp,
  }) => {
    const state = await installRecorded(frigateApp);
    await expect
      .poll(
        () =>
          state.calls.filter((call) => call.body.action === "activate").length,
      )
      .toBe(1);
    const video = frigateApp.page.locator(
      '[data-recorded-transport="webrtc"] video',
    );
    await video.evaluate((element: HTMLVideoElement) => {
      element.style.transform = "translateX(200vw)";
    });
    await expect
      .poll(() => [...state.sessions.values()][0].state)
      .toBe("paused");
    await video.evaluate((element: HTMLVideoElement) => {
      element.style.transform = "";
    });
    await expect
      .poll(
        () =>
          state.calls.filter((call) => call.body.action === "activate").length,
      )
      .toBe(2);
    expect(state.forbidden).toEqual([]);
  });

  test.describe("unavailable recording", () => {
    test.use({
      expectedErrors: [
        [
          /503 Service Unavailable.*\/api\/front_door\/recordings\/webrtc\/sessions/,
          /Failed to load resource: the server responded with a status of 503/,
        ],
        { scope: "test" },
      ],
    });
    test("fails visibly without a streaming fallback", async ({
      frigateApp,
    }) => {
      const state = await installRecorded(frigateApp, true);
      await expect(
        frigateApp.page
          .locator("[data-recorded-transport=webrtc]")
          .getByRole("alert"),
      ).toBeVisible();
      expect(state.forbidden).toEqual([]);
    });
  });
});
