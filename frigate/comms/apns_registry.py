"""Atomic, account-owned native push registrations, independent of browser tokens."""

import json
import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from math import isfinite
from typing import Any, Literal, Protocol
from uuid import UUID

from frigate.comms.native_preferences import DevicePreferences, NativeDeviceFeature
from frigate.models import User


class _RedactNativeRegistrySQL(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if "native_push_devices" in record.getMessage():
            record.msg = "Native push registry SQL parameters omitted"
            record.args = ()
        return True


_database_logger = logging.getLogger("peewee")
if not any(
    isinstance(item, _RedactNativeRegistrySQL) for item in _database_logger.filters
):
    _database_logger.addFilter(_RedactNativeRegistrySQL())


class RegistryCursor(Protocol):
    """The cursor operations shared by SQLite and the queued database."""

    @property
    def rowcount(self) -> int: ...

    def fetchone(self) -> tuple[Any, ...] | None: ...


ExecuteSQL = Callable[[str, tuple[object, ...]], RegistryCursor]


@dataclass(frozen=True)
class NativePushDevice:
    """One installation's current token and explicitly selected camera scope."""

    installation_id: str
    token: str
    environment: Literal["production", "sandbox"]
    camera_ids: list[str] | None
    registered_at: float
    features: list[NativeDeviceFeature] = field(default_factory=list)
    preferences: DevicePreferences = field(default_factory=DevicePreferences)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.installation_id, str)
            or str(UUID(self.installation_id)) != self.installation_id
            or not isinstance(self.token, str)
            or not re.fullmatch(r"[0-9a-f]{2,512}", self.token)
            or len(self.token) % 2
            or self.environment not in ("production", "sandbox")
            or not isinstance(self.registered_at, (float, int))
            or isinstance(self.registered_at, bool)
            or not isfinite(self.registered_at)
            or self.registered_at < 0
            or not isinstance(self.features, list)
            or any(
                feature not in ("actions-v1", "health-v1") for feature in self.features
            )
            or len(set(self.features)) != len(self.features)
            or not isinstance(self.preferences, DevicePreferences)
            or (
                self.camera_ids is not None
                and (
                    not isinstance(self.camera_ids, list)
                    or not all(isinstance(camera, str) for camera in self.camera_ids)
                )
            )
        ):
            raise ValueError("Invalid native push registration")


