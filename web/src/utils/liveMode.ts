import type { LivePlayerMode } from "@/types/live";

export function resolveLivePlayerMode({
  isRestreamed,
  mseSupported,
  webRTCAvailable,
  webRTCChecking = false,
  requested,
  preferMse = false,
}: {
  isRestreamed: boolean;
  mseSupported: boolean;
  webRTCAvailable: boolean;
  webRTCChecking?: boolean;
  requested?: LivePlayerMode;
  preferMse?: boolean;
}): LivePlayerMode {
  if (!isRestreamed || requested === "jsmpeg") return "jsmpeg";
  if (requested === "mse" && mseSupported) return "mse";
  if (requested === undefined && preferMse && mseSupported) return "mse";
  if (webRTCAvailable || (requested === "webrtc" && webRTCChecking)) {
    return "webrtc";
  }
  return mseSupported ? "mse" : "jsmpeg";
}
