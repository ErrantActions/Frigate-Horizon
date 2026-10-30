"""Persist archive hysteresis intent without changing the configured watermarks."""

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from frigate.record.archive import fsync_directory

IDENTITY_FIELDS = (
    "archive_path",
    "mount_id",
    "local_budget_gib",
    "drain_percent",
    "start_percent",
)


class ArchiveDrainState:
    """Retain an admitted drain through restarts until its low-water target is met."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._identity: dict[str, object] | None = None
        self._active = False
        self._dirty = False

    def load(self, settings: Mapping[str, object]) -> bool:
        """Load matching intent, rejecting malformed state instead of inventing it."""
        identity = {
            key: settings.get(key, 100) if key == "start_percent" else settings[key]
            for key in IDENTITY_FIELDS
        }
        if identity == self._identity:
            return self._active
        if self.path.is_symlink():
            raise ValueError("Archive drain state must not be a symbolic link")
        try:
            if self.path.stat().st_size > 4096:
                raise ValueError("Archive drain state exceeds its size limit")
            contents = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._identity, self._active, self._dirty = identity, False, False
            return False
        data = json.loads(contents)
        if (
            not isinstance(data, dict)
            or type(data.get("version")) is not int
            or data.get("version") != 1
            or type(data.get("active")) is not bool
            or not isinstance(data.get("identity"), dict)
            or set(data["identity"])
            not in (set(IDENTITY_FIELDS), set(IDENTITY_FIELDS) - {"start_percent"})
        ):
            raise ValueError("Invalid archive drain state")
        legacy = "start_percent" not in data["identity"]
        saved_identity = {**data["identity"]}
        saved_identity.setdefault("start_percent", 100)
        matches = saved_identity == identity
        self._identity = identity
        self._active = data["active"] if matches else False
        self._dirty = not matches or legacy
        return self._active

    def save(self, settings: Mapping[str, object], active: bool) -> None:
        """Durably record a transition before transport releases any local footage."""
        if type(active) is not bool:
            raise ValueError("Archive drain activity must be a boolean")
        current = self.load(settings)
        if current == active and not self._dirty:
            return
        if self.path.is_symlink():
            raise ValueError("Archive drain state must not be a symbolic link")
        payload = json.dumps(
            {
                "version": 1,
                "identity": self._identity,
                "active": active,
            }
        ).encode("utf-8")
        descriptor, name = tempfile.mkstemp(
            prefix=".archive-drain-", dir=self.path.parent
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as writer:
                writer.write(payload)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(temporary, self.path)
            fsync_directory(self.path.parent)
        finally:
            temporary.unlink(missing_ok=True)
        self._active, self._dirty = active, False
