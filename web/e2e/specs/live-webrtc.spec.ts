import { test, expect, type FrigateApp } from "../fixtures/frigate-test";
import { h264Offer, closeH264Peer } from "../fixtures/webrtc-peer";

async function installWebRTC(app: FrigateApp, unavailable = false) {
  await app.installDefaults({
    config: {
      live_webrtc: { enabled: true, grid_height: 360, max_height: 1080 },
      ui: { live_hls: false, live_compatibility: false },
      go2rtc: { streams: { front_door: ["rtsp://synthetic.invalid/main"] } },
      cameras: {
        front_door: {
          capture_mode: "recording_only",
          live: {
            streams: {
              Main: "front_door",
              "2160p": "front_door_transcode_2160p",
            },
            transcode: {
              enabled: false,
              source: "front_door",
              qualities: [{ height: 720, bitrate: 1200 }],
            },
          },
        },
        backyard: { enabled: false },
        garage: { enabled: false },
      },
    },
  });
  await app.page.addInitScript(() => {
    window.liveTestPeers = new Map();
  });
  const calls: { method: string; path: string; body: unknown }[] = [];
  let sequence = 0;
  let hlsRequests = 0;
  await app.page.route("**/api/front_door/live/hls/**", async (route) => {
    hlsRequests += 1;
    await route.fulfill({ status: 503, json: { detail: "unexpected_hls" } });
  });
  const lease = (id: string) => {
    const base = `front_door/live/webrtc/sessions/${id}`;
    return {
      version: 1,
      protocol: "webrtc",
      session_id: id,
      expires_at: Date.now() / 1000 + 30,
      heartbeat_seconds: 5,
      answer_url: base + "/answer",
      candidates_url: base + "/candidates",
      heartbeat_url: base,
      close_url: base,
      quality_url: base + "/quality",
      ice_servers: [],
    };
  };
  await app.page.route("**/api/front_door/live/webrtc/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.split("/live/webrtc/")[1];
    const method = request.method();
    const body = request.postData() ? request.postDataJSON() : undefined;
    calls.push({ method, path, body });
    if (path === "status") {
      await route.fulfill({
        json: {
          version: 1,
          protocol: "webrtc",
          enabled: true,
          available: !unavailable,
          session_url: "front_door/live/webrtc/sessions",
          grid_height: 360,
          grid_rendition: { height: 360, bitrate: 500 },
          renditions: [
            { height: 360, bitrate: 500 },
            { height: 720, bitrate: 1200 },
            { height: 1080, bitrate: 2500 },
          ],
          relay_available: false,
          reason: unavailable ? "synthetic_unavailable" : null,
        },
      });
      return;
    }
    expect(request.headers()["x-csrf-token"]).toBe("1");
    if (method === "POST" && path === "sessions") {
      sequence += 1;
      const id = `00000000-0000-4000-8000-${String(sequence).padStart(12, "0")}`;
      const height = body.view === "grid" ? 360 : (body.height ?? 720);
      const offer = await h264Offer(app.page, id, height);
      await route.fulfill({
        status: 201,
        json: {
          ...lease(id),
          offer,
          ice_candidates: [],
          ice_transport_policy: "all",
          renditions:
            body.view === "grid"
              ? [{ height, bitrate: 500 }]
              : [720, 1080]
                  .filter((value) => value <= body.max_height)
                  .map((value) => ({
                    height: value,
                    bitrate: value === 720 ? 1200 : 2500,
                  })),
          initial_height: body.height ?? null,
        },
      });
      return;
    }
    const id = path.split("/")[1];
    if (path.endsWith("/answer")) {
      await app.page.evaluate(
        async ({ id, answer }) => {
          await window.liveTestPeers.get(id)?.peer.setRemoteDescription(answer);
        },
        { id, answer: body },
      );
    } else if (path.endsWith("/candidates")) {
      await app.page.evaluate(
        async ({ id, candidates }) => {
          const peer = window.liveTestPeers.get(id)?.peer;
          if (!peer) throw new Error("Synthetic peer was already closed");
          for (const candidate of candidates)
            await peer.addIceCandidate(candidate);
        },
        { id, candidates: body.candidates },
      );
    } else if (path.endsWith("/quality")) {
      await app.page.evaluate(
        ({ id, height }) => {
          const source = window.liveTestPeers.get(id);
          if (!source) throw new Error("Synthetic peer was already closed");
          source.canvas.width = Math.round((height * 16) / 9);
          source.canvas.height = height;
        },
        { id, height: body.height ?? 720 },
      );
    } else if (method === "DELETE") {
      await closeH264Peer(app.page, id);
    } else if (method === "PATCH" && !path.endsWith("/quality")) {
      await route.fulfill({ json: lease(id) });
      return;
    }
    await route.fulfill({ status: 204 });
  });
  return { calls, hlsRequests: () => hlsRequests };
}

