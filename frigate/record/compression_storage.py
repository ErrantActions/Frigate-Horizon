"""Pinned-tier compression publication, durable reservations and owned recovery."""

import json
import os
import re
import shutil
import stat
import time
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path

from frigate.models import ArchiveTransfer
from frigate.record.archive import pin_archive, storage_lock, validate_mount
from frigate.record.archive_limits import archive_budget_bytes, archive_owned_bytes
from frigate.record.audio_repair import (
    METADATA,
    RepairCancelled,
    RepairError,
    _digest,
    _fingerprint,
    _plain_path,
    _sync_directory,
)


def reservation_schema_ready(database) -> bool:
    """Require explicitly migrated ownership tags, never reinterpret old transfers."""
    if not {"kind", "owner"}.issubset(ArchiveTransfer._meta.fields):
        return False
    columns = {
        row[1]
        for row in database.execute_sql(
            f"PRAGMA table_info({ArchiveTransfer._meta.table_name})"
        ).fetchall()
    }
    return {"kind", "owner"}.issubset(columns)


def _tool_path(path: Path) -> Path:
    """A subprocess must address the parent's pinned descriptor, not its own."""
    prefix = Path("/proc/self/fd")
    if os.name != "nt" and path.is_relative_to(prefix):
        return Path("/proc") / str(os.getpid()) / "fd" / path.relative_to(prefix)
    return path


def allocated_bytes(info) -> int:
    """Use filesystem-reported allocation, with a portable test-platform fallback."""
    blocks = getattr(info, "st_blocks", None)
    return blocks * 512 if blocks is not None else info.st_size


def _link_fingerprint(path: Path) -> list[int]:
    info = path.lstat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


