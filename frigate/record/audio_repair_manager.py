"""One user-controlled, persistent audio-maintenance worker with safe boundaries."""

import asyncio
import json
import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout
from peewee import DatabaseError, SqliteDatabase

from frigate.api.defs.request.audio_repair_body import (
    AudioRepairScanBody,
    AudioRepairStartBody,
)
from frigate.const import BASE_DIR, CACHE_DIR, RECORD_DIR
from frigate.models import Recordings
from frigate.record.audio_repair import (
    DEFAULT_FFMPEG,
    DEFAULT_FFPROBE,
    AudioRepair,
    RepairCancelled,
    RepairError,
    _plain_path,
)
from frigate.record.policy import CapturePadding

logger = logging.getLogger(__name__)
BACKUP_ROOT = Path(BASE_DIR) / "audio-repair-backups"
BUSY = ("scanning", "running", "pause_requested", "stop_requested")
SAFE_ITEM_FIELDS = (
    "id",
    "camera",
    "start_time",
    "end_time",
    "video_codec",
    "audio_codec",
    "audio_rate",
    "has_audio",
    "state",
    "reason",
)


class RepairJobConflict(RepairError):
    """The requested control is not valid in the current maintenance state."""


class RepairJobNotFound(RepairError):
    """No such maintenance job exists."""


