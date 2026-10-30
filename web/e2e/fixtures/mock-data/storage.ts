import type { RecordingStorageTiers } from "../../../src/types/storage";

export const STORAGE_TIERS: RecordingStorageTiers = {
  settings: {
    enabled: true,
    archive_path: "/media/frigate/archive",
    mount_id: "frigate-archive",
    local_budget_gib: 1024,
    drain_percent: 90,
    retain_days: 90,
    transfer_mib_per_second: 0,
    emergency_delete: true,
  },
  local: { usage_mib: 512 * 1024, recordings: 1000 },
  archive: { usage_mib: 4096 * 1024, recordings: 8000 },
  cameras: {
    front_door: {
      friendly_name: "Front Door",
      tiers: {
        local: { usage_mib: 256 * 1024, recordings: 500 },
        archive: { usage_mib: 2048 * 1024, recordings: 4000 },
      },
    },
    backyard: {
      friendly_name: "Backyard",
      tiers: {
        local: { usage_mib: 160 * 1024, recordings: 300 },
        archive: { usage_mib: 1536 * 1024, recordings: 3000 },
      },
    },
    garage: {
      friendly_name: "Garage",
      tiers: {
        local: { usage_mib: 96 * 1024, recordings: 200 },
        archive: { usage_mib: 512 * 1024, recordings: 1000 },
      },
    },
  },
  health: {
    state: "ready",
    updated_at: 1700000000,
    error: null,
    archive_free_mib: 40000 * 1024,
    archive_total_mib: 48000 * 1024,
    backlog_mib: 0,
  },
};
