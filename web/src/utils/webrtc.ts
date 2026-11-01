import { baseUrl } from "@/api/baseUrl";

export function webrtcEndpoint(path: string): string {
  const root = new URL("api/", baseUrl);
  const url = new URL(path, root);
  if (url.origin !== root.origin || !url.pathname.startsWith(root.pathname)) {
    throw new Error("Media endpoint is outside the configured API");
  }
  return url.href;
}
