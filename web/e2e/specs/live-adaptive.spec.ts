import { test, expect, type FrigateApp } from "../fixtures/frigate-test";
import { LiveStreamGovernor } from "../../src/components/player/LiveStreamGovernor";

async function openResolutionMenu(app: FrigateApp) {
  const card = app.page.locator('[data-camera="front_door"]').first();
  const option = app.page.getByRole("menuitemcheckbox", {
    name: "Show resolution badges",
    exact: true,
  });
  if (app.isMobile) {
    await card.dispatchEvent("pointerdown", {
      pointerType: "touch",
      button: 0,
      clientX: 100,
      clientY: 150,
    });
    await option.waitFor();
    await card.dispatchEvent("pointerup", { pointerType: "touch", button: 0 });
  } else {
    await card.click({ button: "right" });
  }
  return option;
}

test.describe("Dynamic main-source live policy @mobile", () => {
  test("fast startup starts on the lowest video rendition, never the preview floor", () => {
    const governor = new LiveStreamGovernor(4, () => 0, true, true);
    expect(governor.getSnapshot()).toEqual({ rung: 2, reason: undefined });
    governor.setMinimumRung(2);
    governor.setMinimumRung(0);
    expect(governor.getSnapshot()).toEqual({ rung: 2, reason: undefined });
  });

  test("fast startup upgrades only after recent clean media, within ten seconds", () => {
    let now = 0;
    const governor = new LiveStreamGovernor(3, () => now, false, true);
    now = 120000;
    governor.tick();
    expect(governor.getSnapshot().rung).toBe(2);
    for (let index = 0; index < 4; index++) {
      now += 1000;
      governor.sample({ bytes: 10000, mediaSeconds: 1, wallSeconds: 1 });
      governor.tick();
    }
    expect(governor.getSnapshot().rung).toBe(1);
    expect(now).toBeLessThan(130000);
  });

  test("a failed startup upgrade restores conservative recovery backoff", () => {
    let now = 0;
    const governor = new LiveStreamGovernor(3, () => now, false, true);
    for (now = 4000; now <= 7000; now += 1000) {
      governor.sample({ bytes: 10000, mediaSeconds: 1, wallSeconds: 1 });
      governor.tick();
    }
    expect(governor.getSnapshot().rung).toBe(1);
    for (now = 11000; now <= 13000; now += 1000) {
      governor.sample({ bytes: 1000, mediaSeconds: 0.1, wallSeconds: 1 });
    }
    expect(governor.getSnapshot().rung).toBe(2);
    for (now = 14000; now <= 30000; now += 1000) {
      governor.sample({ bytes: 10000, mediaSeconds: 1, wallSeconds: 1 });
      governor.tick();
    }
    expect(governor.getSnapshot().rung).toBe(2);
  });

  test("fast startup honors Data Saver and explicit highest-quality retry", () => {
    let now = 0;
    const governor = new LiveStreamGovernor(3, () => now, false, true);
    governor.setHoldLow(true);
    for (now = 4000; now <= 12000; now += 1000) {
      governor.sample({ bytes: 10000, mediaSeconds: 1, wallSeconds: 1 });
      governor.tick();
    }
    expect(governor.getSnapshot().rung).toBe(2);
    governor.setHoldLow(false);
    governor.reset();
    expect(governor.getSnapshot().rung).toBe(0);
  });

  test("highest-quality retry still skips unsupported original and GPU codecs", () => {
    const governor = new LiveStreamGovernor(4, () => 0, false, true);
    governor.reset();
    expect(governor.getSnapshot().rung).toBe(0);
    expect(governor.playerError("mse-codec", true)).toBe(true);
    expect(governor.getSnapshot().rung).toBe(1);
    expect(governor.playerError("mse-codec", true)).toBe(true);
    expect(governor.getSnapshot().rung).toBe(2);
  });

  test("sustained browser frame drops lower resolution even with full delivery", () => {
    let now = 0;
    const governor = new LiveStreamGovernor(3, () => now, false);
    governor.setMinimumRung(1);
    for (now = 4000; now <= 6000; now += 1000) {
      governor.sample({
        bytes: 100000,
        mediaSeconds: 1,
        wallSeconds: 1,
        totalFrames: 100,
        droppedFrames: 20,
      });
    }
    expect(governor.getSnapshot()).toEqual({ rung: 2, reason: "decoder" });
    for (let index = 0; index < 65; index++) {
      now += 1000;
      governor.sample({ bytes: 100000, mediaSeconds: 1, wallSeconds: 1 });
      governor.tick();
    }
    expect(governor.getSnapshot().rung).toBe(1);
    now += 61000;
    governor.tick();
    expect(governor.getSnapshot().rung).toBe(1);
  });

  test("delivery shortfall lowers quality without inventing decoder pressure", () => {
    let now = 0;
    const governor = new LiveStreamGovernor(3, () => now, false);
    for (now = 4000; now <= 6000; now += 1000) {
      governor.sample({ bytes: 1000, mediaSeconds: 0.2, wallSeconds: 1 });
    }
    expect(governor.getSnapshot()).toEqual({ rung: 1, reason: "bandwidth" });
  });

  test("time without media samples is not treated as recovered bandwidth", () => {
    let now = 0;
    const governor = new LiveStreamGovernor(3, () => now, false);
    governor.playerError("stalled", true);
    now = 120000;
    governor.tick();
    expect(governor.getSnapshot().rung).toBe(1);
    governor.sample({ bytes: 1000, mediaSeconds: 1, wallSeconds: 1 });
    governor.tick();
    expect(governor.getSnapshot().rung).toBe(1);
  });

  test("recording-only quality never falls through to a decoded preview", () => {
    const governor = new LiveStreamGovernor(3, () => 0, false);
    expect(governor.playerError("mse-codec", true)).toBe(true);
    expect(governor.playerError("mse-codec", true)).toBe(true);
    expect(governor.playerError("mse-codec", true)).toBe(false);
    expect(governor.getSnapshot().rung).toBe(2);
    expect(governor.getSnapshot().reason).not.toBe("floor");
  });

  test("resize updates the pixel budget and Data Saver still wins", () => {
    const governor = new LiveStreamGovernor(3, () => 0, false);
    governor.setMinimumRung(2);
    expect(governor.getSnapshot()).toEqual({ rung: 2, reason: "viewport" });
    governor.setMinimumRung(1);
    expect(governor.getSnapshot().rung).toBe(1);
    governor.setHoldLow(true);
    governor.setMinimumRung(0);
    governor.reset();
    expect(governor.getSnapshot().rung).toBe(2);
  });

  test("startup bursts and invalid frame counters cannot force a downswitch", () => {
    let now = 0;
    const governor = new LiveStreamGovernor(4, () => now, false);
    const sample = {
      bytes: 1000,
      mediaSeconds: 1,
      wallSeconds: 1,
      totalFrames: 100,
      droppedFrames: 90,
    };
    for (now = 0; now < 3000; now += 1000) governor.sample(sample);
    for (now = 4000; now <= 8000; now += 1000)
      governor.sample({ ...sample, droppedFrames: -1 });
    expect(governor.getSnapshot().rung).toBe(0);
  });
});