class AudioRepairManager:
    """Persist scans and run at most one cancellable per-file worker."""

    def __init__(
        self,
        config_provider: Callable,
        *,
        backup_root: Path = BACKUP_ROOT,
        recordings_root: Path = Path(RECORD_DIR),
        cache_root: Path = Path(CACHE_DIR),
        ffmpeg: str | None = None,
        ffprobe: str | None = None,
        timeout: float = 120,
    ) -> None:
        """Use server-controlled paths; test overrides are not exposed by the API."""
        self.config_provider = config_provider
        self.root = backup_root
        self.recordings = recordings_root
        self.cache = cache_root
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.timeout = timeout
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="audio-repair"
        )
        self._future: Future | None = None
        self._started = False
        self._closing = False

    async def start(self) -> None:
        """Initialize durable state without automatically resuming old work."""
        await asyncio.to_thread(self._initialize)

    def _initialize(self) -> None:
        _plain_path(self.root)
        _plain_path(self.root / "jobs.sqlite")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _plain_path(self.root)
        with (
            FileLock(str(self.root / "audio-repair.lock"), timeout=0),
            self._state() as db,
        ):
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, phase TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    total INTEGER NOT NULL DEFAULT 0, scanned INTEGER NOT NULL DEFAULT 0,
                    current_item_id TEXT, error TEXT, selectors TEXT NOT NULL,
                    cursor_time REAL, cursor_id TEXT
                );
                CREATE TABLE IF NOT EXISTS items (
                    job_id TEXT NOT NULL, id TEXT NOT NULL, camera TEXT NOT NULL,
                    start_time REAL NOT NULL, end_time REAL NOT NULL,
                    video_codec TEXT, audio_codec TEXT, audio_rate INTEGER,
                    has_audio INTEGER, state TEXT NOT NULL, reason TEXT,
                    chosen INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (job_id, id)
                );
                CREATE INDEX IF NOT EXISTS item_states ON items(job_id, state);
                """
            )
            db.execute(
                """UPDATE jobs SET status='interrupted', current_item_id=NULL,
                   error='restart-requires-new-scan', updated_at=?
                   WHERE status IN ('scanning','running','pause_requested',
                                    'stop_requested','paused')""",
                (time.time(),),
            )
            db.execute(
                """UPDATE items SET state='stopped', reason='interrupted'
                   WHERE state IN ('selected','repairing')"""
            )
        self._started = True

    async def close(self) -> None:
        """Terminate in-flight tools, leave originals intact, and wait for safe exit."""
        with self._lock:
            self._closing = True
            self._cancel.set()
            future = self._future
        if future is not None:
            await asyncio.wrap_future(future)
        self._executor.shutdown(wait=True)

    @contextmanager
    def _state(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(str(self.root / "jobs.sqlite"), timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def _available(self) -> None:
        if not self._started or self._closing:
            raise RepairJobConflict("Maintenance manager is unavailable")
        if self._future is not None and not self._future.done():
            raise RepairJobConflict("Another maintenance operation is active")

    def _job(self, db: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise RepairJobNotFound("Maintenance job not found")
        return row

    def _set_job(self, job_id: str, **values) -> None:
        values["updated_at"] = time.time()
        with self._state() as db:
            db.execute(
                f"UPDATE jobs SET {','.join(f'{key}=?' for key in values)} WHERE id=?",
                (*values.values(), job_id),
            )

    @staticmethod
    def _item(row: sqlite3.Row) -> dict:
        item = {key: row[key] for key in SAFE_ITEM_FIELDS}
        if item["has_audio"] is not None:
            item["has_audio"] = bool(item["has_audio"])
        return item

    def get_job(self, job_id: str) -> dict:
        """Return truthful counters and an optional safe current-item DTO."""
        with self._state() as db:
            row = self._job(db, job_id)
            result = {
                key: row[key]
                for key in (
                    "id",
                    "status",
                    "phase",
                    "created_at",
                    "updated_at",
                    "total",
                    "scanned",
                    "current_item_id",
                    "error",
                )
            }
            counts = db.execute(
                """SELECT
                   SUM(state='candidate') eligible,
                   SUM(chosen) selected,
                   SUM(chosen=1 AND state IN ('repaired','failed','skipped')) processed,
                   SUM(state='repaired') repaired, SUM(state='skipped') skipped,
                   SUM(state='failed') failed
                   FROM items WHERE job_id=?""",
                (job_id,),
            ).fetchone()
            result.update({key: counts[key] or 0 for key in counts.keys()})
            current = db.execute(
                "SELECT * FROM items WHERE job_id=? AND id=?",
                (job_id, row["current_item_id"]),
            ).fetchone()
            result["current_item"] = self._item(current) if current else None
            return result

    def list_jobs(self, limit: int = 10, offset: int = 0) -> dict:
        """List persistent job history, newest first."""
        with self._state() as db:
            total = db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            ids = db.execute(
                "SELECT id FROM jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return {
            "jobs": [self.get_job(row["id"]) for row in ids],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def list_items(
        self, job_id: str, limit: int = 100, offset: int = 0, state: str | None = None
    ) -> dict:
        """Page a scan without loading the entire selection into memory."""
        with self._state() as db:
            self._job(db, job_id)
            where, parameters = "job_id=?", [job_id]
            if state is not None:
                where += " AND state=?"
                parameters.append(state)
            total = db.execute(
                f"SELECT COUNT(*) FROM items WHERE {where}", parameters
            ).fetchone()[0]
            items = db.execute(
                f"SELECT * FROM items WHERE {where} ORDER BY start_time,id LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
        return {
            "items": [self._item(item) for item in items],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def create_scan(self, selectors: AudioRepairScanBody) -> dict:
        """Start an explicitly requested, bounded read-only scan."""
        with self._lock:
            self._available()
            job_id = uuid.uuid4().hex
            now = time.time()
            with self._state() as db:
                db.execute(
                    """INSERT INTO jobs(id,status,phase,created_at,updated_at,selectors)
                       VALUES (?,'scanning','scan',?,?,?)""",
                    (job_id, now, now, selectors.model_dump_json()),
                )
            self._launch(job_id)
            return self.get_job(job_id)

    def _launch(self, job_id: str) -> None:
        self._cancel.clear()
        self._future = self._executor.submit(self._worker, job_id)

    def _unresolved(self) -> bool:
        for journal in self.root.glob("*.json"):
            try:
                if json.loads(journal.read_text(encoding="utf-8")).get("state") not in (
                    "committed",
                    "rolled-back",
                ):
                    return True
            except (OSError, ValueError):
                return True
        return False

    def start_job(self, job_id: str, selection: AudioRepairStartBody) -> dict:
        """Repair only explicitly selected scanned candidates."""
        with self._lock:
            self._available()
            if self._unresolved():
                raise RepairJobConflict(
                    "Unfinished repair journal requires offline recovery"
                )
            with self._state() as db:
                job = self._job(db, job_id)
                if job["status"] not in ("ready", "completed", "stopped") or (
                    job["phase"] == "scan" and job["status"] != "ready"
                ):
                    raise RepairJobConflict("Finish the scan before selecting repairs")
                if selection.all_eligible:
                    chosen = db.execute(
                        """UPDATE items SET state='selected',chosen=1,reason=NULL
                           WHERE job_id=? AND state='candidate'""",
                        (job_id,),
                    ).rowcount
                else:
                    ids = selection.item_ids or []
                    placeholders = ",".join("?" for _ in ids)
                    count = db.execute(
                        f"""SELECT COUNT(*) FROM items WHERE job_id=?
                            AND state='candidate' AND id IN ({placeholders})""",
                        (job_id, *ids),
                    ).fetchone()[0]
                    if count != len(ids):
                        raise RepairJobConflict(
                            "Select only candidates belonging to this job"
                        )
                    chosen = db.execute(
                        f"""UPDATE items SET state='selected',chosen=1,reason=NULL
                            WHERE job_id=? AND id IN ({placeholders})""",
                        (job_id, *ids),
                    ).rowcount
                if not chosen:
                    raise RepairJobConflict("No eligible candidates selected")
                db.execute(
                    """UPDATE jobs SET status='running',phase='repair',error=NULL,
                       updated_at=? WHERE id=?""",
                    (time.time(), job_id),
                )
            self._launch(job_id)
            return self.get_job(job_id)

    def pause(self, job_id: str) -> dict:
        """Request a pause after the current safe scan/repair boundary."""
        with self._lock, self._state() as db:
            job = self._job(db, job_id)
            if job["status"] not in ("scanning", "running"):
                raise RepairJobConflict("Only active work can be paused")
            db.execute(
                "UPDATE jobs SET status='pause_requested',updated_at=? WHERE id=?",
                (time.time(), job_id),
            )
        return self.get_job(job_id)

    def resume(self, job_id: str) -> dict:
        """Resume a paused phase only by explicit user action."""
        with self._lock:
            self._available()
            with self._state() as db:
                job = self._job(db, job_id)
                if job["status"] != "paused":
                    raise RepairJobConflict("Only paused work can resume")
                if job["phase"] == "repair" and self._unresolved():
                    raise RepairJobConflict(
                        "Unfinished repair journal requires offline recovery"
                    )
                db.execute(
                    "UPDATE jobs SET status=?,updated_at=? WHERE id=?",
                    (
                        "scanning" if job["phase"] == "scan" else "running",
                        time.time(),
                        job_id,
                    ),
                )
            self._launch(job_id)
            return self.get_job(job_id)

    def stop(self, job_id: str) -> dict:
        """Stop conversion before publication; completed repairs remain committed."""
        with self._lock, self._state() as db:
            job = self._job(db, job_id)
            if job["status"] not in (*BUSY, "paused"):
                raise RepairJobConflict("Only active or paused work can be stopped")
            status = "stopped" if job["status"] == "paused" else "stop_requested"
            db.execute(
                "UPDATE jobs SET status=?,updated_at=? WHERE id=?",
                (status, time.time(), job_id),
            )
            self._cancel.set()
            if status == "stopped":
                db.execute(
                    """UPDATE items SET state='candidate',chosen=0
                       WHERE job_id=? AND state='selected'""",
                    (job_id,),
                )
        return self.get_job(job_id)

    def _boundary(self, job_id: str) -> bool:
        with self._lock, self._state() as db:
            job = self._job(db, job_id)
            if self._cancel.is_set() or job["status"] == "stop_requested":
                db.execute(
                    """UPDATE jobs SET status='stopped',current_item_id=NULL,
                       updated_at=? WHERE id=?""",
                    (time.time(), job_id),
                )
                db.execute(
                    """UPDATE items SET state='candidate',chosen=0,reason=NULL
                       WHERE job_id=? AND state IN ('selected','repairing')""",
                    (job_id,),
                )
                return False
            if job["status"] == "pause_requested":
                db.execute(
                    "UPDATE jobs SET status='paused',current_item_id=NULL,updated_at=? WHERE id=?",
                    (time.time(), job_id),
                )
                return False
            return True

    def _engine(
        self, database: SqliteDatabase, camera: str | None = None
    ) -> AudioRepair:
        config = self.config_provider()
        camera_config = config.cameras.get(camera) if camera is not None else None
        padding = CapturePadding.from_record(
            camera_config.record if camera_config is not None else config.record
        )
        ffmpeg_config = getattr(camera_config or config, "ffmpeg", None)
        return AudioRepair(
            database,
            self.recordings,
            self.cache,
            self.root,
            self.ffmpeg or getattr(ffmpeg_config, "ffmpeg_path", DEFAULT_FFMPEG),
            self.ffprobe or getattr(ffmpeg_config, "ffprobe_path", DEFAULT_FFPROBE),
            self.timeout,
            padding,
            Path(config.storage.archive_path)
            if getattr(config.storage, "archive_path", None)
            else None,
            online=True,
            cancel=self._cancel,
            padding_provider=lambda: self._capture_padding(camera),
        )

    def _capture_padding(self, camera: str | None) -> CapturePadding:
        config = self.config_provider()
        camera_config = config.cameras.get(camera) if camera is not None else None
        if camera is not None and camera_config is None:
            raise RepairError("Camera configuration changed; rescan before repairing")
        return CapturePadding.from_record(
            camera_config.record if camera_config is not None else config.record
        )

    def _worker(self, job_id: str) -> None:
        database = None
        lease = FileLock(str(self.root / "audio-repair.lock"), timeout=0)
        try:
            lease.acquire()
            # SqliteQueueDatabase does not implement transactions. A separate
            # FULL-synchronous writer supplies a real short IMMEDIATE CAS/commit,
            # without rebinding the application's process-global Peewee models.
            database = SqliteDatabase(
                self.config_provider().database.path,
                timeout=5,
                pragmas={"synchronous": "FULL"},
            )
            database.connect()
            if self.get_job(job_id)["phase"] == "scan":
                self._scan(job_id, database)
            else:
                self._repair(job_id, database)
        except RepairCancelled:
            self._boundary(job_id)
        except RepairError as error:
            self._set_job(
                job_id, status="failed", current_item_id=None, error=str(error)
            )
        except (
            OSError,
            DatabaseError,
            sqlite3.Error,
            ValueError,
            Timeout,
            KeyError,
            TypeError,
            AttributeError,
        ):
            logger.warning(
                "Audio maintenance job failed; originals and recovery journals retained"
            )
            self._set_job(
                job_id,
                status="failed",
                current_item_id=None,
                error="maintenance-failed-recovery-may-be-required",
            )
        finally:
            if lease.is_locked:
                lease.release()
            if database is not None and not database.is_closed():
                database.close()
            # The shared protection policy uses the application's read
            # connection. Close only this worker thread's connection, not its
            # queue writer or the main API thread's connection.
            policy_database = Recordings._meta.database
            if policy_database is not database and not policy_database.is_closed():
                policy_database.close()

    def _scan(self, job_id: str, database: SqliteDatabase) -> None:
        with self._state() as db:
            job = self._job(db, job_id)
            selectors = AudioRepairScanBody.model_validate_json(job["selectors"])
            cursor_time, cursor_id = job["cursor_time"], job["cursor_id"]
        query = Recordings.select().bind(database)
        if selectors.cameras:
            query = query.where(Recordings.camera.in_(selectors.cameras))
        if selectors.start is not None:
            query = query.where(Recordings.start_time >= selectors.start)
        if selectors.end is not None:
            query = query.where(Recordings.end_time <= selectors.end)
        self._set_job(job_id, total=min(selectors.limit, query.count()))
        if cursor_time is not None:
            query = query.where(
                (Recordings.start_time > cursor_time)
                | ((Recordings.start_time == cursor_time) & (Recordings.id > cursor_id))
            )
        remaining = selectors.limit - self.get_job(job_id)["scanned"]
        # Peewee's iterator avoids a full-result cache.
        for row in (
            query.order_by(Recordings.start_time, Recordings.id)
            .limit(remaining)
            .iterator()
        ):
            if not self._boundary(job_id):
                return
            engine = self._engine(database, row.camera)
            reason = (
                "camera-not-configured"
                if row.camera not in self.config_provider().cameras
                else engine.skip_reason(row)
            )
            codec, audio_codec, rate, has_audio = (
                row.video_codec,
                row.audio_codec,
                row.audio_rate,
                row.has_audio,
            )
            if not reason:
                try:
                    probe = engine.probe(Path(row.path))
                    codec = probe["video"]["codec_name"]
                    audio = probe["audio"]
                    has_audio = audio is not None
                    audio_codec = audio["codec_name"] if audio else None
                    rate = (
                        int(audio["sample_rate"])
                        if audio and audio.get("sample_rate")
                        else None
                    )
                    if audio is None:
                        reason = "no-audio"
                    elif (
                        audio_codec == "aac"
                        and audio.get("profile") == "LC"
                        and rate == 48000
                        and audio.get("channels") == 2
                    ):
                        reason = "already-compatible"
                except RepairCancelled:
                    raise
                except (RepairError, OSError, ValueError):
                    reason = "inspection-failed"
            state = "skipped" if reason else "candidate"
            with self._state() as db:
                db.execute(
                    """INSERT INTO items
                       (job_id,id,camera,start_time,end_time,video_codec,audio_codec,
                        audio_rate,has_audio,state,reason)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job_id,
                        row.id,
                        row.camera,
                        row.start_time,
                        row.end_time,
                        codec,
                        audio_codec,
                        rate,
                        has_audio,
                        state,
                        reason,
                    ),
                )
                db.execute(
                    """UPDATE jobs SET scanned=scanned+1,cursor_time=?,cursor_id=?,
                       updated_at=? WHERE id=?""",
                    (row.start_time, row.id, time.time(), job_id),
                )
        if self._boundary(job_id):
            self._set_job(
                job_id,
                status="ready",
                total=self.get_job(job_id)["scanned"],
                current_item_id=None,
            )

    def _repair(self, job_id: str, database: SqliteDatabase) -> None:
        while self._boundary(job_id):
            with self._state() as db:
                item = db.execute(
                    """SELECT * FROM items WHERE job_id=? AND state='selected'
                       ORDER BY start_time,id LIMIT 1""",
                    (job_id,),
                ).fetchone()
                if item is None:
                    db.execute(
                        """UPDATE jobs SET status='completed',current_item_id=NULL,
                           updated_at=? WHERE id=?""",
                        (time.time(), job_id),
                    )
                    return
                db.execute(
                    "UPDATE items SET state='repairing' WHERE job_id=? AND id=?",
                    (job_id, item["id"]),
                )
                db.execute(
                    "UPDATE jobs SET current_item_id=?,updated_at=? WHERE id=?",
                    (item["id"], time.time(), job_id),
                )
            engine = self._engine(database, item["camera"])
            row = engine._load(item["id"])
            state, reason = "skipped", "missing"
            fatal_error = None
            try:
                if row is not None:
                    result = engine.repair(row, apply=True)
                    state = "repaired" if result == "repaired" else "skipped"
                    reason = (
                        None if result == "repaired" else result.removeprefix("skip:")
                    )
            except RepairCancelled:
                raise
            except RepairError as error:
                state = "failed"
                reason = "repair-validation-failed"
                if "disk space" in str(error):
                    fatal_error = str(error)
                    reason = "insufficient-disk-space"
            except (OSError, DatabaseError, ValueError):
                state, reason = (
                    "failed",
                    "repair-failed-original-retained-or-recovery-required",
                )
            with self._state() as db:
                db.execute(
                    "UPDATE items SET state=?,reason=? WHERE job_id=? AND id=?",
                    (state, reason, job_id, item["id"]),
                )
                if state == "repaired":
                    db.execute(
                        """UPDATE items SET audio_codec='aac',audio_rate=48000,has_audio=1
                           WHERE job_id=? AND id=?""",
                        (job_id, item["id"]),
                    )
                db.execute(
                    "UPDATE jobs SET current_item_id=NULL,updated_at=? WHERE id=?",
                    (time.time(), job_id),
                )
            if fatal_error is not None:
                raise RepairError(fatal_error)
            if state == "failed" and self._unresolved():
                raise RepairError("Unfinished repair requires offline recovery")