test.describe("Owned on-demand WebRTC playback @mobile", () => {
  test("grid decodes real H264 and releases its session while hidden", async ({
    frigateApp,
  }) => {
    test.setTimeout(60000);
    const state = await installWebRTC(frigateApp);
    await frigateApp.goto("/");
    const video = frigateApp.page
      .locator('[data-camera="front_door"] [data-live-transport="webrtc"]')
      .first();
    await expect
      .poll(
        () =>
          video.evaluate((element: HTMLVideoElement) => ({
            width: element.videoWidth,
            height: element.videoHeight,
            frames: element.getVideoPlaybackQuality().totalVideoFrames,
          })),
        { timeout: 20000 },
      )
      .toMatchObject({
        width: 640,
        height: 360,
        frames: expect.any(Number),
      });
    await expect
      .poll(() =>
        video.evaluate(
          (element: HTMLVideoElement) =>
            element.getVideoPlaybackQuality().totalVideoFrames,
        ),
      )
      .toBeGreaterThan(5);
    const creates = () =>
      state.calls.filter(
        (call) => call.method === "POST" && call.path === "sessions",
      );
    expect(creates()).toHaveLength(1);
    expect(creates()[0].body).toMatchObject({
      view: "grid",
      height: 360,
      audio: false,
    });
    await expect
      .poll(() => state.calls.some((call) => call.method === "PATCH"))
      .toBe(true);
    await frigateApp.page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        value: "hidden",
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await expect
      .poll(() => state.calls.filter((call) => call.method === "DELETE").length)
      .toBe(1);
    expect(
      await frigateApp.page.evaluate(() => window.liveTestPeers.size),
    ).toBe(0);
    expect(state.hlsRequests()).toBe(0);
    await frigateApp.page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        value: "visible",
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await expect.poll(() => creates().length).toBe(2);
    await expect
      .poll(() =>
        video.evaluate((element: HTMLVideoElement) => element.videoWidth),
      )
      .toBe(640);
    await expect(frigateApp.page.getByRole("alert")).toHaveCount(0);
  });

  test("unavailable WebRTC is explicit and never silently falls back to HLS", async ({
    frigateApp,
  }) => {
    const state = await installWebRTC(frigateApp, true);
    await frigateApp.goto("/");
    await expect(frigateApp.page.getByRole("alert")).toContainText(
      "WebRTC live video is unavailable",
    );
    expect(state.hlsRequests()).toBe(0);
    expect(state.calls.filter((call) => call.method === "POST")).toHaveLength(
      0,
    );
  });

  test("Data Saver detail selects 720p, not the grid-only 360p profile", async ({
    frigateApp,
  }) => {
    const state = await installWebRTC(frigateApp);
    await frigateApp.page.addInitScript(() => {
      Object.defineProperty(navigator, "connection", {
        value: { saveData: true },
      });
    });
    await frigateApp.goto("/#front_door");
    const video = frigateApp.page
      .locator('#player-container [data-live-transport="webrtc"]')
      .first();
    await expect
      .poll(
        () =>
          state.calls
            .filter(
              (call) => call.method === "POST" && call.path === "sessions",
            )
            .map((call) => call.body),
        { timeout: 20000 },
      )
      .toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            view: "detail",
            height: 720,
            max_height: 720,
          }),
        ]),
      );
    await expect
      .poll(
        async () => ({
          height: await video.evaluate(
            (element: HTMLVideoElement) => element.videoHeight,
          ),
          senders: await frigateApp.page.evaluate(async () => {
            const values: {
              sourceHeight: number;
              height?: number;
              reason?: string;
            }[] = [];
            for (const source of window.liveTestPeers.values()) {
              const stats = await source.peer.getStats();
              stats.forEach((value) => {
                if (value.type === "outbound-rtp" && value.kind === "video") {
                  values.push({
                    sourceHeight: source.canvas.height,
                    height: value.frameHeight,
                    reason: value.qualityLimitationReason,
                  });
                }
              });
            }
            return values;
          }),
        }),
        { timeout: 20000 },
      )
      .toEqual({
        height: 720,
        senders: [
          { sourceHeight: 720, height: 720, reason: expect.any(String) },
        ],
      });
    await expect
      .poll(() =>
        frigateApp.page.evaluate(() =>
          [...window.liveTestPeers.values()].map(
            (source) => source.canvas.height,
          ),
        ),
      )
      .toEqual([720]);
    expect(state.hlsRequests()).toBe(0);
    await expect(frigateApp.page.getByRole("alert")).toHaveCount(0);
  });

  test("detail quality is capped independently of legacy HLS streams and changes on one peer", async ({
    frigateApp,
  }) => {
    test.setTimeout(60000);
    const state = await installWebRTC(frigateApp);
    await frigateApp.goto("/#front_door");
    const video = frigateApp.page
      .locator('#player-container [data-live-transport="webrtc"]')
      .first();
    await expect
      .poll(
        () =>
          video.evaluate((element: HTMLVideoElement) => element.videoHeight),
        { timeout: 20000 },
      )
      .toBe(720);
    await frigateApp.page
      .getByRole("button", { name: /front.*settings/i })
      .first()
      .click();
    const picker = frigateApp.page
      .getByRole("combobox")
      .filter({ hasText: /Auto/ })
      .first();
    await picker.click();
    await expect(
      frigateApp.page.getByRole("option", { name: "2160p", exact: true }),
    ).toHaveCount(0);
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
    await expect
      .poll(() =>
        state.calls
          .filter(
            (call) => call.method === "PATCH" && call.path.endsWith("/quality"),
          )
          .map((call) => call.body),
      )
      .toContainEqual({ height: 1080 });
    const creates = state.calls.filter(
      (call) =>
        call.method === "POST" &&
        call.path === "sessions" &&
        typeof call.body === "object" &&
        call.body !== null &&
        "view" in call.body &&
        call.body.view === "detail",
    );
    expect(creates).toHaveLength(1);
    await expect
      .poll(() => frigateApp.page.evaluate(() => window.liveTestPeers.size))
      .toBe(1);
    await expect(frigateApp.page.getByRole("alert")).toHaveCount(0);
  });
});