async function installDynamic(
  app: FrigateApp,
  onClose?: (stream: string) => void,
  external = false,
  failing = false,
  gpu4k = false,
  compatibility = true,
) {
  await app.installDefaults({
    config: {
      ui: { live_compatibility: compatibility },
      camera_groups: {
        outdoor: { cameras: ["front_door"], order: 0, icon: "LuVideo" },
      },
      ...(external
        ? {
            go2rtc: {
              streams: {
                front_door_transcode_1080p: [
                  "ffmpeg:front_door#video=h264#height=1080#audio=copy#bitrate=2500k",
                ],
                front_door_transcode_720p: [
                  "ffmpeg:front_door#video=h264#height=720#audio=copy#bitrate=1200k",
                ],
                ...(gpu4k
                  ? {
                      front_door_live_gpu_4k: [
                        "ffmpeg:front_door#video=h264#height=2160#hardware#audio=copy#bitrate=8000k",
                      ],
                    }
                  : {}),
              },
            },
          }
        : {}),
      cameras: {
        front_door: {
          capture_mode: "recording_only",
          live: {
            height: 2160,
            streams: {
              Main: "front_door",
              Sub: "front_door_sub",
              "1080p": "front_door_transcode_1080p",
              "720p": "front_door_transcode_720p",
              "480p": "front_door_transcode_480p",
              "360p": "front_door_transcode_360p",
              ...(gpu4k ? { "4K GPU": "front_door_live_gpu_4k" } : {}),
            },
            transcode: {
              enabled: !external,
              source: "front_door",
              qualities: [
                { height: 1080, bitrate: 2500 },
                { height: 720, bitrate: 1200 },
                { height: 480, bitrate: 500 },
                { height: 360, bitrate: 250 },
              ],
            },
          },
        },
        backyard: { enabled: false },
        garage: { enabled: false },
      },
    },
    stats: {
      cameras: {
        front_door: { capture_mode: "recording_only", camera_fps: 0 },
      },
    },
  });
  const connections: string[] = [];
  await app.page.routeWebSocket("**/live/mse/api/ws**", (socket) => {
    const stream = new URL(socket.url()).searchParams.get("src") ?? "";
    connections.push(stream);
    socket.onClose(() => onClose?.(stream));
    socket.onMessage((raw) => {
      if (JSON.parse(raw.toString()).type === "mse")
        socket.send(
          JSON.stringify({
            type:
              failing &&
              (!gpu4k ||
                stream === "front_door" ||
                stream === "front_door_live_gpu_4k")
                ? "error"
                : "mse",
            value:
              failing &&
              (!gpu4k ||
                stream === "front_door" ||
                stream === "front_door_live_gpu_4k")
                ? "mse: streams: codecs not matched: H265 => H264"
                : 'video/mp4; codecs="avc1.640029"',
          }),
        );
    });
  });
  return connections;
}