class APNsRegistry:
    """Use one SQL statement per mutation, including installation ownership moves."""

    def __init__(self, execute: ExecuteSQL) -> None:
        self.execute = execute

    def register(self, username: str, device: NativePushDevice) -> bool:
        """Replace an installation atomically without overwriting browser subscriptions."""
        values = {**asdict(device), "preferences": device.preferences.model_dump()}
        payload = json.dumps(values, separators=(",", ":"))
        defaults = json.dumps(DevicePreferences().model_dump(), separators=(",", ":"))
        owned_payload = (
            "json_set(json(?), '$.preferences', json(COALESCE("
            "(SELECT json_extract(value, '$.preferences') "
            "FROM json_each(native_push_devices) "
            "WHERE json_extract(value, '$.installation_id') = ? LIMIT 1), ?)))"
        )
        filtered = (
            "(SELECT json_group_array(json(value)) "
            "FROM json_each(native_push_devices) "
            "WHERE json_extract(value, '$.installation_id') IS NOT ? "
            "AND json_extract(value, '$.token') IS NOT ?)"
        )
        cursor = self.execute(
            'UPDATE "user" SET native_push_devices = CASE WHEN username = ? '
            f"THEN json_insert({filtered}, '$[#]', {owned_payload}) "
            f"ELSE {filtered} END "
            'WHERE EXISTS (SELECT 1 FROM "user" AS owner WHERE owner.username = ?) '
            "AND (username = ? OR EXISTS (SELECT 1 FROM json_each(native_push_devices) "
            "WHERE json_extract(value, '$.installation_id') = ? "
            "OR json_extract(value, '$.token') = ?))",
            (
                username,
                device.installation_id,
                device.token,
                payload,
                device.installation_id,
                defaults,
                device.installation_id,
                device.token,
                username,
                username,
                device.installation_id,
                device.token,
            ),
        )
        return cursor.rowcount > 0

    def devices(self, username: str) -> tuple[str, list[NativePushDevice]] | None:
        """Read current role and devices together, so delivery can recheck permissions."""
        row = self.execute(
            'SELECT role, native_push_devices FROM "user" WHERE username = ?',
            (username,),
        ).fetchone()
        if row is None:
            return None
        values = json.loads(row[1])
        required = {
            "installation_id",
            "token",
            "environment",
            "camera_ids",
            "registered_at",
        }
        if not isinstance(values, list) or any(
            not isinstance(value, dict) or not required.issubset(value)
            for value in values
        ):
            raise ValueError("Invalid native push registration storage")
        devices = [
            NativePushDevice(
                installation_id=value["installation_id"],
                token=value["token"],
                environment=value["environment"],
                camera_ids=value["camera_ids"],
                registered_at=value["registered_at"],
                features=value.get("features", []),
                preferences=DevicePreferences.model_validate(
                    value.get("preferences", {})
                ),
            )
            for value in values
        ]
        return row[0], devices

    def set_preferences(
        self,
        username: str,
        role: str,
        installation_id: str,
        expected_revision: int,
        preferences: DevicePreferences,
        expected_registered_at: float | None = None,
    ) -> bool:
        """Compare and swap only the requesting owner's matching installation revision."""
        if preferences.revision != expected_revision + 1:
            raise ValueError("Preference revision must advance exactly once")
        payload = preferences.model_dump_json()
        cursor = self.execute(
            'UPDATE "user" SET native_push_devices = '
            "(SELECT json_group_array(json(CASE "
            "WHEN json_extract(value, '$.installation_id') = ? "
            "THEN json_set(value, '$.preferences', json(?)) ELSE value END)) "
            "FROM json_each(native_push_devices)) "
            "WHERE username = ? AND role = ? AND EXISTS ("
            "SELECT 1 FROM json_each(native_push_devices) "
            "WHERE json_extract(value, '$.installation_id') = ? "
            "AND COALESCE(json_extract(value, '$.preferences.revision'), 0) = ? "
            "AND (? IS NULL OR json_extract(value, '$.registered_at') = ?))",
            (
                installation_id,
                payload,
                username,
                role,
                installation_id,
                expected_revision,
                expected_registered_at,
                expected_registered_at,
            ),
        )
        return cursor.rowcount > 0

    def unregister(self, username: str, installation_id: str) -> bool:
        """Remove only the requesting account's registration, idempotently."""
        cursor = self.execute(
            'UPDATE "user" SET native_push_devices = '
            "(SELECT json_group_array(json(value)) FROM json_each(native_push_devices) "
            "WHERE json_extract(value, '$.installation_id') IS NOT ?) "
            "WHERE username = ?",
            (installation_id, username),
        )
        return cursor.rowcount > 0

    def authorized_devices(
        self,
        username: str,
        camera: str | None,
        roles: dict[str, list[str]],
        configured_cameras: set[str],
    ) -> list[NativePushDevice]:
        """Resolve current database roles at delivery, not only when queued."""
        result = self.devices(username)
        if result is None:
            return []
        role, devices = result
        if role not in roles:
            return []
        if camera is not None and camera not in User.get_allowed_cameras(
            role, roles, configured_cameras
        ):
            return []
        return devices

    def invalidate(
        self, username: str, device: NativePushDevice, invalidated_at: float
    ) -> None:
        """Do not erase a newer registration when APNs invalidates an older token."""
        self.execute(
            'UPDATE "user" SET native_push_devices = '
            "(SELECT json_group_array(json(value)) FROM json_each(native_push_devices) "
            "WHERE NOT (json_extract(value, '$.installation_id') = ? "
            "AND json_extract(value, '$.token') = ? "
            "AND json_extract(value, '$.environment') = ? "
            "AND json_extract(value, '$.registered_at') <= ? "
            "AND json_extract(value, '$.registered_at') = ?)) WHERE username = ?",
            (
                device.installation_id,
                device.token,
                device.environment,
                invalidated_at,
                device.registered_at,
                username,
            ),
        )
