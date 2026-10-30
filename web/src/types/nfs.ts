export type NfsShare = {
  role: "archive" | "recordings";
  server: string;
  export: string;
  version: "4.1" | "4.2" | "3";
  connections: number;
};

export type NfsMount = {
  container_path: string;
  mounted: boolean;
  filesystem: string | null;
  source: string | null;
  configured_connections: number | null;
  access_verified: boolean;
};

export type NfsStatus = {
  mounts: Record<NfsShare["role"], NfsMount>;
  archive_path: string;
  mount_id: string;
  local_budget_gib: number;
  runtime_uid: number | null;
  runtime_gid: number | null;
  automatic_remounts: boolean;
  recording_enabled: boolean;
};

export type NfsPlan = {
  volume_definitions_yaml: string;
  service_volumes_yaml: string;
  frigate_storage_yaml: string | null;
  local_first: boolean;
  enables_recording: false;
  archive_marker: { filename: string; contents: string } | null;
};

export type NfsProbe = {
  reachable: boolean;
  export_verified: false;
  port: 2049;
  error?: "timeout" | "dns_failed" | "address_not_remote" | "unreachable";
};
