"""One durable bounded compression worker, with explicit controls and UTC windows."""

import asyncio
import json
import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from filelock import FileLock
from peewee import DatabaseError, SqliteDatabase

from frigate.config.compression import StorageCompressionConfig
from frigate.const import CACHE_DIR, CONFIG_DIR, RECORD_DIR
from frigate.models import Recordings
from frigate.record.audio_repair import (
    DEFAULT_FFMPEG,
    DEFAULT_FFPROBE,
    RepairCancelled,
    RepairError,
    _plain_path,
)
from frigate.record.compression import RecordingCompression
from frigate.record.compression_storage import reservation_schema_ready
from frigate.record.lease_reader import RecordingLeaseLost
from frigate.record.policy import CapturePadding

logger = logging.getLogger(__name__)
ACTIVE = ("running", "pause_requested", "paused", "stop_requested")


class CompressionConflict(RuntimeError):
    """Compression is disabled, occupied, or unsafe to activate."""


class CompressionNotFound(RuntimeError):
    """The requested persistent job does not exist."""


def weekly_window(
    policy: StorageCompressionConfig, now: float
) -> tuple[str, float] | None:
    """Return the UTC window identity and end, including cross-midnight windows."""
    if not policy.window.enabled:
        return None
    current = datetime.fromtimestamp(now, UTC)
    start = current.replace(
        hour=policy.window.start_hour, minute=0, second=0, microsecond=0
    )
    start -= timedelta(days=(start.weekday() - policy.window.weekday) % 7)
    if start > current:
        start -= timedelta(days=7)
    end = start + timedelta(minutes=policy.window.duration_minutes)
    return (start.isoformat(), end.timestamp()) if start <= current < end else None


