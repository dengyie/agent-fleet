"""JSONL observation repository (per-machine snapshot history).

Owns observation persistence and idempotent ingest receipts:

* per-machine ``RLock`` serializes access for one machine
* ``machine_transaction`` holds the ``RLock`` plus a cross-process file lock
  for a whole read→decide→save sequence; it is re-entrant per thread so the
  nested ``save_snapshot`` never re-locks the same file through a new fd
* ``state/<machine>.jsonl`` is appended with one JSON snapshot per line; each
  snapshot is stamped with ``_ts`` in-place before serialization
* oversized JSONL files rotate by atomically replacing the file with the tail
  ``keep_lines`` rows (``NamedTemporaryFile`` + ``flush`` + ``fsync`` +
  ``os.replace``)
* ``state/<machine>.json`` holds the latest snapshot as pretty JSON, replaced
  atomically
* ``state/observation-reports.sqlite3`` retains bounded ingest receipts so a
  retried POST remains idempotent after JSONL rotation or process restart
* malformed lines in the JSONL history are skipped silently
* machine names must match the auth machine-name contract so repository paths
  never escape the repository directory

Callers (``hub.bootstrap`` and the ``hub/scan.py`` CLI) construct it on an
explicit ``Path`` and inject it into the application services.
"""

import json
import os
import sqlite3
import tempfile
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

from hub.infrastructure.jsonl import repair_jsonl_tail
from hub.repositories import MACHINE_NAME_RE, validate_machine_name

MAX_JSONL_BYTES = 5 * 1024 * 1024
ROTATE_KEEP_LINES = 2000
REPORT_ID_RETENTION_S = 24 * 60 * 60


