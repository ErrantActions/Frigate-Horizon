"""Profile manager for activating/deactivating named config profiles."""

import copy
import json
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from frigate.config.camera.birdseye import birdseye_modes_to_mqtt_payload
from frigate.config.camera.profile import environment_profile_restart_fields
from frigate.config.camera.updater import (
    CameraConfigUpdateEnum,
    CameraConfigUpdatePublisher,
    CameraConfigUpdateTopic,
)
from frigate.config.camera.zone import ZoneConfig
from frigate.const import CONFIG_DIR
from frigate.util.builtin import deep_merge
from frigate.util.config import apply_section_update

if TYPE_CHECKING:
    from frigate.config import FrigateConfig

logger = logging.getLogger(__name__)

PROFILE_SECTION_UPDATES: dict[str, CameraConfigUpdateEnum] = {
    "audio": CameraConfigUpdateEnum.audio,
    "birdseye": CameraConfigUpdateEnum.birdseye,
    "detect": CameraConfigUpdateEnum.detect,
    "face_recognition": CameraConfigUpdateEnum.face_recognition,
    "lpr": CameraConfigUpdateEnum.lpr,
    "motion": CameraConfigUpdateEnum.motion,
    "notifications": CameraConfigUpdateEnum.notifications,
    "objects": CameraConfigUpdateEnum.objects,
    "record": CameraConfigUpdateEnum.record,
    "review": CameraConfigUpdateEnum.review,
    "snapshots": CameraConfigUpdateEnum.snapshots,
    "zones": CameraConfigUpdateEnum.zones,
}

# Retained MQTT switch topics per profile section, with a payload getter.
# Republished on profile change so MQTT/HA don't show a stale toggle.
SECTION_STATE_TOPICS: dict[str, list[tuple[str, Callable[[Any], Any]]]] = {
    "audio": [("audio", lambda c: "ON" if c.audio.enabled else "OFF")],
    "birdseye": [
        ("birdseye", lambda c: "ON" if c.birdseye.enabled else "OFF"),
        (
            "birdseye_modes",
            lambda c: (
                birdseye_modes_to_mqtt_payload(c.birdseye.modes)
                if c.birdseye.enabled
                else "OFF"
            ),
        ),
    ],
    "detect": [("detect", lambda c: "ON" if c.detect.enabled else "OFF")],
    "motion": [
        ("motion", lambda c: "ON" if c.motion.enabled else "OFF"),
        ("improve_contrast", lambda c: "ON" if c.motion.improve_contrast else "OFF"),
        ("motion_threshold", lambda c: c.motion.threshold),
        ("motion_contour_area", lambda c: c.motion.contour_area),
    ],
    "notifications": [
        ("notifications", lambda c: "ON" if c.notifications.enabled else "OFF"),
    ],
    "objects": [
        ("object_descriptions", lambda c: "ON" if c.objects.genai.enabled else "OFF"),
    ],
    "record": [("recordings", lambda c: "ON" if c.record.enabled else "OFF")],
    "review": [
        ("review_alerts", lambda c: "ON" if c.review.alerts.enabled else "OFF"),
        (
            "review_detections",
            lambda c: "ON" if c.review.detections.enabled else "OFF",
        ),
        (
            "review_descriptions",
            lambda c: "ON" if c.review.genai.enabled else "OFF",
        ),
    ],
    "snapshots": [("snapshots", lambda c: "ON" if c.snapshots.enabled else "OFF")],
}

PERSISTENCE_FILE = Path(CONFIG_DIR) / ".profiles"
ENVIRONMENT_POWER_SWITCHES = (
    "enabled",
    "detect.enabled",
    "motion.enabled",
    "audio.enabled",
    "record.enabled",
    "snapshots.enabled",
    "notifications.enabled",
    "review.alerts.enabled",
    "review.detections.enabled",
)


