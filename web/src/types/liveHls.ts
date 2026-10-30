export type LiveHlsRendition = {
  height: number;
  bitrate: number;
};

export type LiveHlsStatus = {
  enabled: boolean;
  available: boolean;
  playlist: string | null;
  renditions: LiveHlsRendition[];
};