test.describe("Minimum live-quality failure @mobile", () => {
  test.use({ expectedErrors: [/MSE error 'mse-codec'/] });
  test("fast startup does not open unavailable original or GPU 4K streams", async ({
    frigateApp,
  }) => {
    const opened = await installDynamic(
      frigateApp,
      undefined,
      true,
      true,
      true,
    );
    await frigateApp.goto("/#front_door");
    await expect.poll(() => opened).toContain("front_door_transcode_720p");
    expect(opened[0]).toBe("front_door_transcode_720p");
    expect(opened).not.toContain("front_door");
    expect(opened).not.toContain("front_door_live_gpu_4k");
    expect(opened).not.toContain("front_door_sub");
    expect(opened).not.toContain("front_door_transcode_480p");
  });
  test("a failing 720p minimum shows a retry instead of selecting a lower stream", async ({
    frigateApp,
  }) => {
    const opened = await installDynamic(frigateApp, undefined, true, true);
    await frigateApp.goto("/");
    const card = frigateApp.page.locator('[data-camera="front_door"]').first();
    await expect(card.getByRole("alert")).toContainText(
      "minimum allowed quality",
    );
    expect(opened).toContain("front_door_transcode_720p");
    expect(opened).not.toContain("front_door_transcode_480p");
    expect(opened).not.toContain("front_door_transcode_360p");
    expect(opened).not.toContain("front_door_sub");
    const before = opened.length;
    await card.getByRole("button", { name: /retry/i }).click();
    await expect.poll(() => opened.length).toBeGreaterThan(before);
    await expect(frigateApp.page).toHaveURL(/\/$/);
  });
});

