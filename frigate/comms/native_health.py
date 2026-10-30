"""Sanitized actual NVR health, with incomplete telemetry and explicit device opt-in."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, Protocol

import psutil
from peewee import DatabaseError
from pydantic import BaseModel, Field

from frigate.comms.apns_registry import APNsRegistry
from frigate.const import RECORD_DIR
from frigate.models import Recordings, User
from frigate.stats.diagnostics import mapping, metric

if TYPE_CHECKING:
    from frigate.comms.dispatcher import Dispatcher
    from frigate.config import FrigateConfig
    from frigate.stats.emitter import StatsEmitter
    from frigate.storage import StorageMaintainer

logger = logging.getLogger(__name__)


class NativeHealthContext(Protocol):
    frigate_config: FrigateConfig
    stats_emitter: StatsEmitter | None
    storage_maintainer: StorageMaintainer | None
    dispatcher: Dispatcher | None


class NativeHealthIssue(BaseModel):
    id: str
    kind: str
    camera: str | None
    severity: Literal["critical", "warning", "info"]
    title: str
    detail: str
    first_seen: float


class NativeHealthResponse(BaseModel):
    sampled_at: float
    checks_complete: bool
    issues: list[NativeHealthIssue] = Field(default_factory=list)


class NativeHealthMonitor:
    """Observe metadata only; zero idle detector FPS is never a failure signal."""

    def __init__(
        self, app: NativeHealthContext, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.app = app
        self.clock = clock
        self.latest = NativeHealthResponse(sampled_at=clock(), checks_complete=False)
        self.first_seen: dict[str, float] = {}
        self.last_sent: dict[tuple[str, str, str], float] = {}
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        self.started_at = clock()

    def start(self) -> None:
        """Start monitoring without enabling notification rules or device health opt-in."""
        from frigate.comms.webpush import WebPushClient

        if self.app.dispatcher is not None:
            for client in self.app.dispatcher.comms:
                if isinstance(client, WebPushClient):
                    client.native_health_validator = self.current_issue
        self.task = asyncio.create_task(self._run())

    def current_issue(
        self, identity: str, camera: str | None, first_seen: float
    ) -> bool:
        """A queued alert must still describe the same current measured condition."""
        return -5 <= self.clock() - self.latest.sampled_at <= 30 and any(
            issue.id == identity
            and issue.camera == camera
            and issue.first_seen == first_seen
            for issue in self.latest.issues
        )

    async def close(self) -> None:
        """Cancel only this monitor's background work."""
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def _recording_progress(self, config: FrigateConfig) -> dict[str, float | None]:
        progress = {}
        for name, camera in config.cameras.items():
            if not camera.enabled or not camera.record.enabled:
                continue
            for stream, days in (
                ("main", camera.record.continuous.days),
                (
                    "sub",
                    camera.record.sub.continuous.days
                    if camera.record.sub.enabled
                    else 0,
                ),
            ):
                if days <= 0:
                    continue
                row = (
                    Recordings.select(Recordings.end_time)
                    .where(Recordings.camera == name, Recordings.stream_type == stream)
                    .order_by(Recordings.start_time.desc())
                    .limit(1)
                    .first()
                )
                progress[f"{name}:{stream}"] = (
                    None if row is None else float(row.end_time)
                )
        return progress

    async def collect(self) -> NativeHealthResponse:
        """Report unknown measurements explicitly instead of declaring the NVR healthy."""
        async with self.lock:
            now = self.clock()
            config = self.app.frigate_config
            emitter = self.app.stats_emitter
            stats = mapping(emitter.get_latest_stats()) if emitter is not None else {}
            service = mapping(stats.get("service"))
            sampled = metric(service.get("last_updated"))
            fresh = sampled is not None and -5 <= now - sampled <= 30
            warmed = now - self.started_at >= 120
            complete = fresh and warmed
            issues: list[NativeHealthIssue] = []
            active: set[str] = set()

            def issue(
                kind: str,
                camera: str | None,
                severity: Literal["critical", "warning", "info"],
                title: str,
                detail: str,
            ) -> None:
                identity = (
                    "health-"
                    + hashlib.sha256(f"{kind}:{camera or ''}".encode()).hexdigest()[:32]
                )
                if identity in active:
                    return
                active.add(identity)
                first = self.first_seen.setdefault(identity, now)
                issues.append(
                    NativeHealthIssue(
                        id=identity,
                        kind=kind,
                        camera=camera,
                        severity=severity,
                        title=title,
                        detail=detail,
                        first_seen=first,
                    )
                )

            try:
                progress = await asyncio.to_thread(self._recording_progress, config)
            except (DatabaseError, OSError, ValueError) as error:
                logger.warning(
                    "Native recording health is unavailable (%s)", type(error).__name__
                )
                progress = {}
                complete = False
            if warmed:
                for key, end in progress.items():
                    camera, stream = key.rsplit(":", 1)
                    end = metric(end)
                    if end is None or end > now + 5:
                        complete = False
                        issue(
                            "recording_unavailable",
                            camera,
                            "warning",
                            "Recording progress unavailable",
                            "No completed recording is available for an enabled continuous stream.",
                        )
                    elif now - end > 90:
                        issue(
                            "recording_stalled",
                            camera,
                            "critical" if now - end > 300 else "warning",
                            "Recording progress has stopped",
                            f"The enabled {stream} recording stream has not produced a recent completed segment.",
                        )
            if fresh:
                storage = mapping(mapping(service.get("storage")).get(RECORD_DIR))
                free, total = metric(storage.get("free")), metric(storage.get("total"))
                if free is None or total is None or total <= 0 or free > total:
                    complete = False
                elif free / total < 0.02:
                    issue(
                        "local_storage_low",
                        None,
                        "critical",
                        "Recording storage is nearly full",
                        "Available space on the recording filesystem is below two percent.",
                    )
                processes = mapping(stats.get("processes"))
                for name in ("recording", "review_segment", "go2rtc"):
                    pid = metric(mapping(processes.get(name)).get("pid"))
                    if pid is None or not pid.is_integer() or pid > 2**31 - 1:
                        complete = False
                    elif warmed and (pid <= 0 or not psutil.pid_exists(int(pid))):
                        issue(
                            f"process_{name}",
                            None,
                            "critical",
                            "An NVR process is unavailable",
                            "A required recording or media process is not running.",
                        )
                detectors = mapping(stats.get("detectors"))
                expected_detectors = (
                    mapping(emitter.stats_tracking.get("detectors"))
                    if emitter is not None
                    else {}
                )
                if not expected_detectors:
                    complete = False
                for name in expected_detectors:
                    pid = metric(mapping(detectors.get(name)).get("pid"))
                    if pid is None or not pid.is_integer() or pid > 2**31 - 1:
                        complete = False
                    elif warmed and (pid <= 0 or not psutil.pid_exists(int(pid))):
                        issue(
                            "detector_unavailable",
                            None,
                            "warning",
                            "Object detection process is unavailable",
                            "A configured detector process is not running.",
                        )
                maintainer = self.app.storage_maintainer
                if config.storage.enabled:
                    health = mapping(
                        maintainer.get_archive_status()
                        if maintainer is not None
                        else {}
                    )
                    updated = metric(health.get("updated_at"))
                    if updated is None or not -5 <= now - updated <= 60:
                        complete = False
                    elif health.get("state") in ("blocked", "unresponsive"):
                        issue(
                            "archive_unavailable",
                            None,
                            "warning",
                            "Archive transfers are paused",
                            "The configured archive reports a blocked or unresponsive state.",
                        )
            self.first_seen = {
                key: first for key, first in self.first_seen.items() if key in active
            }
            self.latest = NativeHealthResponse(
                sampled_at=now, checks_complete=complete, issues=issues
            )
            return self.latest

    def scoped(self, user: dict) -> NativeHealthResponse:
        """Never leak another camera or global operator diagnostics to a restricted role."""
        config = self.app.frigate_config
        if user["role"] not in config.auth.roles:
            return NativeHealthResponse(
                sampled_at=self.latest.sampled_at, checks_complete=False
            )
        allowed = set(
            User.get_allowed_cameras(
                user["role"], config.auth.roles, set(config.cameras)
            )
        )
        return NativeHealthResponse(
            sampled_at=self.latest.sampled_at,
            checks_complete=self.latest.checks_complete,
            issues=[
                issue
                for issue in self.latest.issues
                if (issue.camera is None and user["role"] == "admin")
                or issue.camera in allowed
            ],
        )

    def _eligible(
        self, report: NativeHealthResponse, now: float
    ) -> list[tuple[str, str, NativeHealthIssue]]:
        config = self.app.frigate_config
        if not config.auth.enabled or not config.apns.enabled:
            return []
        registry = APNsRegistry(User._meta.database.execute_sql)
        eligible = []
        for account in User.select(User.username).iterator():
            result = registry.devices(account.username)
            if result is None or result[0] not in config.auth.roles:
                continue
            role, devices = result
            allowed = set(
                User.get_allowed_cameras(role, config.auth.roles, set(config.cameras))
            )
            for issue in report.issues:
                if now - issue.first_seen < 30:
                    continue
                if issue.camera is None and role != "admin":
                    continue
                if issue.camera is not None and issue.camera not in allowed:
                    continue
                for device in devices:
                    key = (account.username, device.installation_id, issue.id)
                    if (
                        "health-v1" not in device.features
                        or not device.preferences.health_alerts
                        or not device.preferences.allows(issue.camera, now)
                        or (
                            issue.camera is not None
                            and device.camera_ids is not None
                            and issue.camera not in device.camera_ids
                        )
                        or now - self.last_sent.get(key, 0) < 900
                    ):
                        continue
                    eligible.append((account.username, device.installation_id, issue))
        return eligible[:50]

    async def _run(self) -> None:
        from frigate.comms.webpush import WebPushClient

        while True:
            try:
                report = await self.collect()
                dispatcher = self.app.dispatcher
                clients = (
                    []
                    if dispatcher is None
                    else [
                        item
                        for item in dispatcher.comms
                        if isinstance(item, WebPushClient)
                    ]
                )
                if clients:
                    now = self.clock()
                    eligible = await asyncio.to_thread(self._eligible, report, now)
                    for username, installation, issue in eligible:
                        clients[0].send_push_notification(
                            user=username,
                            payload={
                                "identifier": issue.id,
                                "camera": issue.camera,
                                "health_first_seen": issue.first_seen,
                            },
                            title="Frigate Horizon",
                            message="NVR health information is available.",
                            notification_type="health",
                            ttl=3600,
                            installation_id=installation,
                        )
                        self.last_sent[(username, installation, issue.id)] = now
            except (DatabaseError, OSError, ValueError, TypeError) as error:
                logger.error(
                    "Native health monitoring failed (%s)", type(error).__name__
                )
                self.latest = NativeHealthResponse(
                    sampled_at=self.clock(), checks_complete=False
                )
            await asyncio.sleep(15)