class CompressionManager:
    """Never resume interrupted jobs or recompress a completed recording implicitly."""

    def __init__(
        self,
        config_provider: Callable,
        *,
        root: Path = Path(CONFIG_DIR) / "compression",
        recordings_root: Path = Path(RECORD_DIR),
        cache_root: Path = Path(CACHE_DIR),
        readers_leased: bool = False,
        ffmpeg: str | None = None,
        ffprobe: str | None = None,
    ) -> None:
        self.config_provider = config_provider
        self.root, self.recordings, self.cache = root, recordings_root, cache_root
        self.readers_leased = readers_leased
        self.ffmpeg, self.ffprobe = ffmpeg, ffprobe
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="compression"
        )
        self._future = None
        self._scheduler = None
        self._ready = False
        self._closing = False
        self.recovery_required = False
        self.reservations_ready = False

    def policy(self) -> StorageCompressionConfig:
        """Read live controls, defaulting to disabled before parent configuration wiring."""
        value = getattr(self.config_provider().storage, "compression", None)
        return StorageCompressionConfig.model_validate(value or {})

    @contextmanager
    def _state(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.root / "jobs.sqlite"), timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    async def start(self) -> None:
        """Reconcile journals before accepting jobs; no interrupted work is resumed."""
        await asyncio.to_thread(self._initialize)
        self._scheduler = asyncio.create_task(self._schedule())

    def _database(self) -> SqliteDatabase:
        return SqliteDatabase(
            self.config_provider().database.path,
            timeout=5,
            pragmas={"synchronous": "FULL"},
        )

    def _initialize(self) -> None:
        for path in (self.root, self.root / "jobs.sqlite", self.root / "backups"):
            _plain_path(path)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (
            FileLock(str(self.root / "control.lock"), timeout=5),
            FileLock(str(self.root / "worker.lock"), timeout=0),
        ):
            self._initialize_locked()

    def _initialize_locked(self) -> None:
        with self._state() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL, policy TEXT NOT NULL, scheduled INTEGER NOT NULL,
                    window_end REAL, runtime REAL NOT NULL DEFAULT 0,
                    current_item_id TEXT, reason TEXT
                );
                CREATE TABLE IF NOT EXISTS items(
                    job_id TEXT NOT NULL, id TEXT NOT NULL, camera TEXT NOT NULL,
                    start_time REAL NOT NULL, end_time REAL NOT NULL,
                    state TEXT NOT NULL, reason TEXT, saved_bytes INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(job_id,id)
                );
                CREATE TABLE IF NOT EXISTS completed(
                    id TEXT PRIMARY KEY, committed_at REAL NOT NULL, journal TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS windows(id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS cursor(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    start_time REAL NOT NULL, id TEXT NOT NULL
                );
            """)
            db.execute(
                "UPDATE jobs SET status='interrupted',current_item_id=NULL,reason='restart-requires-new-batch' "
                "WHERE status IN ('running','pause_requested','paused','stop_requested')"
            )
        database = self._database()
        try:
            database.connect()
            self.reservations_ready = reservation_schema_ready(database)
            for journal in sorted((self.root / "backups").glob("*.json")):
                try:
                    data = json.loads(journal.read_text(encoding="utf-8"))
                    if data.get("kind") != "compression":
                        raise RepairError("Unexpected recovery journal")
                    if data["state"] in ("committed", "cleaned"):
                        self._complete(data["row"]["id"], journal)
                    if data["state"] == "committed" and data.get("version") == 2:
                        engine = self._engine(
                            database, data["row"]["camera"], self.policy()
                        )
                        engine.recover(journal)
                        self._complete(data["row"]["id"], journal)
                    elif data["state"] not in (
                        "cleaned",
                        "rolled-back-cleaned",
                        "committed",
                    ) and not (
                        data.get("version") == 1 and data["state"] == "rolled-back"
                    ):
                        engine = self._engine(
                            database, data["row"]["camera"], self.policy()
                        )
                        engine.recover(journal)
                except (
                    RepairError,
                    OSError,
                    ValueError,
                    KeyError,
                    DatabaseError,
                    CompressionConflict,
                    TypeError,
                    AttributeError,
                ):
                    self.recovery_required = True
                    logger.warning(
                        "Compression recovery requires operator attention; originals retained"
                    )
        finally:
            database.close()
        self._ready = True

    async def close(self) -> None:
        """Kill owned subprocesses before waiting for the safe worker boundary."""
        self._closing = True
        self._cancel.set()
        if self._scheduler:
            self._scheduler.cancel()
            try:
                await self._scheduler
            except asyncio.CancelledError:
                pass
        if self._future:
            await asyncio.wrap_future(self._future)
        self._executor.shutdown(wait=True)

    def _complete(self, recording_id: str, journal: Path) -> None:
        with self._state() as db:
            db.execute(
                "INSERT OR IGNORE INTO completed VALUES(?,?,?)",
                (recording_id, time.time(), journal.name),
            )
            data = json.loads(journal.read_text(encoding="utf-8"))
            if data.get("job_id") and data["state"] == "cleaned":
                row = data["row"]
                db.execute(
                    "INSERT INTO items VALUES(?,?,?,?,?,'compressed',NULL,?) "
                    "ON CONFLICT(job_id,id) DO UPDATE SET state='compressed',"
                    "reason=NULL,saved_bytes=excluded.saved_bytes",
                    (
                        data["job_id"],
                        row["id"],
                        row["camera"],
                        row["start_time"],
                        row["end_time"],
                        data["actual_saved_bytes"],
                    ),
                )
                db.execute(
                    "UPDATE jobs SET updated_at=? WHERE id=?",
                    (time.time(), data["job_id"]),
                )
                db.execute(
                    "UPDATE jobs SET status='interrupted',reason='recovery-completed-new-batch-required' "
                    "WHERE id=? AND status='failed' AND reason='recovery-required'",
                    (data["job_id"],),
                )

    def _completed(self, recording_id: str) -> bool:
        with self._state() as db:
            return (
                db.execute(
                    "SELECT 1 FROM completed WHERE id=?", (recording_id,)
                ).fetchone()
                is not None
            )

    def _engine(self, database, camera: str, policy: StorageCompressionConfig):
        config = self.config_provider()
        camera_config = config.cameras.get(camera)
        if camera_config is None:
            raise CompressionConflict("camera-not-configured")
        ffmpeg_config = camera_config.ffmpeg
        return RecordingCompression(
            database,
            self.recordings,
            self.cache,
            self.root / "backups",
            self.ffmpeg or getattr(ffmpeg_config, "ffmpeg_path", DEFAULT_FFMPEG),
            self.ffprobe or getattr(ffmpeg_config, "ffprobe_path", DEFAULT_FFPROBE),
            timeout=120,
            online=True,
            cancel=self._cancel,
            padding=CapturePadding.from_record(camera_config.record),
            padding_provider=lambda: CapturePadding.from_record(
                self.config_provider().cameras[camera].record
            ),
            policy=policy,
            readers_leased=self.readers_leased,
            completed=self._completed,
            archive_settings_provider=lambda: self.config_provider().storage,
        )

    def _set(self, job_id: str, **values) -> None:
        values["updated_at"] = time.time()
        with self._state() as db:
            db.execute(
                f"UPDATE jobs SET {','.join(f'{key}=?' for key in values)} WHERE id=?",
                (*values.values(), job_id),
            )

    def get_job(self, job_id: str) -> dict:
        """Return persistent counters, never tool output or filesystem paths."""
        with self._state() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise CompressionNotFound("Compression job not found")
            policy = StorageCompressionConfig.model_validate_json(job["policy"])
            counts = db.execute(
                """SELECT COUNT(*) processed,SUM(state='compressed') compressed,
                SUM(state='skipped') skipped,SUM(state='failed') failed,
                SUM(saved_bytes) saved_bytes FROM items WHERE job_id=?""",
                (job_id,),
            ).fetchone()
            result = {
                key: job[key]
                for key in (
                    "id",
                    "status",
                    "created_at",
                    "updated_at",
                    "current_item_id",
                    "reason",
                )
            }
            result.update({key: counts[key] or 0 for key in counts.keys()})
            result.update(
                scheduled=bool(job["scheduled"]),
                max_files=policy.max_files,
                target_mbps=policy.target_mbps,
                min_age_days=policy.min_age_days,
                encoder=policy.encoder,
                video_codec=policy.video_codec,
                cpu_threads=policy.cpu_threads,
                svt_preset=policy.svt_preset,
                frame_rate=policy.frame_rate,
            )
            return result

    def list_jobs(self) -> dict:
        """Expose activation gates alongside the most recent twenty jobs."""
        with self._state() as db:
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM jobs ORDER BY created_at DESC LIMIT 20"
                )
            ]
        return {
            "jobs": [self.get_job(job_id) for job_id in ids],
            "enabled": self.policy().enabled,
            "readers_leased": self.readers_leased,
            "recovery_required": self.recovery_required,
            "archive_supported": True,
            "reservations_ready": self.reservations_ready,
        }

    def list_items(self, job_id: str, limit: int = 50, offset: int = 0) -> dict:
        """Read a bounded audit page."""
        self.get_job(job_id)
        with self._state() as db:
            items = db.execute(
                "SELECT id,camera,start_time,end_time,state,reason,saved_bytes FROM items "
                "WHERE job_id=? ORDER BY start_time,id LIMIT ? OFFSET ?",
                (job_id, min(max(limit, 1), 100), max(offset, 0)),
            ).fetchall()
            total = db.execute(
                "SELECT COUNT(*) FROM items WHERE job_id=?", (job_id,)
            ).fetchone()[0]
        return {"items": [dict(item) for item in items], "total": total}

    def create_job(self, *, window: tuple[str, float] | None = None) -> dict:
        """Snapshot policy only after explicit user confirmation or an opted-in window."""
        with self._lock, FileLock(str(self.root / "control.lock"), timeout=5):
            policy = self.policy()
            if not self._ready or self._closing or not policy.enabled:
                raise CompressionConflict("compression-disabled")
            if not self.readers_leased:
                raise CompressionConflict("reader-leases-not-certified")
            if not self.reservations_ready:
                raise CompressionConflict("reservation-migration-required")
            if self.recovery_required:
                raise CompressionConflict("recovery-required")
            if self._future and not self._future.done():
                raise CompressionConflict("compression-busy")
            now, job_id = time.time(), uuid.uuid4().hex
            with self._state() as db:
                if db.execute(
                    "SELECT 1 FROM jobs WHERE status IN ('running','pause_requested','paused','stop_requested')"
                ).fetchone():
                    raise CompressionConflict("compression-busy")
                if window:
                    if db.execute(
                        "SELECT 1 FROM windows WHERE id=?", (window[0],)
                    ).fetchone():
                        raise CompressionConflict("window-already-used")
                    db.execute("INSERT INTO windows VALUES(?)", (window[0],))
                db.execute(
                    "INSERT INTO jobs(id,status,created_at,updated_at,policy,scheduled,window_end) "
                    "VALUES(?,'running',?,?,?,?,?)",
                    (
                        job_id,
                        now,
                        now,
                        policy.model_dump_json(),
                        int(window is not None),
                        window[1] if window else None,
                    ),
                )
            self._cancel.clear()
            self._future = self._executor.submit(self._worker, job_id)
            return self.get_job(job_id)

    def control(self, job_id: str, action: str) -> dict:
        """Pause at a file boundary; stop cancels before publication; resume is explicit."""
        with self._lock, FileLock(str(self.root / "control.lock"), timeout=5):
            job = self.get_job(job_id)
            if action == "resume":
                if job["status"] != "paused" or (
                    self._future and not self._future.done()
                ):
                    raise CompressionConflict("job-not-paused")
                if (
                    not self.policy().enabled
                    or self.recovery_required
                    or not self.readers_leased
                    or not self.reservations_ready
                ):
                    raise CompressionConflict("compression-unavailable")
                with self._state() as db:
                    stored = db.execute(
                        "SELECT * FROM jobs WHERE id=?", (job_id,)
                    ).fetchone()
                policy = StorageCompressionConfig.model_validate_json(stored["policy"])
                if stored["runtime"] >= policy.max_batch_minutes * 60:
                    raise CompressionConflict("batch-runtime-exhausted")
                if stored["scheduled"] and time.time() >= stored["window_end"]:
                    raise CompressionConflict("maintenance-window-ended")
                self._cancel.clear()
                self._set(job_id, status="running", reason=None)
                self._future = self._executor.submit(self._worker, job_id)
            elif action == "pause" and job["status"] == "running":
                self._set(job_id, status="pause_requested")
            elif action == "stop" and job["status"] in ACTIVE:
                self._cancel.set()
                self._set(
                    job_id,
                    status="stopped" if job["status"] == "paused" else "stop_requested",
                )
            else:
                raise CompressionConflict("invalid-job-control")
            return self.get_job(job_id)

    async def _schedule(self) -> None:
        while True:
            await asyncio.sleep(15)
            try:
                policy = self.policy()
                window = weekly_window(policy, time.time())
                if policy.enabled and window:
                    await asyncio.to_thread(self.create_job, window=window)
            except CompressionConflict:
                pass
            except (OSError, sqlite3.Error, ValueError):
                logger.warning("Compression scheduling unavailable")

    def _worker(self, job_id: str) -> None:
        database = self._database()
        started = time.monotonic()
        try:
            with FileLock(str(self.root / "worker.lock"), timeout=0):
                database.connect()
                with self._state() as db:
                    job = db.execute(
                        "SELECT * FROM jobs WHERE id=?", (job_id,)
                    ).fetchone()
                    cursor = db.execute(
                        "SELECT * FROM cursor WHERE singleton=1"
                    ).fetchone()
                policy = StorageCompressionConfig.model_validate_json(job["policy"])
                cutoff = time.time() - policy.min_age_days * 86400
                query = (
                    Recordings.select()
                    .where(
                        Recordings.end_time < cutoff,
                        Recordings.storage_tier.in_(("local", "archive")),
                    )
                    .bind(database)
                )
                if cursor:
                    query = query.where(
                        (Recordings.start_time > cursor["start_time"])
                        | (
                            (Recordings.start_time == cursor["start_time"])
                            & (Recordings.id > cursor["id"])
                        )
                    )
                scanned = 0
                exhausted = True
                for row in (
                    query.order_by(Recordings.start_time, Recordings.id)
                    .limit(10000)
                    .iterator()
                ):
                    status = self.get_job(job_id)["status"]
                    reason = None
                    if self._cancel.is_set() or status == "stop_requested":
                        self._set(job_id, status="stopped", current_item_id=None)
                        return
                    if status == "pause_requested":
                        self._set(job_id, status="paused", current_item_id=None)
                        return
                    if not self.policy().enabled:
                        reason = "compression-disabled"
                    elif job["scheduled"] and time.time() >= job["window_end"]:
                        reason = "maintenance-window-ended"
                    elif (
                        job["runtime"] + time.monotonic() - started
                        >= policy.max_batch_minutes * 60
                    ):
                        reason = "batch-runtime-exhausted"
                    if reason:
                        self._set(
                            job_id,
                            status="completed",
                            reason=reason,
                            current_item_id=None,
                        )
                        return
                    if self.get_job(job_id)["processed"] >= policy.max_files:
                        exhausted = False
                        break
                    scanned += 1
                    with self._state() as db:
                        seen = db.execute(
                            "SELECT 1 FROM items WHERE job_id=? AND id=?",
                            (job_id, row.id),
                        ).fetchone()
                    if seen is not None or self._completed(row.id):
                        self._cursor(row)
                        continue
                    self._set(job_id, current_item_id=row.id)
                    state, saved = "skipped", 0
                    try:
                        engine = self._engine(database, row.camera, policy)
                        engine.job_id = job_id
                        engine.deadline = (
                            started + policy.max_batch_minutes * 60 - job["runtime"]
                        )
                        if job["scheduled"]:
                            engine.deadline = min(
                                engine.deadline,
                                time.monotonic()
                                + max(0, job["window_end"] - time.time()),
                            )
                        outcome, saved = engine.compress(row)
                        if outcome == "compressed":
                            state = "compressed"
                            self._complete(row.id, engine.last_journal)
                        else:
                            reason = outcome
                    except RepairCancelled:
                        self._set(job_id, status="stopped", current_item_id=None)
                        return
                    except (
                        RepairError,
                        OSError,
                        DatabaseError,
                        ValueError,
                        CompressionConflict,
                        RecordingLeaseLost,
                    ):
                        state, reason = "failed", "conversion-failed-original-retained"
                        # Any unexpected unresolved intent blocks all further writes.
                        for journal in (self.root / "backups").glob("*.json"):
                            data = json.loads(journal.read_text(encoding="utf-8"))
                            if data["state"] in ("committed", "cleaned"):
                                self._complete(data["row"]["id"], journal)
                            if data["state"] not in (
                                "cleaned",
                                "rolled-back-cleaned",
                            ) and not (
                                data.get("version") == 1
                                and data["state"] in ("committed", "rolled-back")
                            ):
                                self.recovery_required = True
                        if self.recovery_required:
                            with self._state() as db:
                                db.execute(
                                    "INSERT OR IGNORE INTO items VALUES(?,?,?,?,?,'failed',?,0)",
                                    (
                                        job_id,
                                        row.id,
                                        row.camera,
                                        row.start_time,
                                        row.end_time,
                                        "recovery-required",
                                    ),
                                )
                            self._set(
                                job_id,
                                status="failed",
                                reason="recovery-required",
                                current_item_id=None,
                            )
                            return
                    with self._state() as db:
                        db.execute(
                            "INSERT OR IGNORE INTO items VALUES(?,?,?,?,?,?,?,?)",
                            (
                                job_id,
                                row.id,
                                row.camera,
                                row.start_time,
                                row.end_time,
                                state,
                                reason,
                                saved,
                            ),
                        )
                    self._cursor(row)
                if exhausted and scanned < 10000:
                    with self._state() as db:
                        db.execute("DELETE FROM cursor")
                self._set(job_id, status="completed", current_item_id=None)
        except Exception:
            # Publication and the private completion ledger are separate durable
            # commits. Never start another batch after an unexpected gap; startup
            # reconciles committed journals before admitting work again.
            self.recovery_required = True
            logger.warning(
                "Compression batch failed; durable originals and journals retained"
            )
            self._set(
                job_id, status="failed", reason="batch-failed", current_item_id=None
            )
        finally:
            with self._state() as db:
                db.execute(
                    "UPDATE jobs SET runtime=runtime+? WHERE id=?",
                    (time.monotonic() - started, job_id),
                )
            if not database.is_closed():
                database.close()
            policy_database = Recordings._meta.database
            if policy_database is not database and not policy_database.is_closed():
                policy_database.close()

    def _cursor(self, row: Recordings) -> None:
        with self._state() as db:
            db.execute(
                "INSERT OR REPLACE INTO cursor VALUES(1,?,?)", (row.start_time, row.id)
            )