class ProfileManager:
    """Manages profile activation, persistence, and config application."""

    def __init__(
        self,
        config,
        config_updater: CameraConfigUpdatePublisher,
        dispatcher=None,
    ):
        from frigate.config.config import FrigateConfig

        self.config: FrigateConfig = config
        self.config_updater = config_updater
        self.dispatcher = dispatcher
        self._lock = threading.RLock()
        self._camera_profiles: dict[str, tuple[str, ...]] = {}
        self._camera_constraints: dict[str, tuple[str, ...]] = {}
        self._generation = 0
        self._camera_generations: dict[str, int] = {}
        self._base_configs: dict[str, dict[str, dict]] = {}
        self._base_api_configs: dict[str, dict[str, dict]] = {}
        self._base_enabled: dict[str, bool] = {}
        self._base_zones: dict[str, dict[str, ZoneConfig]] = {}
        self._snapshot_base_configs()

    def _snapshot_base_configs(self) -> None:
        """Snapshot each camera's current section configs, enabled, and zones."""
        for cam_name, cam_config in tuple(self.config.cameras.items()):
            self._base_configs[cam_name] = {}
            self._base_api_configs[cam_name] = {}
            self._base_enabled[cam_name] = cam_config.enabled
            self._base_zones[cam_name] = copy.deepcopy(cam_config.zones)
            for section in PROFILE_SECTION_UPDATES:
                section_value = getattr(cam_config, section, None)
                if section_value is None:
                    continue

                if section == "zones":
                    # zones is a dict of ZoneConfig models
                    self._base_configs[cam_name][section] = {
                        name: zone.model_dump() for name, zone in section_value.items()
                    }
                    self._base_api_configs[cam_name][section] = {
                        name: {
                            **zone.model_dump(
                                mode="json",
                                warnings="none",
                                exclude_none=True,
                            ),
                            "color": zone.color,
                        }
                        for name, zone in section_value.items()
                    }
                else:
                    self._base_configs[cam_name][section] = section_value.model_dump()
                    self._base_api_configs[cam_name][section] = (
                        section_value.model_dump(
                            mode="json",
                            warnings="none",
                            exclude_none=True,
                        )
                    )

    def update_config(self, new_config) -> None:
        """Rebase profile layers atomically when the parsed configuration changes."""
        with self._lock:
            self._update_config(new_config)

    def _update_config(self, new_config) -> None:
        """Update config reference after config/set replaces the in-memory config.

        Preserves active profile state: re-snapshots base configs from the new
        (freshly parsed) config, then re-applies profile overrides if a profile
        was active.

        Deliberately does not clear the dispatcher's runtime overrides. This is
        the config-save path, not a profile switch: the save only invalidates
        the toggles it rewrote in yaml, which /api/config/set already clears by
        key. The broad wipe belongs to activate_profile alone.
        """
        current_active = self.config.active_profile
        self.config = new_config

        # Re-snapshot base configs from the new config (which has base values)
        self._base_configs.clear()
        self._base_api_configs.clear()
        self._base_enabled.clear()
        self._base_zones.clear()
        self._snapshot_base_configs()
        self._generation += 1
        self._camera_generations.clear()
        for camera_name, profiles in list(self._camera_profiles.items()):
            camera = self.config.cameras.get(camera_name)
            if camera is None or not camera.environment.enabled:
                self._camera_profiles.pop(camera_name, None)
                self._camera_constraints.pop(camera_name, None)
                continue
            valid = tuple(name for name in profiles if name in camera.profiles)
            if valid != profiles:
                logger.warning(
                    "Removed unavailable environmental profile references for camera %s",
                    camera_name,
                )
                self._camera_profiles[camera_name] = valid

        # Re-apply profile overrides without publishing ZMQ updates
        # (the config/set caller handles its own ZMQ publishing)
        if current_active is not None:
            if current_active in self.config.profiles:
                self.config.active_profile = current_active
            else:
                # Profile was deleted — deactivate
                self.config.active_profile = None
                self._persist_active_profile(None)
        else:
            self.config.active_profile = None
        _, error = self._apply_to_config(self.config.active_profile)
        if error:
            logger.error("Unable to reapply configuration profiles: %s", error)

    def _validate_profile_name(self, profile_name: str | None) -> str | None:
        """Return an error message if the name is not a defined profile."""
        if profile_name is not None and profile_name not in self.config.profiles:
            return f"Profile '{profile_name}' is not defined in the profiles section"

        return None

    def _apply_to_config(
        self, profile_name: str | None, camera_name: str | None = None
    ) -> tuple[dict[str, set[str]], str | None]:
        """Reset every camera to base, then apply the named profile on top.

        Returns the changed camera/section pairs, plus an error message if
        applying the profile failed partway through.
        """
        changed: dict[str, set[str]] = {}

        self._reset_to_base(changed, camera_name)

        if profile_name is not None:
            err = self._apply_profile_overrides(
                profile_name, changed, camera_name=camera_name
            )
            if err:
                return changed, err

        for name, profiles in self._camera_profiles.items():
            if camera_name is not None and name != camera_name:
                continue
            camera = self.config.cameras.get(name)
            if camera is None:
                continue
            disabled = {
                path
                for path in ENVIRONMENT_POWER_SWITCHES
                if camera.get_nested_object(path.replace(".", "/")) is False
            }
            for profile in profiles:
                err = self._apply_profile_overrides(
                    profile, changed, camera_name=name, merge_current=True
                )
                if err:
                    return changed, err
            constrained: list[str] = []
            for path in disabled:
                if camera.get_nested_object(path.replace(".", "/")) is not True:
                    continue
                parts = path.split(".")
                target = camera
                for part in parts[:-1]:
                    target = getattr(target, part)
                setattr(target, parts[-1], False)
                changed.setdefault(name, set()).add(parts[0])
                constrained.append(path)
            self._camera_constraints[name] = tuple(sorted(constrained))

        return changed, None

    def apply_profile_to_config(self, profile_name: str | None) -> str | None:
        """Apply profile layers to in-process configuration without publication."""
        with self._lock:
            return self._apply_profile_to_config(profile_name)

    def _apply_profile_to_config(self, profile_name: str | None) -> str | None:
        """Apply a profile to the in-memory config, without publishing it.

        Safe to call ahead of activate_profile: both reset to the base config
        first, so the later call re-derives the same state and still reports
        every section as changed.

        Returns:
            None on success, or an error message string on failure.
        """
        err = self._validate_profile_name(profile_name)

        if err:
            return err

        return self._apply_to_config(profile_name)[1]

    def _persisted_profile_to_restore(self) -> str | None:
        """Return the persisted profile name, if it still applies to a camera."""
        persisted = self.load_persisted_profile()

        if not persisted or not any(
            persisted in cam.profiles for cam in self.config.cameras.values()
        ):
            return None

        return persisted

    def restore_persisted_profile_to_config(self) -> None:
        """Restore the persisted profile into the config, without publishing.

        Called before worker processes start, so they are handed a config that
        already carries the profile rather than relying on the broadcast that
        restore_persisted_profile() sends later.
        """
        persisted = self._persisted_profile_to_restore()

        if persisted is None:
            return

        err = self.apply_profile_to_config(persisted)

        if err:
            logger.error("Failed to apply persisted profile '%s': %s", persisted, err)

    def restore_persisted_profile(self) -> None:
        """Re-activate the persisted profile once subscribers are connected.

        The config already carries the profile; this pass publishes it for the
        processes that start before the config can be corrected, and for the
        retained MQTT states.
        """
        persisted = self._persisted_profile_to_restore()

        if persisted is None:
            return

        logger.info("Restoring persisted profile '%s'", persisted)
        # runtime overrides are layered on top by the dispatcher's replay
        self.activate_profile(persisted, clear_runtime_overrides=False)

    def activate_profile(
        self,
        profile_name: str | None,
        clear_runtime_overrides: bool = True,
    ) -> str | None:
        """Atomically activate a global profile while preserving camera-specific layers."""
        with self._lock:
            return self._activate_profile(profile_name, clear_runtime_overrides)

    def _activate_profile(
        self,
        profile_name: str | None,
        clear_runtime_overrides: bool = True,
    ) -> str | None:
        """Activate a profile by name, or deactivate if None.

        Args:
            profile_name: Profile name to activate, or None to deactivate.
            clear_runtime_overrides: When True (the default, for user-initiated
                activations) drop the dispatcher's runtime override file because
                the layer below changed. Startup callers that are replaying a
                persisted profile pass False so the runtime state stays
                available for the subsequent replay step.

        Returns:
            None on success, or an error message string on failure.
        """
        err = self._validate_profile_name(profile_name)

        if err:
            return err

        # Track which camera/section pairs get changed for ZMQ publishing
        changed, err = self._apply_to_config(profile_name)

        if err:
            return err

        # Publish ZMQ updates only for sections that actually changed
        self._publish_updates(changed)

        self.config.active_profile = profile_name
        self._persist_active_profile(profile_name)

        # a profile switch invalidates the steady-state runtime overrides
        if clear_runtime_overrides and self.dispatcher is not None:
            self.dispatcher.clear_runtime_state()

        logger.info(
            "Profile %s",
            f"'{profile_name}' activated" if profile_name else "deactivated",
        )
        return None

    def _reset_to_base(
        self, changed: dict[str, set[str]], camera_name: str | None = None
    ) -> None:
        """Reset all cameras to their base (no-profile) config."""
        for cam_name, cam_config in tuple(self.config.cameras.items()):
            if camera_name is not None and cam_name != camera_name:
                continue
            # Restore enabled state
            base_enabled = self._base_enabled.get(cam_name)
            if base_enabled is not None and cam_config.enabled != base_enabled:
                cam_config.enabled = base_enabled
                changed.setdefault(cam_name, set()).add("enabled")

            # Restore zones (always restore from snapshot; direct Pydantic
            # comparison fails when ZoneConfig contains numpy arrays)
            base_zones = self._base_zones.get(cam_name)
            if base_zones is not None:
                cam_config.zones = copy.deepcopy(base_zones)
                changed.setdefault(cam_name, set()).add("zones")

            # Restore section configs (zones handled above)
            base = self._base_configs.get(cam_name, {})
            for section in PROFILE_SECTION_UPDATES:
                if section == "zones":
                    continue
                base_data = base.get(section)
                if base_data is None:
                    continue
                err = apply_section_update(cam_config, section, base_data)
                if err:
                    logger.error(
                        "Failed to reset section '%s' on camera '%s': %s",
                        section,
                        cam_name,
                        err,
                    )
                else:
                    changed.setdefault(cam_name, set()).add(section)

    def _apply_profile_overrides(
        self,
        profile_name: str,
        changed: dict[str, set[str]],
        camera_name: str | None = None,
        merge_current: bool = False,
    ) -> str | None:
        """Apply profile overrides for all cameras that have the named profile."""
        for cam_name, cam_config in tuple(self.config.cameras.items()):
            if camera_name is not None and cam_name != camera_name:
                continue
            profile = cam_config.profiles.get(profile_name)
            if profile is None:
                continue

            # Apply enabled override
            if profile.enabled is not None and cam_config.enabled != profile.enabled:
                cam_config.enabled = profile.enabled
                changed.setdefault(cam_name, set()).add("enabled")

            # Apply zones override — merge profile zones into base zones
            if profile.zones is not None:
                base_zones = (
                    cam_config.zones
                    if merge_current
                    else self._base_zones.get(cam_name, {})
                )
                merged_zones = copy.deepcopy(base_zones)
                merged_zones.update(profile.zones)
                # Profile zone objects are parsed without colors or contours
                # (those are set during CameraConfig init / post-validation).
                # Inherit the base zone's color when available, and ensure
                # every zone has a valid contour for rendering.
                for name, zone in merged_zones.items():
                    if zone.contour.size == 0:
                        zone.generate_contour(cam_config.frame_shape)
                    if zone.color == (0, 0, 0) and name in base_zones:
                        zone._color = base_zones[name].color
                cam_config.zones = merged_zones
                changed.setdefault(cam_name, set()).add("zones")

            base = self._base_configs.get(cam_name, {})

            for section in PROFILE_SECTION_UPDATES:
                if section == "zones":
                    continue
                profile_section = getattr(profile, section, None)
                if profile_section is None:
                    continue

                overrides = profile_section.model_dump(exclude_unset=True)
                if not overrides:
                    continue

                base_data = (
                    getattr(cam_config, section).model_dump()
                    if merge_current
                    else base.get(section, {})
                )
                merged = deep_merge(overrides, base_data)

                err = apply_section_update(cam_config, section, merged)
                if err:
                    return f"Failed to apply profile '{profile_name}' section '{section}' on camera '{cam_name}': {err}"

                changed.setdefault(cam_name, set()).add(section)

        return None

    def apply_camera_profiles(
        self,
        camera_name: str,
        profiles: tuple[str, ...],
        *,
        expected_config: "FrigateConfig | None" = None,
    ) -> str | None:
        """Apply only one camera's environmental layers, preserving explicit off switches."""
        with self._lock:
            if expected_config is not None and self.config is not expected_config:
                return "Configuration changed while selecting environmental profiles"
            camera = self.config.cameras.get(camera_name)
            if camera is None:
                logger.warning("Environmental profile references an unknown camera")
                return "Camera is not configured"
            if profiles and not camera.environment.enabled:
                logger.warning(
                    "Environmental profile automation is disabled for camera %s",
                    camera_name,
                )
                return "Environmental profiles are disabled for this camera"
            for name in profiles:
                profile = camera.profiles.get(name)
                if profile is None:
                    logger.warning(
                        "Environmental profile is undefined for camera %s", camera_name
                    )
                    return "Profile is not defined for this camera"
                unsupported = environment_profile_restart_fields(profile)
                if unsupported:
                    logger.warning(
                        "Environmental profile for camera %s requires unsupported live updates",
                        camera_name,
                    )
                    return f"Profile requires a restart or global update: {sorted(unsupported)}"
            if (
                self._camera_profiles.get(camera_name, ()) == profiles
                and self._camera_generations.get(camera_name) == self._generation
            ):
                return None
            previous = self._camera_profiles.get(camera_name, ())
            self._camera_profiles[camera_name] = profiles
            changed, error = self._apply_to_config(
                self.config.active_profile, camera_name=camera_name
            )
            if error:
                self._camera_profiles[camera_name] = previous
                _, rollback_error = self._apply_to_config(
                    self.config.active_profile, camera_name=camera_name
                )
                if rollback_error:
                    logger.error(
                        "Unable to restore camera profile after failure: %s",
                        rollback_error,
                    )
                logger.error("Unable to apply camera environmental profile: %s", error)
                return error
            if self.dispatcher is not None:
                self.dispatcher.reapply_runtime_state_to_config()
            self._camera_constraints[camera_name] = tuple(
                path
                for path in self._camera_constraints.get(camera_name, ())
                if camera.get_nested_object(path.replace(".", "/")) is False
            )
            self._publish_updates(changed)
            self._camera_generations[camera_name] = self._generation
            return None

    def has_camera_profiles(self, camera_name: str) -> bool:
        """Indicate whether settings must expose the camera's original base values."""
        with self._lock:
            return bool(self._camera_profiles.get(camera_name))

    def get_camera_profile_info(self, camera_name: str) -> dict[str, list[str]]:
        """Report applied NVR layers and explicitly preserved disabled services."""
        with self._lock:
            return {
                "profiles": list(self._camera_profiles.get(camera_name, ())),
                "blocked_fields": list(self._camera_constraints.get(camera_name, ())),
            }

    def _publish_updates(self, changed: dict[str, set[str]]) -> None:
        """Publish ZMQ config updates only for sections that changed."""
        for cam_name, sections in changed.items():
            cam_config = self.config.cameras.get(cam_name)
            if cam_config is None:
                continue

            for section in sections:
                if section == "enabled":
                    self.config_updater.publish_update(
                        CameraConfigUpdateTopic(
                            CameraConfigUpdateEnum.enabled, cam_name
                        ),
                        cam_config.enabled,
                    )
                    if self.dispatcher is not None:
                        self.dispatcher.publish(
                            f"{cam_name}/enabled/state",
                            "ON" if cam_config.enabled else "OFF",
                            retain=True,
                        )
                    continue

                if section == "zones":
                    self.config_updater.publish_update(
                        CameraConfigUpdateTopic(CameraConfigUpdateEnum.zones, cam_name),
                        cam_config.zones,
                    )
                    continue

                update_enum = PROFILE_SECTION_UPDATES.get(section)
                if update_enum is None:
                    continue
                settings = getattr(cam_config, section, None)
                if settings is not None:
                    self.config_updater.publish_update(
                        CameraConfigUpdateTopic(update_enum, cam_name),
                        settings,
                    )

                # republish MQTT switch states
                if self.dispatcher is not None:
                    for suffix, get_payload in SECTION_STATE_TOPICS.get(section, ()):
                        self.dispatcher.publish(
                            f"{cam_name}/{suffix}/state",
                            get_payload(cam_config),
                            retain=True,
                        )

    def _persist_active_profile(self, profile_name: str | None) -> None:
        """Persist the active profile state to disk as JSON."""
        try:
            data = self._load_persisted_data()
            data["active"] = profile_name
            if profile_name is not None:
                data.setdefault("last_activated", {})[profile_name] = datetime.now(
                    UTC
                ).timestamp()
            PERSISTENCE_FILE.write_text(json.dumps(data))
        except OSError:
            logger.exception("Failed to persist active profile")

    @staticmethod
    def _load_persisted_data() -> dict:
        """Load the full persisted profile data from disk."""
        try:
            if PERSISTENCE_FILE.exists():
                raw = PERSISTENCE_FILE.read_text().strip()
                if raw:
                    return json.loads(raw)
        except (OSError, json.JSONDecodeError):
            logger.exception("Failed to load persisted profile data")
        return {"active": None, "last_activated": {}}

    @staticmethod
    def load_persisted_profile() -> str | None:
        """Load the persisted active profile name from disk."""
        data = ProfileManager._load_persisted_data()
        name = data.get("active")
        return name if name else None

    def get_base_configs_for_api(self, camera_name: str) -> dict[str, dict]:
        """Return base (pre-profile) section configs for a camera.

        These are JSON-serializable dicts suitable for direct inclusion in
        the /api/config response, with None values already excluded.
        """
        with self._lock:
            return copy.deepcopy(self._base_api_configs.get(camera_name, {}))

    def get_available_profiles(self) -> list[dict[str, str]]:
        """Get list of all profile definitions from the top-level config."""
        return [
            {"name": name, "friendly_name": defn.friendly_name}
            for name, defn in sorted(self.config.profiles.items())
        ]

    def get_profile_info(self) -> dict:
        """Get profile state info for API responses."""
        data = self._load_persisted_data()
        return {
            "profiles": self.get_available_profiles(),
            "active_profile": self.config.active_profile,
            "last_activated": data.get("last_activated", {}),
        }
