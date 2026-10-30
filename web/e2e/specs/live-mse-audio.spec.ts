import type { Locator, Page } from "@playwright/test";
import { test, expect } from "../fixtures/frigate-test";

type MseProbe = { listeners: (() => void)[]; sent: number };
const expectedCodecFailureTest = test.extend({
  expectedErrors: [
    [
      /MSE error 'mse-codec'/,
      /Browser negotiated codecs:/,
      /Supported codecs:/,
    ],
    { option: true },
  ],
});

async function installLiveProtocol(
  page: Page,
  audio: boolean,
  replyGate: Promise<void> = Promise.resolve(),
) {
  await page.addInitScript((enabled) => {
    sessionStorage.setItem("liveAudio", JSON.stringify(enabled));
    HTMLMediaElement.prototype.play = () => Promise.resolve();
  }, audio);
  const offers: { source: string; codecs: string[] }[] = [];
  await page.routeWebSocket("**/live/mse/api/ws**", (socket) => {
    socket.onMessage(async (message) => {
      const request = JSON.parse(message.toString());
      if (request.type !== "mse") {
        return;
      }
      offers.push({
        source: new URL(socket.url()).searchParams.get("src") ?? "",
        codecs: request.value.split(","),
      });
      await replyGate;
      socket.send(
        JSON.stringify({
          type: "mse",
          value: 'video/mp4; codecs="avc1.640029"',
        }),
      );
    });
  });
  await page.route("**/api/go2rtc/streams/**", (route) =>
    route.fulfill({
      json: {
        producers: [
          { medias: ["video, recvonly, H264", "audio, recvonly, AAC"] },
        ],
        consumers: [],
      },
    }),
  );
  return offers;
}

async function failAudioDecoder(video: Locator) {
  await video.evaluate((element) => {
    if (!(element instanceof HTMLVideoElement)) {
      throw new Error("Expected the live video element");
    }
    Object.defineProperty(element, "error", {
      configurable: true,
      value: {
        code: MediaError.MEDIA_ERR_DECODE,
        message: "audio decoder initialization failed",
      },
    });
    element.dispatchEvent(new Event("error"));
    Reflect.deleteProperty(element, "error");
  });
}

