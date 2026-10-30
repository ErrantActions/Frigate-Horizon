import { test, expect } from "../fixtures/frigate-test";
import { resolveLivePlayerMode } from "../../src/utils/liveMode";

test.describe("Compatible WebRTC defaults @mobile", () => {
  const supported = {
    isRestreamed: true,
    mseSupported: true,
    webRTCAvailable: true,
  };

  test("prefers verified WebRTC without an explicit user choice", () => {
    expect(resolveLivePlayerMode(supported)).toBe("webrtc");
  });

  test("main-derived live profiles start with MSE without changing legacy defaults", () => {
    expect(resolveLivePlayerMode({ ...supported, preferMse: true })).toBe(
      "mse",
    );
    expect(resolveLivePlayerMode(supported)).toBe("webrtc");
  });

  test("main-derived defaults preserve explicit WebRTC and unsupported-MSE fallbacks", () => {
    expect(
      resolveLivePlayerMode({
        ...supported,
        preferMse: true,
        requested: "webrtc",
      }),
    ).toBe("webrtc");
    expect(
      resolveLivePlayerMode({
        ...supported,
        preferMse: true,
        mseSupported: false,
      }),
    ).toBe("webrtc");
  });

  test("keeps an explicit MSE or low-bandwidth choice", () => {
    expect(resolveLivePlayerMode({ ...supported, requested: "mse" })).toBe(
      "mse",
    );
    expect(resolveLivePlayerMode({ ...supported, requested: "jsmpeg" })).toBe(
      "jsmpeg",
    );
  });

  test("does not assume a pending connectivity probe has succeeded", () => {
    expect(
      resolveLivePlayerMode({
        ...supported,
        webRTCAvailable: false,
        webRTCChecking: true,
      }),
    ).toBe("mse");
  });

  test("preserves an explicit WebRTC preference during its connectivity probe", () => {
    expect(
      resolveLivePlayerMode({
        ...supported,
        webRTCAvailable: false,
        webRTCChecking: true,
        requested: "webrtc",
      }),
    ).toBe("webrtc");
  });

  test("falls back when WebRTC is unreachable or incompatible", () => {
    expect(
      resolveLivePlayerMode({
        ...supported,
        webRTCAvailable: false,
        requested: "webrtc",
      }),
    ).toBe("mse");
    expect(
      resolveLivePlayerMode({
        ...supported,
        webRTCAvailable: false,
        mseSupported: false,
      }),
    ).toBe("jsmpeg");
  });

  test("uses WebRTC when MSE is unavailable and never restreams a non-restreamed camera", () => {
    expect(
      resolveLivePlayerMode({
        ...supported,
        mseSupported: false,
        requested: "mse",
      }),
    ).toBe("webrtc");
    expect(
      resolveLivePlayerMode({
        ...supported,
        isRestreamed: false,
      }),
    ).toBe("jsmpeg");
  });
});
