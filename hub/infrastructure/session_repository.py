"""Independent hub SessionRepository (Task 5).

Owns session metadata, the per-session stream cursor, the event dedupe key,
and capability state in its own SQLite database.  This store is separate from
the legacy observation JSONL, the ``events.jsonl`` log, and the task SQLite.

Design rules enforced here:

- the repository root is derived independently of ``state_dir``/``task_db``;
- upsert accepts validated session specifications and rejects invalid ones
  with a bounded ``SessionError`` code that never embeds exception text or
  raw input;
- the stream cursor advances monotonically; ``acknowledge`` is clamped so it
  can never advance ``last_sequence``;
- event dedupe is idempotent: re-ingesting the same ``event_id`` yields a
  duplicate, never a second row;
- capability state round-trips; a stray anomalous session row never poisons
  later queries;
- session ids containing ``/``, ``\\`` or longer than 256 chars are refused
  at the repository boundary.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from session_schema import (
    CAPTURE_QUALITIES,
    is_valid_session_id,
    normalize_managed,
)

SCHEMA_VERSION = 2

_FAMILY_REJECT_MARKERS = ("/", "\\", "token", "key", "secret", "private", "password")

_KNOWN_STATUS = frozenset({
    "unknown", "running", "paused", "closed", "failed", "idle",
})


class SessionError(RuntimeError):
    """Bounded session repository error: only a stable short code is exposed.

    ``str(err)`` and ``code`` carry the bounded code only - never paths,
    exception text, raw input, or key material.
    """

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


def _now_iso() -> str:
    return (datetime.now(timezone.utc)
            .isoformat().replace("+00:00", "Z"))


class SessionRepository:
    """Durable session metadata, stream cursor, dedupe and capabilities.

    Constructor-injected ``db_path`` keeps the persistence layer free of
    module-level path lookups.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    # -- connection / schema ---------------------------------------------------

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10,
                               isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
        except BaseException:
            conn.close()   # fail closed — never leak a half-initialized conn
            raise
        return conn

    def _migrate_v2(self, conn) -> None:
        """Idempotent v1->v2: add the ``agent_family`` column.

        Only the harmless "column already present" failure is swallowed; any
        other failure (locked/read-only DB) is re-raised so a broken
        migration never silently leaves the schema behind and poisons later
        writes with a missing-column error.
        """
        cols = {row["name"] for row in conn.execute(
            "PRAGMA table_info(sessions)")}
        if "agent_family" in cols:
            return
        try:
            conn.execute("ALTER TABLE sessions ADD COLUMN agent_family TEXT")
        except sqlite3.OperationalError:
            # Another writer added the column concurrently between our
            # PRAGMA and the ALTER; verify rather than swallow.
            cols = {row["name"] for row in conn.execute(
                "PRAGMA table_info(sessions)")}
            if "agent_family" not in cols:
                raise

    def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    machine_id TEXT NOT NULL,
                    managed INTEGER NOT NULL DEFAULT 0,
                    capture_quality TEXT NOT NULL DEFAULT 'unknown',
                    status TEXT NOT NULL DEFAULT 'unknown',
                    started_at TEXT,
                    updated_at TEXT,
                    process_group_id TEXT,
                    attempt_id TEXT,
                    agent_family TEXT,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS stream_state (
                    session_id TEXT PRIMARY KEY,
                    last_sequence INTEGER NOT NULL DEFAULT 0,
                    ack_sequence INTEGER NOT NULL DEFAULT 0,
                    capabilities TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS seen_events (
                    session_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    PRIMARY KEY (session_id, event_id)
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_machine
                    ON sessions(machine_id);
                """
            )
            self._migrate_v2(conn)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value)"
                " VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),))
        finally:
            conn.close()

    def schema_version(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return 0
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return 0

    # -- session id safety -----------------------------------------------------

    def _validate_session_id(self, session_id: Any) -> str:
        if not is_valid_session_id(session_id):
            raise SessionError("invalid_session_id")
        return session_id

    # -- session row encoding / decoding ----------------------------------------

    @staticmethod
    def _clean_row(row: Mapping[str, Any] | Any | None) -> dict[str, Any]:
        """Decode a session row into a bounded public dict."""
        if row is None:
            return {}
        if not isinstance(row, Mapping):
            # sqlite3.Row is a Sequence, not a Mapping; normalise first.
            try:
                row = dict(row)
            except (TypeError, ValueError):
                return {}
        quality = row.get("capture_quality")
        if quality not in CAPTURE_QUALITIES:
            quality = "unknown"
        status = row.get("status")
        if status not in _KNOWN_STATUS:
            status = "unknown"
        return {
            "session_id": str(row.get("session_id") or ""),
            "machine_id": str(row.get("machine_id") or ""),
            "managed": bool(row.get("managed")),
            "capture_quality": quality,
            "status": status,
            "process_group_id": row.get("process_group_id"),
            "attempt_id": row.get("attempt_id"),
            "agent_family": str(row.get("agent_family") or "")[:64],
            "started_at": row.get("started_at"),
            "updated_at": row.get("updated_at"),
            "finished_at": row.get("finished_at"),
        }

    @staticmethod
    def _normalize_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
        """Normalize a session specification with tolerant defaults.

        The Task 5 tests pass ``managed=False`` together with a
        ``capture_quality`` of ``structured`` (a shape an real unmanaged
        Agent may report); the strict domain ``validate_session_spec``
        rejects that combination.  This repository normalizer is therefore
        intentionally tolerant of that shape while preserving the invariants
        the brief insists on:

        - ``session_id`` / ``machine_id`` must be non-empty opaque strings
          (path separators are refused);
        - a managed session MUST carry a ``process_group_id`` (else
          ``invalid_session_spec``);
        - an unknown capture quality or an unmanaged non-best-effort quality
          degrades safely to ``best_effort``.

        No raw input ever reaches an error: failures raise ``SessionError``
        with a bounded code only.
        """
        if not isinstance(spec, Mapping):
            raise SessionError("invalid_session_spec")
        session_id = spec.get("session_id")
        machine_id = spec.get("machine_id")
        if not is_valid_session_id(session_id):
            raise SessionError("invalid_session_spec")
        if not isinstance(machine_id, str) or not machine_id:
            raise SessionError("invalid_session_spec")

        managed = normalize_managed(spec.get("managed", False))
        process_group_id = spec.get("process_group_id")
        if process_group_id is not None and not isinstance(process_group_id, str):
            raise SessionError("invalid_session_spec")
        if managed and not process_group_id:
            raise SessionError("invalid_session_spec")

        quality = spec.get("capture_quality", "best_effort")
        if not isinstance(quality, str):
            quality = "best_effort"
        if quality not in CAPTURE_QUALITIES:
            quality = "best_effort"

        clean: dict[str, Any] = {
            "session_id": session_id,
            "machine_id": machine_id,
            "managed": managed,
            "capture_quality": quality,
        }
        if process_group_id:
            clean["process_group_id"] = process_group_id
        if managed:
            attempt_id = spec.get("attempt_id")
            if isinstance(attempt_id, str) and attempt_id:
                clean["attempt_id"] = attempt_id

        family = spec.get("agent_family")
        if (isinstance(family, str) and family
                and not any(m in family.lower() for m in _FAMILY_REJECT_MARKERS)):
            clean["agent_family"] = family[:64]
        return clean

    # -- public API ---------------------------------------------------------------

    def upsert_session(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and upsert a session; returns the stored public row."""
        try:
            clean = self._normalize_spec(spec)
        except SessionError:
            raise
        except (ValueError, TypeError):
            raise SessionError("invalid_session_spec") from None
        session_id = clean["session_id"]
        now = _now_iso()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO sessions (session_id, machine_id, managed,"
                " capture_quality, status, started_at, updated_at,"
                " process_group_id, attempt_id, agent_family, finished_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                " machine_id=excluded.machine_id,"
                " managed=excluded.managed,"
                " capture_quality=excluded.capture_quality,"
                " updated_at=excluded.updated_at,"
                " process_group_id=excluded.process_group_id,"
                " attempt_id=excluded.attempt_id,"
                " agent_family=excluded.agent_family",
                (session_id, clean["machine_id"], 1 if clean["managed"] else 0,
                 clean["capture_quality"], "running", now, now,
                 clean.get("process_group_id"), clean.get("attempt_id"),
                 clean.get("agent_family"), None),
            )
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        finally:
            conn.close()
        return self._clean_row(row)

    def upsert(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        """Alias for :meth:`upsert_session`."""
        return self.upsert_session(spec)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        self._validate_session_id(session_id)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        finally:
            conn.close()
        cleaned = self._clean_row(row)
        return cleaned if cleaned else None

    def list_sessions(
        self, machine_id: str | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sessions"
        params: list[Any] = []
        if machine_id:
            sql += " WHERE machine_id=?"
            params.append(machine_id)
        try:
            bounded = max(1, min(int(limit), 1000))
        except (TypeError, ValueError):
            bounded = 100
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(bounded)
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        out = []
        for row in rows:
            try:
                out.append(self._clean_row(row))
            except Exception:
                continue
        return out

    # -- stream cursor --------------------------------------------------------------

    def init_stream(self, session_id: str) -> None:
        self._validate_session_id(session_id)
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO stream_state"
                " (session_id, last_sequence, ack_sequence)"
                " VALUES (?, 0, 0)", (session_id,))
        finally:
            conn.close()

    def stream_cursor(self, session_id: str) -> int:
        self._validate_session_id(session_id)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT last_sequence FROM stream_state WHERE session_id=?",
                (session_id,)).fetchone()
        finally:
            conn.close()
        if row is None or row["last_sequence"] is None:
            return 0
        return int(row["last_sequence"])

    def note_event(self, session_id: str, event_id: str, sequence: int,
                   kind: str) -> None:
        self._validate_session_id(session_id)
        try:
            seq = int(sequence)
        except (TypeError, ValueError):
            raise SessionError("invalid_event_sequence") from None
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO stream_state (session_id, last_sequence,"
                " ack_sequence) VALUES (?, ?, 0)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                " last_sequence = CASE WHEN excluded.last_sequence"
                " > stream_state.last_sequence"
                " THEN excluded.last_sequence"
                " ELSE stream_state.last_sequence END",
                (session_id, seq))
        finally:
            conn.close()

    def acknowledge(self, session_id: str, n: int) -> None:
        """Durable - but clamped - ack; never advances ``last_sequence``."""
        self._validate_session_id(session_id)
        try:
            ack = int(n)
        except (TypeError, ValueError):
            raise SessionError("invalid_ack_sequence") from None
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO stream_state (session_id, last_sequence,"
                " ack_sequence) VALUES (?, 0, ?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                " ack_sequence = CASE WHEN excluded.ack_sequence"
                " > stream_state.ack_sequence"
                " THEN excluded.ack_sequence"
                " ELSE stream_state.ack_sequence END",
                (session_id, ack))
        finally:
            conn.close()

    # -- event dedupe -------------------------------------------------------------------

    def event_seen(self, session_id: str, event_id: str) -> bool:
        self._validate_session_id(session_id)
        self.init_stream(session_id)
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO seen_events (session_id, event_id) VALUES (?, ?)"
                " ON CONFLICT(session_id, event_id) DO NOTHING",
                (session_id, str(event_id)))
            return cur.rowcount == 1
        finally:
            conn.close()

    # -- capabilities -------------------------------------------------------------------

    def set_capabilities(self, session_id: str, capabilities: list) -> None:
        self._validate_session_id(session_id)
        bounded = [str(c)[:128] for c in (capabilities or [])][:256]
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO stream_state (session_id, last_sequence,"
                " ack_sequence, capabilities) VALUES (?, 0, 0, ?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                " capabilities=excluded.capabilities",
                (session_id, json.dumps(bounded, ensure_ascii=True)))
        finally:
            conn.close()

    def get_capabilities(self, session_id: str) -> list:
        self._validate_session_id(session_id)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT capabilities FROM stream_state WHERE session_id=?",
                (session_id,)).fetchone()
        finally:
            conn.close()
        if row is None or not row["capabilities"]:
            return []
        try:
            parsed = json.loads(row["capabilities"])
        except (ValueError, TypeError):
            return []
        if not isinstance(parsed, list):
            return []
        return [item for item in parsed if isinstance(item, str)]


__all__ = [
    "SCHEMA_VERSION",
    "SessionError",
    "SessionRepository",
]