test.describe("Live MSE audio negotiation @mobile", () => {
  test.beforeEach(async ({ frigateApp }) => {
    await frigateApp.installDefaults({
      config: {
        go2rtc: {
          streams: { front_door: ["rtsp://127.0.0.1:8554/front_door"] },
          webrtc: { candidates: [], ice_servers: [] },
        },
      },
    });
  });

  expectedCodecFailureTest(
    "global Live switch reports failure without a compatibility or low-bandwidth downgrade",
    async ({ frigateApp }) => {
      await frigateApp.installDefaults({
        config: {
          ui: { live_compatibility: false },
          go2rtc: {
            streams: { front_door: ["rtsp://127.0.0.1:8554/front_door"] },
            webrtc: { candidates: [], ice_servers: [] },
          },
          cameras: {
            front_door: {
              live: {
                streams: {
                  Main: "front_door",
                  Compatible: "front_door_transcode_720p",
                },
                transcode: {
                  enabled: true,
                  source: "front_door",
                  qualities: [{ height: 720, bitrate: 1500 }],
                },
              },
            },
          },
        },
      });
      await installLiveProtocol(frigateApp.page, false);
      const sources: string[] = [];
      await frigateApp.page.routeWebSocket("**/live/mse/api/ws**", (socket) => {
        sources.push(new URL(socket.url()).searchParams.get("src") ?? "");
        socket.onMessage((message) => {
          if (JSON.parse(message.toString()).type !== "mse") return;
          socket.send(
            JSON.stringify({
              type: "error",
              value: "mse: streams: codecs not matched: H265 => H264",
            }),
          );
        });
      });
      await frigateApp.goto("/#front_door");
      await expect(
        frigateApp.page.getByRole("alert").filter({
          hasText:
            "Live compatibility fallback is disabled by an administrator.",
        }),
      ).toBeVisible();
      expect(sources).toEqual(["front_door"]);
      await expect(
        frigateApp.page.getByRole("button", {
          name: "Retry playback",
          exact: true,
        }),
      ).toBeVisible();
    },
  );

  test("queues a high-bitrate burst without the old two-MiB overflow", async ({
    frigateApp,
  }) => {
    await installLiveProtocol(frigateApp.page, false);
    await frigateApp.page.addInitScript(() => {
      const sizes: number[] = [];
      let checks = 0;
      Reflect.set(window, "horizonAppendSizes", sizes);
      Reflect.set(window, "horizonQueueChecks", () => checks);
      const add = MediaSource.prototype.addSourceBuffer;
      MediaSource.prototype.addSourceBuffer = function (mime) {
        const buffer = add.call(this, mime);
        let busy = true;
        Object.defineProperty(buffer, "updating", {
          get: () => {
            checks++;
            return busy;
          },
        });
        Object.defineProperty(buffer, "appendBuffer", {
          value: (data: ArrayBuffer) => {
            sizes.push(data.byteLength);
            busy = true;
          },
        });
        Reflect.set(window, "horizonFlushQueue", () => {
          busy = false;
          buffer.dispatchEvent(new Event("updateend"));
        });
        return buffer;
      };
    });
    await frigateApp.page.routeWebSocket("**/live/mse/api/ws**", (socket) => {
      socket.onMessage((message) => {
        if (JSON.parse(message.toString()).type !== "mse") return;
        socket.send(
          JSON.stringify({
            type: "mse",
            value: 'video/mp4; codecs="avc1.640029"',
          }),
        );
        for (let index = 0; index < 3; index++) {
          socket.send(Buffer.alloc(1024 * 1024));
        }
      });
    });
    await frigateApp.goto("/#front_door");
    await expect
      .poll(() =>
        frigateApp.page.evaluate(() =>
          (Reflect.get(window, "horizonQueueChecks") as () => number)(),
        ),
      )
      .toBeGreaterThanOrEqual(3);
    await frigateApp.page.evaluate(() => {
      const flush = Reflect.get(window, "horizonFlushQueue") as () => void;
      flush();
      flush();
      flush();
    });
    expect(
      await frigateApp.page.evaluate(() =>
        Reflect.get(window, "horizonAppendSizes"),
      ),
    ).toEqual([1024 * 1024, 1024 * 1024, 1024 * 1024]);
  });

  test("cold transcode negotiation survives the short stall timeout", async ({
    frigateApp,
  }) => {
    let release = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    await frigateApp.page.clock.install();
    const offers = await installLiveProtocol(frigateApp.page, false, gate);
    await frigateApp.goto("/#front_door");
    await expect.poll(() => offers.length).toBe(1);
    await frigateApp.page.clock.fastForward(19000);
    await expect(
      frigateApp.page.locator('video[src^="blob:"]').first(),
    ).toBeAttached();
    expect(offers.length).toBe(1);
    release();
    await expect
      .poll(() =>
        frigateApp.page.evaluate(() => {
          const video = document.querySelector("video");
          return Boolean(video && !video.error);
        }),
      )
      .toBe(true);
  });

  test("muted live requests video only and audio toggles renegotiate", async ({
    frigateApp,
  }) => {
    const offers = await installLiveProtocol(frigateApp.page, false);
    await frigateApp.goto("/#front_door");
    await expect.poll(() => offers.length).toBe(1);
    expect(offers[0].source).toBe("front_door");
    expect(offers[0].codecs.length).toBeGreaterThan(0);
    expect(
      offers[0].codecs.every(
        (codec) => codec.startsWith("avc1.") || codec.startsWith("hvc1."),
      ),
    ).toBe(true);
    await frigateApp.page.keyboard.press("m");
    await expect.poll(() => offers.at(-1)?.codecs).toContain("mp4a.40.2");
    await frigateApp.page.keyboard.press("m");
    await expect
      .poll(() => offers.at(-1)?.codecs.includes("mp4a.40.2"))
      .toBe(false);
    expect(offers.every((offer) => offer.source === "front_door")).toBe(true);
  });

  test("audio decoder failure retries the original video without sound", async ({
    frigateApp,
  }) => {
    const offers = await installLiveProtocol(frigateApp.page, true);
    await frigateApp.goto("/#front_door");
    await expect.poll(() => offers.length).toBe(1);
    expect(offers[0].codecs).toContain("mp4a.40.2");
    const video = frigateApp.page.locator('video[src^="blob:"]').first();
    await expect(video).toBeAttached();
    await failAudioDecoder(video);
    await expect.poll(() => offers.length).toBe(2);
    expect(
      offers[1].codecs.every(
        (codec) => codec.startsWith("avc1.") || codec.startsWith("hvc1."),
      ),
    ).toBe(true);
    await expect(
      frigateApp.page.getByRole("status").filter({
        hasText: "Playing the original video without sound",
      }),
    ).toBeVisible();
    await expect(video).toHaveJSProperty("muted", true);
    expect(offers.every((offer) => offer.source === "front_door")).toBe(true);
    await frigateApp.page.keyboard.press("m");
    await expect
      .poll(() =>
        frigateApp.page.evaluate(() => sessionStorage.getItem("liveAudio")),
      )
      .toBe("false");
    await frigateApp.page.keyboard.press("m");
    await expect.poll(() => offers.at(-1)?.codecs).toContain("mp4a.40.2");
    await expect(
      frigateApp.page.getByRole("status").filter({
        hasText: "Playing the original video without sound",
      }),
    ).toHaveCount(0);
  });

  test("a stale source-open callback cannot negotiate a newer connection", async ({
    frigateApp,
  }) => {
    await frigateApp.page.addInitScript(() => {
      const probe: MseProbe = { listeners: [], sent: 0 };
      Reflect.set(window, "frigateMseProbe", probe);
      const addListener = EventTarget.prototype.addEventListener;
      EventTarget.prototype.addEventListener = function (
        type,
        listener,
        options,
      ) {
        if (this instanceof MediaSource && type === "sourceopen" && listener) {
          probe.listeners.push(() => {
            const event = new Event("sourceopen");
            if (typeof listener === "function") {
              listener.call(this, event);
            } else {
              listener.handleEvent(event);
            }
          });
          if (probe.listeners.length === 2) {
            return;
          }
        }
        addListener.call(this, type, listener, options);
      };
      const send = WebSocket.prototype.send;
      WebSocket.prototype.send = function (data) {
        if (
          this.url.includes("/live/mse/api/ws") &&
          typeof data === "string" &&
          JSON.parse(data).type === "mse"
        ) {
          probe.sent++;
        }
        send.call(this, data);
      };
    });
    const offers = await installLiveProtocol(frigateApp.page, false);
    await frigateApp.goto("/#front_door");
    await expect.poll(() => offers.length).toBe(1);
    await frigateApp.page.keyboard.press("m");
    await expect
      .poll(() =>
        frigateApp.page.evaluate(
          () =>
            (Reflect.get(window, "frigateMseProbe") as MseProbe).listeners
              .length,
        ),
      )
      .toBe(2);
    expect(
      await frigateApp.page.evaluate(() => {
        const probe = Reflect.get(window, "frigateMseProbe") as MseProbe;
        probe.listeners[0]();
        return probe.sent;
      }),
    ).toBe(1);
    await frigateApp.page.evaluate(() => {
      const probe = Reflect.get(window, "frigateMseProbe") as MseProbe;
      probe.listeners[1]();
    });
    await expect.poll(() => offers.length).toBe(2);
    expect(offers[1].codecs).toContain("mp4a.40.2");
  });
});