class CompressionStorage:
    """Keep stable recording URLs unchanged while replacing physical tier content."""

    def __init__(self, engine):
        self.engine = engine
        self.location = None

    @contextmanager
    def locate(self, row: dict, archive: dict | None = None):
        logical = Path(row["path"])
        relative = logical.relative_to(self.engine.recordings)
        if ".." in relative.parts or relative.suffix.lower() != ".mp4":
            raise RepairError("Invalid managed recording path")
        if row["storage_tier"] == "archive":
            settings = archive or self.engine.archive_identity()
            root = Path(settings["root"])
            _plain_path(root)
            with pin_archive(root, settings["mount_id"]) as pinned:
                info = pinned.stat()
                location = {
                    "source": pinned / relative,
                    "logical": logical,
                    "root": root,
                    "pinned": pinned,
                    "identity": (info.st_dev, info.st_ino),
                    "archive": settings,
                    "link_fingerprint": _link_fingerprint(logical),
                }
                previous, self.location = self.location, location
                try:
                    self.check_location()
                    yield location
                finally:
                    self.location = previous
        elif row["storage_tier"] == "local":
            _plain_path(logical)
            location = {"source": logical, "logical": logical, "archive": None}
            previous, self.location = self.location, location
            try:
                yield location
            finally:
                self.location = previous
        else:
            raise RepairError("Unknown recording storage tier")

    def check_location(self) -> None:
        location = self.location
        source = location["source"]
        if location["archive"] is None:
            _plain_path(source)
            return
        root, pinned = location["root"], location["pinned"]
        validate_mount(root, location["archive"]["mount_id"])
        current = root.stat()
        if (current.st_dev, current.st_ino) != location["identity"]:
            raise RepairError("Archive mount changed")
        expected = root / location["logical"].relative_to(self.engine.recordings)
        logical = location["logical"]
        if not logical.is_symlink() or logical.readlink() != expected:
            raise RepairError("Stable archive link changed")
        if _link_fingerprint(logical) != location["link_fingerprint"]:
            raise RepairError("Stable archive link identity changed")
        for parent in logical.parents:
            if parent.is_symlink():
                raise RepairError("Stable recording parent is a symlink")
        relative = source.relative_to(pinned)
        current_path = pinned
        for part in relative.parts:
            current_path = current_path / part
            if current_path.is_symlink():
                raise RepairError("Pinned archive path contains a symlink")

    def unchanged(self, snapshot, fingerprint, digest=None) -> None:
        row = self.engine._load(snapshot["id"])
        if row is None or row.__data__ != snapshot or self.engine.skip_reason(row):
            raise RepairError(
                "Recording changed, became protected or acquired a reader"
            )
        self.check_location()
        if self.location["archive"] is not None:
            if self.engine.archive_identity() != self.location["archive"]:
                raise RepairError("Archive configuration changed")
        source = self.location["source"]
        if _fingerprint(source) != fingerprint or (
            digest is not None and _digest(source) != digest
        ):
            raise RepairError("Physical recording changed")

    def _reservation(self, data):
        return self.engine.database.execute_sql(
            f"SELECT path,size,kind,owner FROM {ArchiveTransfer._meta.table_name} WHERE id=?",
            (data["row"]["id"],),
        ).fetchone()

    def _owns_reservation(self, data) -> bool:
        row = self._reservation(data)
        return row == (
            data["row"]["path"],
            data["reserved_bytes"],
            "compression",
            data["token"],
        )

    def _release(self, data) -> None:
        self.engine.database.execute_sql(
            f"DELETE FROM {ArchiveTransfer._meta.table_name} "
            "WHERE id=? AND path=? AND kind='compression' AND owner=?",
            (data["row"]["id"], data["row"]["path"], data["token"]),
        )

    def reserve(self, data, snapshot, fingerprint) -> None:
        engine = self.engine
        with storage_lock(), engine.database.atomic("IMMEDIATE"):
            self.unchanged(snapshot, fingerprint)
            if (
                engine.database.execute_sql(
                    f"SELECT 1 FROM {ArchiveTransfer._meta.table_name} LIMIT 1"
                ).fetchone()
                is not None
            ):
                raise RepairError("Another storage operation is reserved")
            if data["archive"] is not None:
                budget = archive_budget_bytes(data["archive"]["budget_gib"])
                if (
                    budget is not None
                    and archive_owned_bytes() + data["reserved_bytes"] > budget
                ):
                    raise RepairError("Archive budget cannot hold compression staging")
            engine.database.execute_sql(
                f"INSERT INTO {ArchiveTransfer._meta.table_name}"
                "(id,path,size,kind,owner) VALUES(?,?,?,'compression',?)",
                (
                    snapshot["id"],
                    snapshot["path"],
                    data["reserved_bytes"],
                    data["token"],
                ),
            )

    def paths(self, data):
        token = data["token"]
        if not re.fullmatch("[a-f0-9]{32}", token):
            raise RepairError("Invalid compression ownership token")
        source = self.location["source"]
        return (
            source,
            source.parent / f".frigate-compression-{token}.original.mp4",
            source.parent / f".frigate-compression-{token}.staged.mp4",
        )

    @staticmethod
    def _remove_owned(path: Path, expected_hash: str | None = None) -> None:
        if not path.exists() and not path.is_symlink():
            return
        _fingerprint(path)
        if expected_hash is not None and _digest(path) != expected_hash:
            raise RepairError("Owned compression artifact changed")
        path.unlink()
        _sync_directory(path.parent)

    def _cleanup(
        self, journal: Path, data: dict, *, committed: bool, _locked=False
    ) -> int:
        """Retire originals only after output commit and durable recovery evidence."""
        engine = self.engine
        source, saved, staged = self.paths(data)
        self.check_location()
        expected = data["output_hash"] if committed else data["source_hash"]
        if _digest(source) != expected:
            raise RepairError("Published recording changed before backup retirement")
        current = engine._load(data["row"]["id"])
        if current is None:
            raise RepairError("Recording disappeared before backup retirement")
        reservation = self._reservation(data)
        if reservation is not None and not self._owns_reservation(data):
            raise RepairError("Backup retirement does not own the reservation")
        metadata = data["metadata"] if committed else data["row"]
        fields = (*METADATA, "archive_bytes") if data["archive"] else METADATA
        if any(
            current.__data__.get(key) != value
            for key, value in data["row"].items()
            if key not in fields
        ):
            raise RepairError("Recording identity changed before backup retirement")
        if any(current.__data__.get(key) != metadata.get(key) for key in fields):
            raise RepairError("Recording metadata changed before backup retirement")
        # A durable intent makes a crash before/after either unlink idempotent.
        data["state"] = "committed" if committed else "rolled-back"
        engine._journal(journal, data)
        self._remove_owned(staged, data.get("output_hash") if committed else None)
        self._remove_owned(saved, data["source_hash"] if committed else None)
        with (
            storage_lock() if not _locked else nullcontext(),
            engine.database.atomic("IMMEDIATE"),
        ):
            self.check_location()
            self._release(data)
        data["state"] = "cleaned" if committed else "rolled-back-cleaned"
        data["actual_saved_bytes"] = (
            max(0, data["source_allocated_bytes"] - data["output_allocated_bytes"])
            if committed
            else 0
        )
        engine._journal(journal, data)
        return data["actual_saved_bytes"]

    def compress(self, row) -> tuple[str, int]:
        engine = self.engine
        if not reservation_schema_ready(engine.database):
            raise RepairError("Compression reservation migration is required")
        snapshot = dict(row.__data__)
        with self.locate(snapshot):
            source = self.location["source"]
            fingerprint = _fingerprint(source)
            if fingerprint[2] > 2 * 2**30 or not 0 < row.duration <= 3600:
                return "segment-too-large", 0
            if row.storage_tier == "archive" and row.archive_bytes != fingerprint[2]:
                raise RepairError("Archive exact size is missing or stale")
            original = engine.probe(_tool_path(source))
            # FFmpeg's -fs boundary can be crossed by its final encoded packet.
            # Reserve a conservative frame/packet allowance in addition to both
            # the original backup and the nominal output cap.
            packet_allowance = 64 * 2**20 + (
                int(original["video"]["width"]) * int(original["video"]["height"]) * 8
            )
            working_bytes = 2 * fingerprint[2] + packet_allowance
            token = uuid.uuid4().hex
            engine._active_token = token
            source_info = source.stat()
            data = {
                "version": 2,
                "kind": "compression",
                "token": token,
                "database": str(Path(engine.database.database).absolute()),
                "row": snapshot,
                "source_hash": _digest(source),
                "source_bytes": fingerprint[2],
                "state": "preparing",
                "source_allocated_bytes": allocated_bytes(source_info),
                "archive": self.location["archive"],
                "policy": engine.policy.model_dump(),
                "mount_identity": list(self.location["identity"])
                if self.location["archive"]
                else None,
                "link_fingerprint": self.location.get("link_fingerprint"),
                "job_id": getattr(engine, "job_id", None),
                "reserved_bytes": working_bytes if row.storage_tier == "archive" else 0,
                "created_at": time.time(),
            }
            source, saved, staged = self.paths(data)
            command = engine.command(_tool_path(source), _tool_path(staged), original)
            engine.backup.mkdir(parents=True, exist_ok=True, mode=0o700)
            _plain_path(engine.backup)
            journal = engine.backup / f"{token}.json"
            engine.last_journal = journal
            reserve = engine.policy.min_free_gib * 2**30 + working_bytes
            if shutil.disk_usage(source.parent).free < reserve:
                raise RepairError("Insufficient compression headroom")
            self.unchanged(snapshot, fingerprint, data["source_hash"])
            engine._journal(journal, data)
            try:
                self.reserve(data, snapshot, fingerprint)
                info = source.stat()
                engine._copy(source, saved, info)
                if _digest(saved) != data["source_hash"]:
                    raise RepairError("Compression backup readback failed")
                _sync_directory(saved.parent)
                with staged.open("xb"):
                    os.chmod(staged, 0o600)
                engine._run(command)
                if staged.stat().st_size >= data["source_bytes"]:
                    self.recover(journal, data=data)
                    return "not-smaller", 0
                metadata = engine.validate(
                    original, engine.probe(_tool_path(staged)), _tool_path(staged)
                )
                if data["archive"]:
                    metadata["archive_bytes"] = staged.stat().st_size
                with staged.open("rb+") as handle:
                    os.fsync(handle.fileno())
                staged_info = staged.stat()
                data["output_allocated_bytes"] = allocated_bytes(staged_info)
                if data["output_allocated_bytes"] >= data["source_allocated_bytes"]:
                    self.recover(journal, data=data)
                    return "no-allocation-savings", 0
                if os.name != "nt":
                    os.chown(staged, info.st_uid, info.st_gid)
                os.chmod(staged, stat.S_IMODE(info.st_mode))
                data.update(
                    metadata=metadata, output_hash=_digest(staged), state="ready"
                )
                engine._journal(journal, data)
                engine.reader.check()
                self.unchanged(snapshot, fingerprint, data["source_hash"])
                with storage_lock():
                    try:
                        if engine.cancel.is_set():
                            raise RepairCancelled(
                                "Stopped before compression publication"
                            )
                        engine.reader.check()
                        with engine.database.atomic("IMMEDIATE"):
                            self.unchanged(snapshot, fingerprint)
                            if not self._owns_reservation(data):
                                raise RepairError("Compression reservation was lost")
                            os.replace(staged, source)
                            _sync_directory(source.parent)
                            engine._update(row.id, metadata)
                        data["state"] = "committed"
                        engine._journal(journal, data)
                    except Exception:
                        self.recover(journal, _locked=True)
                        raise
                return "compressed", self._cleanup(journal, data, committed=True)
            except Exception:
                self.recover(journal)
                raise

    def recover(self, journal: Path, *, _locked=False, data=None) -> str:
        """Reconcile only journal-owned artifacts on the original verified mount."""
        engine = self.engine
        _plain_path(journal)
        if journal.parent != engine.backup:
            raise RepairError("Unexpected compression journal directory")
        data = data or json.loads(journal.read_text(encoding="utf-8"))
        if (
            data["version"] != 2
            or data["kind"] != "compression"
            or data["database"] != str(Path(engine.database.database).absolute())
            or journal.name != f"{data['token']}.json"
        ):
            raise RepairError("Compression journal identity mismatch")
        if data["state"] in ("cleaned", "rolled-back-cleaned"):
            return data["state"]
        with self.locate(data["row"], data["archive"]):
            if (
                data["archive"]
                and list(self.location["identity"]) != data["mount_identity"]
            ):
                raise RepairError("Recovery mount identity changed")
            if (
                data["archive"]
                and self.location["link_fingerprint"] != data["link_fingerprint"]
            ):
                raise RepairError("Recovery stable archive link changed")
            source, saved, staged = self.paths(data)
            if data["state"] == "committed":
                self._cleanup(journal, data, committed=True, _locked=_locked)
                return "cleaned"
            current_fingerprint = _fingerprint(source)
            current_hash = _digest(source)
            expected_hashes = (data["source_hash"], data.get("output_hash"))
            if current_hash not in expected_hashes:
                raise RepairError("Unexpected recording content during recovery")
            saved_fingerprint = None
            if current_hash != data["source_hash"]:
                saved_fingerprint = _fingerprint(saved)
                if _digest(saved) != data["source_hash"]:
                    raise RepairError("Compression recovery original is damaged")
            with (
                storage_lock() if not _locked else nullcontext(),
                engine.database.atomic("IMMEDIATE"),
            ):
                current = engine._load(data["row"]["id"])
                fields = (*METADATA, "archive_bytes")
                if current is None or any(
                    current.__data__.get(key) != value
                    for key, value in data["row"].items()
                    if key not in fields
                ):
                    raise RepairError("Recording identity changed during recovery")
                reservation = self._reservation(data)
                if reservation is not None and not self._owns_reservation(data):
                    raise RepairError("Another storage operation owns this recording")
                self.check_location()
                if _fingerprint(source) != current_fingerprint:
                    raise RepairError("Recording changed during recovery")
                if current_hash != data["source_hash"]:
                    # The original reader lease may have expired across a crash.
                    # Never change content underneath a newly acquired reader.
                    if engine.other_readers(current):
                        raise RepairError("Recovery is waiting for active readers")
                    if _fingerprint(saved) != saved_fingerprint:
                        raise RepairError("Recovery backup changed")
                    # The same-volume original itself becomes canonical again.
                    # A crash after this rename still leaves the original at the
                    # stable target, so no large copy is needed under the lock.
                    os.replace(saved, source)
                    _sync_directory(source.parent)
                metadata = {key: data["row"].get(key) for key in METADATA}
                if data["archive"]:
                    metadata["archive_bytes"] = data["row"]["archive_bytes"]
                engine._update(current.id, metadata)
            self._cleanup(journal, data, committed=False, _locked=_locked)
            return "rolled-back"
