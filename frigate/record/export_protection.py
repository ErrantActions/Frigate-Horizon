"""Permanent protection belongs to the exported asset, never a bookmark."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from peewee import fn

from frigate.const import EXPORT_DIR, EXPORT_STAGING_DIR
from frigate.models import Export, User
from frigate.record.archive import storage_lock

if TYPE_CHECKING:
    from frigate.config import FrigateConfig


class ExportProtectionConflict(ValueError):
    """Protected evidence must be explicitly unprotected before removal."""


class ExportProtectionDenied(PermissionError):
    """Current account permissions no longer allow protecting this asset."""


def protected_export_exists(ids: list[str]) -> bool:
    """Use persisted protection, not an earlier in-memory export object."""
    return (
        Export.select()
        .where(Export.id.in_(ids), Export.retain_indefinitely == True)
        .exists()
    )


def protect_export_asset(
    identifier: str,
    camera: str,
    owner: str,
    config_provider: Callable[[], FrigateConfig],
) -> str:
    """Durably protect one complete, verified local exported asset."""
    with storage_lock():
        config = config_provider()
        account = User.get_or_none(User.username == owner)
        if (
            account is None
            or account.role != "admin"
            or account.role not in config.auth.roles
            or camera
            not in User.get_allowed_cameras(
                account.role, config.auth.roles, set(config.cameras)
            )
        ):
            raise ExportProtectionDenied("Export protection authorization changed")
        export = Export.get_or_none(Export.id == identifier, Export.camera == camera)
        if export is None:
            raise FileNotFoundError("Export no longer exists")
        if export.in_progress:
            raise ExportProtectionConflict("An in-progress export cannot be protected")
        path = Path(export.video_path)
        root = Path(EXPORT_DIR).resolve(strict=True)
        resolved = path.resolve(strict=True)
        if (
            path.is_symlink()
            or not resolved.is_relative_to(root)
            or resolved.is_relative_to(root / Path(EXPORT_STAGING_DIR).name)
        ):
            raise ExportProtectionConflict(
                "Export asset is outside the protected asset store"
            )
        original = path.lstat()
        if not stat.S_ISREG(original.st_mode) or original.st_size <= 0:
            raise ExportProtectionConflict("Export asset is not complete")
        flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            current = path.lstat()
            if (
                not stat.S_ISREG(opened.st_mode)
                or any(
                    (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
                    != (
                        original.st_dev,
                        original.st_ino,
                        original.st_size,
                        original.st_mtime_ns,
                    )
                    for value in (opened, current)
                )
                or path.is_symlink()
                or not path.resolve(strict=True).is_relative_to(root)
            ):
                raise ExportProtectionConflict(
                    "Export asset changed during verification"
                )
            latest_config = config_provider()
            if (
                "admin" not in latest_config.auth.roles
                or camera
                not in User.get_allowed_cameras(
                    "admin", latest_config.auth.roles, set(latest_config.cameras)
                )
            ):
                raise ExportProtectionDenied("Export protection authorization changed")
            authorized_account = User.select(User.username).where(
                User.username == owner,
                User.role == "admin",
                User.password_changed_at == account.password_changed_at,
            )
            changed = (
                Export.update(retain_indefinitely=True)
                .where(
                    Export.id == identifier,
                    Export.camera == camera,
                    Export.in_progress == False,
                    Export.video_path == str(path),
                    Export.retain_indefinitely == export.retain_indefinitely,
                    fn.EXISTS(authorized_account),
                )
                .execute()
            )
        finally:
            os.close(descriptor)
        if changed != 1:
            raise ExportProtectionConflict(
                "Export or authorization changed while protecting it"
            )
        return export.id