@contextmanager
def _machine_file_lock(state_dir: Path, machine: str):
    """Serialize one machine's observation writes across Hub processes."""
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(state_dir / f".{machine}.observation.lock", flags, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


class JsonlObservationRepository:
    """Per-machine JSONL history with an atomic current-JSON mirror.

    Constructor-injected ``state_dir`` keeps the persistence layer free of
    module-level path lookups and the Flask stack.
    """

    def __init__(self, state_dir: Path, *, max_jsonl_bytes=MAX_JSONL_BYTES,
                 keep_lines=ROTATE_KEEP_LINES) -> None:
        self.state_dir = Path(state_dir)
        self.max_jsonl_bytes = int(max_jsonl_bytes)
        self.keep_lines = int(keep_lines)
        self._locks = defaultdict(threading.RLock)
        # 线程本地的每机事务深度：嵌套进入时只计数，不再对同一锁文件新开 fd
        # 加 flock（flock 按 open file description 计，新 fd 会自死锁）。
        self._txn_local = threading.local()

    @contextmanager
    def machine_transaction(self, machine: str):
        """Re-entrant per-machine critical section across threads and processes.

        Holds the in-process ``RLock`` and the machine's file lock for the
        whole block, so a caller's read_current → decide → save sequence cannot
        interleave with another Hub process (e.g. ``hub/scan.py``) writing the
        same machine. Nested entries in the same thread only bump a depth
        counter; the outermost entry owns (and releases) the file lock.
        """
        validated = validate_machine_name(machine)
        with self._locks[validated]:
            depths = getattr(self._txn_local, "depths", None)
            if depths is None:
                depths = self._txn_local.depths = {}
            if depths.get(validated, 0):
                depths[validated] += 1
                try:
                    yield
                finally:
                    depths[validated] -= 1
                return
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with _machine_file_lock(self.state_dir, validated):
                depths[validated] = 1
                try:
                    yield
                finally:
                    depths.pop(validated, None)

    # -- internal helpers -------------------------------------------------

    def _mach_file(self, machine: str, ext: str = "jsonl") -> Path:
        return self.state_dir / f"{validate_machine_name(machine)}.{ext}"

    @contextmanager
    def _report_db(self):
        connection = sqlite3.connect(
            self.state_dir / "observation-reports.sqlite3", timeout=30,
        )
        try:
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS observation_reports (
                    machine TEXT NOT NULL,
                    report_id TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (machine, report_id)
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS observation_report_expiry "
                "ON observation_reports(created_at) WHERE completed = 1"
            )
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _set_report_completed(self, machine: str, report_id: str) -> None:
        with self._report_db() as connection:
            connection.execute(
                "UPDATE observation_reports SET completed = 1, snapshot_json = '' "
                "WHERE machine = ? AND report_id = ?",
                (machine, report_id),
            )

    def _last_history_snapshot(self, machine: str) -> dict | None:
        rows = self.read_history(machine, limit=1)
        return rows[-1] if rows else None

    def _materialize_report(
        self, machine: str, report_id: str, snapshot: dict
    ) -> None:
        """Finish a pending receipt; safe to resume after any process crash."""
        self._remove_incomplete_history_tail(machine)
        latest = self._last_history_snapshot(machine)
        if (not latest or latest.get("_ingest_report_id") != report_id
                or latest.get("_ts") != snapshot["_ts"]):
            self._append_and_publish(machine, dict(snapshot))
        else:
            current = dict(latest)
            current.pop("_ingest_report_id", None)
            self._write_current(machine, current)
        self._set_report_completed(machine, report_id)

    def _remove_incomplete_history_tail(self, machine: str) -> None:
        """Repair a torn final JSONL record before resuming a pending receipt."""
        repair_jsonl_tail(self._mach_file(machine))

    def _recover_pending_reports(self, machine: str) -> None:
        if not (self.state_dir / "observation-reports.sqlite3").exists():
            return
        with self._report_db() as connection:
            pending = connection.execute(
                "SELECT report_id, snapshot_json FROM observation_reports "
                "WHERE machine = ? AND completed = 0 ORDER BY rowid",
                (machine,),
            ).fetchall()
        for report_id, snapshot_json in pending:
            self._materialize_report(machine, report_id, json.loads(snapshot_json))

    def _rotate_if_needed(self, machine: str, f: Path) -> bool:
        """Truncate an oversized JSONL to the tail ``keep_lines`` rows.

        Not holding a lock here by itself: callers hold the machine lock so
        the rotation and its atomic replace stay serialized.
        """
        try:
            if not f.exists() or f.stat().st_size <= self.max_jsonl_bytes:
                return False
        except OSError:
            return False
        try:
            lines = f.read_text().splitlines()
        except OSError:
            return False
        keep = lines[-self.keep_lines:]
        tmp = tempfile.NamedTemporaryFile(
            mode="w", dir=self.state_dir, prefix="rotate-",
            suffix=".tmp", delete=False,
        )
        tmp_name = tmp.name
        try:
            with tmp:
                tmp.write("\n".join(keep) + "\n")
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, f)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
        return True

    # -- ObservationRepository protocol -----------------------------------

    def save_snapshot(self, machine: str, snapshot: dict) -> None:
        """Append the snapshot to the JSONL and atomically replace ``current``.

        Mirrors the legacy behavior exactly: an in-place ``_ts`` stamp, the
        JSONL append, bounded rotation, and the atomic current-JSON replace.
        """
        validated = validate_machine_name(machine)
        with self.machine_transaction(validated):
            self._recover_pending_reports(validated)
            self._append_and_publish(validated, snapshot)

    def save_snapshot_once(
        self, machine: str, snapshot: dict, report_id: str
    ) -> bool:
        """Persist once within the receipt window; return True for a new report."""
        validated = validate_machine_name(machine)
        if not isinstance(report_id, str) or not report_id:
            raise ValueError("report_id must be a non-empty string")
        with self.machine_transaction(validated):
            self._recover_pending_reports(validated)
            stored = dict(snapshot)
            stored["_ingest_report_id"] = report_id
            stored["_ts"] = time.time()
            snapshot["_ts"] = stored["_ts"]
            snapshot_json = json.dumps(stored, ensure_ascii=False)
            with self._report_db() as connection:
                connection.execute(
                    "DELETE FROM observation_reports "
                    "WHERE completed = 1 AND created_at < ?",
                    (time.time() - REPORT_ID_RETENTION_S,),
                )
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO observation_reports "
                    "(machine, report_id, snapshot_json, completed, created_at) "
                    "VALUES (?, ?, ?, 0, ?)",
                    (validated, report_id, snapshot_json, time.time()),
                )
                inserted = cursor.rowcount == 1
                row = connection.execute(
                    "SELECT snapshot_json, completed FROM observation_reports "
                    "WHERE machine = ? AND report_id = ?",
                    (validated, report_id),
                ).fetchone()
                if row is None:
                    raise RuntimeError("observation receipt disappeared")
                if row[1]:
                    connection.execute(
                        "UPDATE observation_reports SET created_at = ? "
                        "WHERE machine = ? AND report_id = ?",
                        (time.time(), validated, report_id),
                    )
            if not inserted and row[1]:
                return False
            # The receipt is durably pending before JSONL/current writes.
            # A later request recovers it before any further history append.
            self._materialize_report(validated, report_id, json.loads(row[0]))
            return inserted

    def _append_and_publish(self, machine: str, snapshot: dict) -> None:
        if "_ingest_report_id" not in snapshot:
            snapshot["_ts"] = time.time()
        history_path = self._mach_file(machine)
        with open(history_path, "a", encoding="utf-8") as history:
            history.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
            history.flush()
            os.fsync(history.fileno())
        self._rotate_if_needed(machine, history_path)
        current = dict(snapshot)
        current.pop("_ingest_report_id", None)
        self._write_current(machine, current)

    def _write_current(self, machine: str, snapshot: dict) -> None:
        target = self._mach_file(machine, "json")
        tmp = tempfile.NamedTemporaryFile(
            mode="w", dir=self.state_dir, prefix="snapshot-",
            suffix=".tmp", delete=False, encoding="utf-8",
        )
        tmp_name = tmp.name
        try:
            with tmp:
                json.dump(snapshot, tmp, ensure_ascii=False, indent=2)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, target)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    def read_current(self, machine: str) -> dict | None:
        """Return the most recent snapshot for ``machine`` or ``None``."""
        validated = validate_machine_name(machine)
        f = self._mach_file(validated, "json")
        if not f.exists():
            return None
        with self._locks[validated]:
            try:
                return json.loads(f.read_text())
            except Exception:
                return None

    def read_history(self, machine: str, limit: int = 50) -> list[dict]:
        """Return the newest (up to ``limit``) snapshots, oldest first."""
        validated = validate_machine_name(machine)
        f = self._mach_file(validated)
        if not f.exists():
            return []
        with self._locks[validated]:
            out = []
            for raw_line in f.read_bytes().splitlines()[-limit:]:
                try:
                    out.append(json.loads(raw_line.decode("utf-8")))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
        return out

    def machines(self) -> set[str]:
        """Return the machine names that have a current snapshot.

        Stems that do not satisfy the machine-name contract are skipped:
        a stray malformed ``*.json`` artifact must never leak into machine
        enumeration and abort reconciliation.
        """
        try:
            return {
                p.stem for p in self.state_dir.glob("*.json")
                if MACHINE_NAME_RE.fullmatch(p.stem)
            }
        except OSError:
            return set()
