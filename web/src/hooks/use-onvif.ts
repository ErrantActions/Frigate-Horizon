import axios from "axios";
import { useCallback, useState } from "react";
import useSWR from "swr";

const REQUEST_TIMEOUT = 45000;

export function onvifPath(camera: string, operation: string) {
  return `onvif/cameras/${encodeURIComponent(camera)}/${operation}`;
}

/** No camera is contacted until the caller explicitly enables the resource. */
export function useOnvif<T>(
  camera: string,
  operation: string,
  enabled: boolean,
  parameters?: Record<string, string>,
  refreshInterval = 0,
) {
  const query = new URLSearchParams(parameters).toString();
  const path = onvifPath(camera, operation) + (query ? `?${query}` : "");
  return useSWR<T>(enabled ? path : null, {
    fetcher: async (url: string) =>
      (await axios.get<T>(url, { timeout: REQUEST_TIMEOUT })).data,
    refreshInterval,
    shouldRetryOnError: false,
    revalidateOnFocus: false,
    revalidateOnReconnect: false,
  });
}

export function useOnvifMutation() {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const run = useCallback(
    async <T>(method: "post" | "put", path: string, body: unknown) => {
      setBusy(true);
      setFailed(false);
      setUncertain(false);
      try {
        const response = await axios.request<T>({
          method,
          url: path,
          data: body,
          timeout: REQUEST_TIMEOUT,
        });
        return response.data;
      } catch (error) {
        setFailed(true);
        setUncertain(
          axios.isAxiosError(error) &&
            (error.response?.status === 409 ||
              !error.response ||
              error.response.status >= 500),
        );
        return undefined;
      } finally {
        setBusy(false);
      }
    },
    [],
  );
  return { run, busy, failed, uncertain };
}
