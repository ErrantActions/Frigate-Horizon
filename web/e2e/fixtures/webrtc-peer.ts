import type { Page } from "@playwright/test";

declare global {
  interface Window {
    liveTestPeers: Map<
      string,
      {
        peer: RTCPeerConnection;
        canvas: HTMLCanvasElement;
        track: MediaStreamTrack;
        timer: number;
      }
    >;
  }
}

export async function h264Offer(page: Page, id: string, height: number) {
  return page.evaluate(
    async ({ id, height }) => {
      window.liveTestPeers ??= new Map();
      const peer = new RTCPeerConnection({ iceServers: [] });
      const canvas = document.createElement("canvas");
      canvas.width = Math.round((height * 16) / 9);
      canvas.height = height;
      const context = canvas.getContext("2d");
      if (!context) throw new Error("Synthetic video canvas is unavailable");
      let frame = 0;
      const draw = () => {
        context.fillStyle = frame++ % 2 ? "#205080" : "#508020";
        context.fillRect(0, 0, canvas.width, canvas.height);
        context.fillStyle = "#ffffff";
        context.fillRect(frame % canvas.width, 40, 100, 100);
      };
      draw();
      const timer = window.setInterval(draw, 100);
      const track = canvas.captureStream(10).getVideoTracks()[0];
      track.contentHint = "detail";
      const transceiver = peer.addTransceiver(track, { direction: "sendonly" });
      const codecs = RTCRtpSender.getCapabilities("video")?.codecs.filter(
        (codec) => codec.mimeType.toLowerCase() === "video/h264",
      );
      if (!codecs?.length) throw new Error("H.264 is required by this test");
      transceiver.setCodecPreferences(codecs);
      window.liveTestPeers.set(id, { peer, canvas, track, timer });
      await peer.setLocalDescription(await peer.createOffer());
      const parameters = transceiver.sender.getParameters();
      parameters.degradationPreference = "maintain-resolution";
      await transceiver.sender.setParameters(parameters);
      if (peer.iceGatheringState !== "complete") {
        await new Promise<void>((ready, reject) => {
          const deadline = setTimeout(
            () => reject(new Error("Synthetic ICE gathering timed out")),
            4000,
          );
          peer.onicegatheringstatechange = () => {
            if (peer.iceGatheringState === "complete") {
              clearTimeout(deadline);
              ready();
            }
          };
        });
      }
      return { type: "offer" as const, sdp: peer.localDescription?.sdp };
    },
    { id, height },
  );
}

export async function closeH264Peer(page: Page, id: string) {
  await page.evaluate((id) => {
    const source = window.liveTestPeers.get(id);
    source?.peer.close();
    source?.track.stop();
    clearInterval(source?.timer);
    window.liveTestPeers.delete(id);
  }, id);
}
