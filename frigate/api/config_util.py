"""Shared helpers for applying a freshly parsed config to the running app."""

from typing import TYPE_CHECKING

from fastapi import FastAPI

from frigate.config import FrigateConfig
from frigate.config.camera.updater import (
    CameraConfigUpdateEnum,
    CameraConfigUpdateTopic,
)
from frigate.models import ArchiveTransfer, Recordings
from frigate.record.archive import apply_archive_retention

if TYPE_CHECKING:
    from frigate.storage import StorageMaintainer


def archive_location_locked(
    previous: FrigateConfig,
    updated: FrigateConfig,
    storage_maintainer: "StorageMaintainer | None" = None,
) -> bool:
    """Require an inactive, empty archive before changing its identity or path."""
    if (
        previous.storage.archive_path == updated.storage.archive_path
        and previous.storage.mount_id == updated.storage.mount_id
    ):
        return False
    return (
        previous.storage.enabled
        or Recordings.select()
        .where(
            (Recordings.storage_tier == "archive")
            | Recordings.archive_bytes.is_null(False)
        )
        .exists()
        or ArchiveTransfer.select().exists()
        or (
            storage_maintainer is not None
            and storage_maintainer.archive_config_pending()
        )
    )


def publish_camera_section_updates(
    app: FastAPI, config: FrigateConfig, update_type: CameraConfigUpdateEnum
) -> None:
    """Broadcast every camera's re-resolved value for a global section.

    Global sections are folded into each camera at parse time and the camera
    copies are what workers read, so send them rather than leave a worker to
    guess which cameras were inheriting.
    """
    for camera_name, camera_config in config.cameras.items():
        settings = getattr(camera_config, update_type.name, None)

        if settings is None:
            continue

        app.config_publisher.publish_update(
            CameraConfigUpdateTopic(update_type, camera_name), settings
        )


def swap_runtime_config(app: FastAPI, config: FrigateConfig) -> None:
    """Point every long-lived collaborator at a newly parsed config object.

    Both /api/config/set and camera deletion re-parse yaml into a fresh
    FrigateConfig and must rebind the same set of references, or the API and
    the dispatcher drift onto different objects (the API reports one camera
    state while the dispatcher acts on another). Runtime toggle overrides are
    re-layered last: the swap rebuilt every camera from yaml, so without this a
    camera the user turned off would silently come back on.
    """
    if config.storage.enabled or app.frigate_config.record.archive_retention_days:
        apply_archive_retention(config)
    app.frigate_config = config

    if app.config_holder is not None:
        app.config_holder.set(config)

    for collaborator in ("record_cleanup", "event_cleanup"):
        worker = getattr(app, collaborator, None)
        if worker is not None:
            worker.config = config
            if collaborator == "event_cleanup":
                worker.camera_keys = list(config.cameras)

    app.genai_manager.update_config(config)

    if app.profile_manager is not None:
        app.profile_manager.update_config(config)

    if app.stats_emitter is not None:
        app.stats_emitter.config = config
        app.stats_emitter.hardware_stats.set_config(config)

    if app.dispatcher is not None:
        app.dispatcher.config = config

        for comm in app.dispatcher.comms:
            comm.config = config

        # workers still hold the live toggle values, so correct only the
        # config object here rather than re-broadcasting every override
        app.dispatcher.reapply_runtime_state_to_config()

    if app.storage_maintainer is not None:
        app.storage_maintainer.update_config(config)

    environment_manager = getattr(app, "environment_manager", None)
    if environment_manager is not None:
        environment_manager.update_config(config)

    vector_previews = getattr(getattr(app, "state", None), "vector_previews", None)
    if vector_previews is not None:
        vector_previews.update_cameras(config.cameras)
