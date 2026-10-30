import { test, expect } from "../fixtures/frigate-test";
import {
  adaptiveLiveStreams,
  dashboardLiveStreams,
} from "../../src/utils/adaptiveLive";
import type { CameraConfig } from "../../src/types/frigateConfig";
import { configFactory } from "../fixtures/mock-data/config";

test.describe("HLS-only quality policy @mobile", () => {
  test("offers only advertised HLS renditions, never a camera or MPEG fallback", () => {
    const camera: CameraConfig = configFactory().cameras.front_door;
    camera.live.streams = {
      Main: "front_door",
      Preview: "front_door_transcode_360p",
      Low: "front_door_transcode_720p",
      High: "front_door_transcode_1080p",
    };
    camera.live.transcode = {
      enabled: false,
      source: "front_door",
      qualities: [
        { height: 360, bitrate: 250 },
        { height: 720, bitrate: 1200 },
        { height: 1080, bitrate: 2500 },
        { height: 2160, bitrate: 6000 },
      ],
    };
    expect(adaptiveLiveStreams(camera)).toEqual([
      "front_door_transcode_1080p",
      "front_door_transcode_720p",
    ]);
    expect(dashboardLiveStreams(camera)).toEqual({
      Low: "front_door_transcode_720p",
      High: "front_door_transcode_1080p",
    });
    expect(camera.live.transcode.enabled).toBe(false);
  });

  test("does not invent a rendition when no configured HLS source is advertised", () => {
    const camera: CameraConfig = configFactory().cameras.front_door;
    camera.live.streams = { Main: "front_door" };
    expect(adaptiveLiveStreams(camera)).toEqual([]);
    expect(dashboardLiveStreams(camera)).toEqual({});
  });
});