test.describe("Dynamic main-source live viewing @mobile", () => {
  test("buffered startup bytes do not downgrade a stream before its first decoded frame", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    await page.clock.install();
    await page.addInitScript(() => {
      const original = MediaSource.prototype.addSourceBuffer;
      MediaSource.prototype.addSourceBuffer = function (mime: string) {
        const buffer = original.call(this, mime);
        Object.defineProperty(buffer, "appendBuffer", {
          value: () => buffer.dispatchEvent(new Event("updateend")),
        });
        return buffer;
      };
    });
    const opened = await installDynamic(
      frigateApp,
      undefined,
      true,
      false,
      true,
    );
    let sendMedia: (() => void) | undefined;
    await page.routeWebSocket("**/live/mse/api/ws**", (socket) => {
      opened.push(new URL(socket.url()).searchParams.get("src") ?? "");
      socket.onMessage((raw) => {
        if (JSON.parse(raw.toString()).type !== "mse") return;
        socket.send(
          JSON.stringify({
            type: "mse",
            value: 'video/mp4; codecs="avc1.640029"',
          }),
        );
        sendMedia = () => socket.send(Buffer.alloc(1024));
      });
    });
    await frigateApp.goto("/#front_door");
    await expect.poll(() => sendMedia !== undefined).toBe(true);
    const card = page.locator('[data-camera="front_door"]').first();
    await expect(card).toHaveAttribute(
      "data-live-stream",
      "front_door_transcode_720p",
    );
    for (let second = 0; second < 8; second++) {
      sendMedia?.();
      await page.clock.runFor(1000);
    }
    expect(opened).toEqual(["front_door_transcode_720p"]);
    await expect(card).toHaveAttribute(
      "data-live-stream",
      "front_door_transcode_720p",
    );
  });

  test("live playback has no not-live badge during loading, playback, or buffering", async ({
    frigateApp,
  }) => {
    const { page } = frigateApp;
    await page.clock.install();
    await installDynamic(frigateApp, undefined, false, false, false, false);
    await frigateApp.goto("/");
    const card = page.locator('[data-camera="front_door"]').first();
    const video = card.locator("video");
    const warning = card.getByTestId("live-warning-badge");
    await expect(warning).toHaveCount(0);
    await video.evaluate((element: HTMLVideoElement) => {
      Object.defineProperties(element, {
        videoWidth: { configurable: true, value: 1280 },
        videoHeight: { configurable: true, value: 720 },
        currentTime: { configurable: true, writable: true, value: 1 },
        paused: { configurable: true, value: false },
        getVideoPlaybackQuality: {
          configurable: true,
          value: () => ({
            totalVideoFrames: element.currentTime * 30,
            droppedVideoFrames: 0,
          }),
        },
        buffered: {
          configurable: true,
          value: { length: 1, start: () => 0, end: () => 2 },
        },
        play: { configurable: true, value: () => Promise.resolve() },
      });
      element.dispatchEvent(new Event("loadeddata"));
    });
    await expect(warning).toHaveCount(0);
    await page.clock.runFor(4000);
    await expect(warning).toHaveCount(0);
    await page.clock.runFor(1000);
    await expect(warning).toHaveCount(0);
    await video.evaluate((element: HTMLVideoElement) => {
      element.currentTime = 2;
    });
    await page.clock.runFor(1000);
    await expect(warning).toHaveCount(0);
    await video.evaluate((element: HTMLVideoElement) => {
      element.currentTime = 3;
      Object.defineProperty(element, "buffered", {
        configurable: true,
        value: { length: 1, start: () => 0, end: () => 20 },
      });
    });
    await page.clock.runFor(1000);
    await expect(warning).toHaveCount(0);
    await video.evaluate((element: HTMLVideoElement) => {
      element.currentTime = 19;
    });
    await page.clock.runFor(1000);
    await expect(warning).toHaveCount(0);
  });

  for (const pinnedStream of ["front_door", "front_door_live_gpu_4k"]) {
    test(`explicit ${pinnedStream} selection overrides native-first Auto`, async ({
      frigateApp,
    }) => {
      const opened = await installDynamic(
        frigateApp,
        undefined,
        true,
        false,
        true,
      );
      await frigateApp.goto("/");
      await frigateApp.page.evaluate(async (stream) => {
        await new Promise<void>((resolve, reject) => {
          const request = indexedDB.open("keyval-store", 1);
          request.onupgradeneeded = () =>
            request.result.createObjectStore("keyval");
          request.onerror = () => reject(request.error);
          request.onsuccess = () => {
            const transaction = request.result.transaction(
              "keyval",
              "readwrite",
            );
            transaction
              .objectStore("keyval")
              .put(stream, "front_door-stream:admin");
            transaction.oncomplete = () => resolve();
            transaction.onerror = () => reject(transaction.error);
          };
        });
      }, pinnedStream);
      await frigateApp.goto("/#front_door");
      const card = frigateApp.page
        .locator('[data-camera="front_door"]')
        .first();
      await expect(card).toHaveAttribute("data-live-stream", pinnedStream);
      await expect(card).toHaveAttribute("data-live-auto", "false");
      await expect.poll(() => opened).toContain(pinnedStream);
    });
  }
  test("the grid never selects the GPU 4K tier", async ({ frigateApp }) => {
    const opened = await installDynamic(
      frigateApp,
      undefined,
      true,
      false,
      true,
    );
    await frigateApp.goto("/");
    const card = frigateApp.page.locator('[data-camera="front_door"]').first();
    await expect(card).toHaveAttribute(
      "data-live-stream",
      /transcode_(720|1080)p/,
    );
    await expect.poll(() => opened.length).toBeGreaterThan(0);
    expect(opened).not.toContain("front_door_live_gpu_4k");
  });

  test("expanded Auto starts on the low-bitrate video rendition before probing higher quality", async ({
    frigateApp,
  }) => {
    const opened = await installDynamic(
      frigateApp,
      undefined,
      true,
      false,
      true,
    );
    await frigateApp.goto("/#front_door");
    await expect.poll(() => opened).toContain("front_door_transcode_720p");
    await expect(
      frigateApp.page.locator('[data-camera="front_door"]').first(),
    ).toHaveAttribute("data-live-stream", "front_door_transcode_720p");
    expect(opened).not.toContain("front_door_live_gpu_4k");
    expect(opened).not.toContain("front_door_sub");
  });
  test("cached live video starts near its buffered live edge", async ({
    frigateApp,
  }) => {
    await installDynamic(frigateApp);
    await frigateApp.goto("/");
    const card = frigateApp.page.locator('[data-camera="front_door"]').first();
    const video = card.locator("video");
    await video.evaluate((element) => {
      Object.defineProperties(element, {
        videoWidth: { configurable: true, value: 1280 },
        videoHeight: { configurable: true, value: 720 },
        currentTime: { configurable: true, writable: true, value: 0 },
        buffered: {
          configurable: true,
          value: { length: 1, start: () => 0, end: () => 15 },
        },
        play: { configurable: true, value: () => Promise.resolve() },
      });
      element.dispatchEvent(new Event("loadeddata"));
    });
    await expect
      .poll(() =>
        video.evaluate((element: HTMLVideoElement) => element.currentTime),
      )
      .toBe(14.25);
    await expect(card.getByRole("alert")).toHaveCount(0);
  });

  test("an old buffering watchdog cannot stop a tile after offscreen reconnection", async ({
    frigateApp,
  }) => {
    await frigateApp.page.clock.install();
    const closed: string[] = [];
    const opened = await installDynamic(frigateApp, (stream) =>
      closed.push(stream),
    );
    await frigateApp.goto("/");
    const card = frigateApp.page.locator('[data-camera="front_door"]').first();
    const video = card.locator("video");
    await expect.poll(() => opened.length).toBeGreaterThan(0);
    await video.evaluate((element) => {
      Object.defineProperties(element, {
        videoWidth: { configurable: true, value: 1280 },
        videoHeight: { configurable: true, value: 720 },
        currentTime: { configurable: true, writable: true, value: 0 },
        paused: { configurable: true, value: false },
        buffered: {
          configurable: true,
          value: { length: 1, start: () => 0, end: () => 2 },
        },
        play: { configurable: true, value: () => Promise.resolve() },
      });
      element.dispatchEvent(new Event("loadeddata"));
    });
    await expect(card.getByTestId("live-resolution-badge")).toBeVisible();
    await video.dispatchEvent("progress");
    await card.evaluate((element) => {
      element.style.transform = "translateY(200vh)";
    });
    await frigateApp.page.clock.fastForward(50);
    await expect.poll(() => closed.length).toBeGreaterThan(0);
    const connections = opened.length;
    await video.evaluate((element) => {
      Object.defineProperty(element, "paused", {
        configurable: true,
        value: true,
      });
    });
    await card.evaluate((element) => {
      element.style.transform = "";
    });
    await frigateApp.page.clock.fastForward(50);
    await expect.poll(() => opened.length).toBeGreaterThan(connections);
    const closedAfterResume = closed.length;
    await frigateApp.page.clock.fastForward(4000);
    expect(closed.length).toBe(closedAfterResume);
    await expect(card.getByRole("alert")).toHaveCount(0);
  });

  test("go2rtc-managed live renditions also adapt automatically", async ({
    frigateApp,
  }) => {
    const opened = await installDynamic(frigateApp, undefined, true);
    await frigateApp.goto("/");
    const card = frigateApp.page.locator('[data-camera="front_door"]').first();
    await expect(card).toHaveAttribute(
      "data-live-stream",
      /front_door_transcode_(720|1080)p/,
    );
    await expect
      .poll(() => opened.some((name) => /transcode_(720|1080)p$/.test(name)))
      .toBe(true);
    expect(opened).not.toContain("front_door_sub");
    expect(opened).not.toContain("front_door_transcode_480p");
    expect(opened).not.toContain("front_door_transcode_360p");
  });
  test("the resolution badge uses actual video dimensions, updates, and stays hidden after reload", async ({
    frigateApp,
  }) => {
    test.setTimeout(60000);
    await installDynamic(frigateApp);
    await frigateApp.goto("/");
    const card = frigateApp.page.locator('[data-camera="front_door"]').first();
    await expect(card).toHaveAttribute("data-live-stream", /transcode_/);
    await expect(card.getByTestId("live-resolution-badge")).toHaveCount(0);
    const video = card.locator("video");
    await video.evaluate((element) => {
      Object.defineProperty(element, "videoWidth", {
        configurable: true,
        value: 1920,
      });
      Object.defineProperty(element, "videoHeight", {
        configurable: true,
        value: 1080,
      });
      Object.defineProperty(element, "play", {
        configurable: true,
        value: () => Promise.resolve(),
      });
      element.dispatchEvent(new Event("loadeddata"));
    });
    await expect(card.getByTestId("live-resolution-badge")).toContainText(
      "1920 x 1080",
    );
    await video.evaluate((element) => {
      Object.defineProperty(element, "videoWidth", {
        configurable: true,
        value: 1280,
      });
      Object.defineProperty(element, "videoHeight", {
        configurable: true,
        value: 720,
      });
      Object.defineProperty(element, "play", {
        configurable: true,
        value: () => Promise.resolve(),
      });
      element.dispatchEvent(new Event("resize"));
    });
    await expect(card.getByTestId("live-resolution-badge")).toContainText(
      "1280 x 720",
    );
    await expect(
      card.getByRole("button", { name: "Hide live resolution badges" }),
    ).toHaveCount(0);
    const playingVideo = await video.elementHandle();
    if (!playingVideo)
      throw new Error("Expected the existing live video element");
    const resolutionOption = await openResolutionMenu(frigateApp);
    await expect(resolutionOption).toBeChecked();
    await resolutionOption.click();
    await expect(card.getByTestId("live-resolution-badge")).toHaveCount(0);
    expect(await playingVideo.evaluate((element) => element.isConnected)).toBe(
      true,
    );
    await expect(frigateApp.page).toHaveURL(/\/$/);
    await frigateApp.page.reload();
    await card.locator("video").evaluate((element) => {
      Object.defineProperty(element, "play", {
        configurable: true,
        value: () => Promise.resolve(),
      });
      Object.defineProperty(element, "videoWidth", {
        configurable: true,
        value: 1280,
      });
      Object.defineProperty(element, "videoHeight", {
        configurable: true,
        value: 720,
      });
      element.dispatchEvent(new Event("loadeddata"));
    });
    await expect(card.getByTestId("live-resolution-badge")).toHaveCount(0);
    await frigateApp.goto("/settings?page=uiSettings");
    const toggle = frigateApp.page.locator(
      frigateApp.isMobile ? "#live-resolution" : "#live-resolution-desktop",
    );
    await expect(toggle).toHaveAttribute("data-state", "unchecked");
    await toggle.click();
    await expect(toggle).toHaveAttribute("data-state", "checked");
    await frigateApp.goto("/");
    await card.locator("video").evaluate((element) => {
      Object.defineProperty(element, "videoWidth", {
        configurable: true,
        value: 1280,
      });
      Object.defineProperty(element, "videoHeight", {
        configurable: true,
        value: 720,
      });
      Object.defineProperty(element, "play", {
        configurable: true,
        value: () => Promise.resolve(),
      });
      element.dispatchEvent(new Event("loadeddata"));
    });
    await expect(card.getByTestId("live-resolution-badge")).toContainText(
      "1280 x 720",
    );
  });

  test("custom-grid resolution options synchronize with UI settings without live-warning badges", async ({
    frigateApp,
  }) => {
    await installDynamic(frigateApp);
    await frigateApp.goto("/?group=outdoor");
    if (!frigateApp.isMobile) {
      await expect(frigateApp.page.locator(".react-grid-layout")).toBeVisible();
    }
    const card = frigateApp.page.locator('[data-camera="front_door"]').first();
    await expect(card.getByTestId("live-warning-badge")).toHaveCount(0);
    const option = await openResolutionMenu(frigateApp);
    await expect(option).toBeChecked();
    await option.click();
    await expect(card.getByTestId("live-warning-badge")).toHaveCount(0);
    await expect(frigateApp.page).toHaveTitle(/Outdoor/);
    const hidden = await openResolutionMenu(frigateApp);
    await expect(hidden).not.toBeChecked();
    await frigateApp.page.keyboard.press("Escape");
    await frigateApp.goto("/settings?page=uiSettings");
    await expect(
      frigateApp.page.locator(
        frigateApp.isMobile ? "#live-resolution" : "#live-resolution-desktop",
      ),
    ).toHaveAttribute("data-state", "unchecked");
  });

  test("the grid does not download the expanded camera's heavy controls", async ({
    frigateApp,
  }) => {
    const resources: string[] = [];
    frigateApp.page.on("request", (request) => {
      resources.push(new URL(request.url()).pathname);
    });
    await installDynamic(frigateApp);
    await frigateApp.goto("/");
    await expect(
      frigateApp.page.locator('[data-camera="front_door"]').first(),
    ).toHaveAttribute("data-live-auto", "true");
    expect(
      resources.filter((path) =>
        /\/assets\/(LiveCameraView-|DebugDrawingLayer-|OnvifImagingPanel-)/.test(
          path,
        ),
      ),
    ).toEqual([]);
  });

  test("resizing during startup does not force an unmeasured bitrate upgrade", async ({
    frigateApp,
  }) => {
    await installDynamic(frigateApp);
    await frigateApp.goto("/");
    const card = frigateApp.page.locator('[data-camera="front_door"]').first();
    await card.evaluate((element) => {
      element.style.height = "100px";
    });
    await expect(card).toHaveAttribute(
      "data-live-stream",
      "front_door_transcode_720p",
    );
    await card.evaluate((element) => {
      element.style.height = "800px";
    });
    await expect(card).toHaveAttribute(
      "data-live-stream",
      "front_door_transcode_720p",
    );
  });

  test("moving a tile off screen disconnects its live consumer", async ({
    frigateApp,
  }) => {
    const closed: string[] = [];
    const opened = await installDynamic(frigateApp, (stream) =>
      closed.push(stream),
    );
    await frigateApp.goto("/");
    const card = frigateApp.page.locator('[data-camera="front_door"]').first();
    await expect.poll(() => opened.length).toBeGreaterThan(0);
    const active = opened.at(-1);
    await card.evaluate((element) => {
      element.style.transform = "translateY(200vh)";
    });
    await expect.poll(() => closed).toContain(active);
  });

  test("the default grid starts derived video and never chooses the camera substream", async ({
    frigateApp,
  }) => {
    const opened = await installDynamic(frigateApp);
    await frigateApp.goto("/");
    const card = frigateApp.page.locator('[data-camera="front_door"]').first();
    await expect(card).toHaveAttribute("data-live-auto", "true");
    await expect(card).toHaveAttribute(
      "data-live-stream",
      /front_door_transcode_/,
    );
    await expect
      .poll(() =>
        opened.some((name) => name.startsWith("front_door_transcode_")),
      )
      .toBe(true);
    expect(opened).not.toContain("front_door_sub");
    expect(opened).not.toContain("front_door");
    expect(opened).not.toContain("front_door_transcode_480p");
    expect(opened).not.toContain("front_door_transcode_360p");
  });

  test("the named grid uses the same automatic main-derived policy", async ({
    frigateApp,
  }) => {
    const opened = await installDynamic(frigateApp);
    await frigateApp.goto("/?group=outdoor");
    const card = frigateApp.page.locator('[data-camera="front_door"]').first();
    await expect(card).toHaveAttribute(
      "data-live-stream",
      /front_door_transcode_/,
    );
    await expect
      .poll(() =>
        opened.some((name) => name.startsWith("front_door_transcode_")),
      )
      .toBe(true);
    expect(opened).not.toContain("front_door_sub");
  });

  test("single-camera Auto excludes camera substreams from its ladder", async ({
    frigateApp,
  }) => {
    const opened = await installDynamic(frigateApp);
    await frigateApp.goto("/#front_door");
    await expect.poll(() => opened.length).toBeGreaterThan(0);
    expect(
      opened.every((name) =>
        /^front_door(?:_transcode_(720|1080)p)?$/.test(name),
      ),
    ).toBe(true);
    expect(opened).not.toContain("front_door_sub");
  });
});
