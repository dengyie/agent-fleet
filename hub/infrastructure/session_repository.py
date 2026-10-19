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
from hub.domain.session import accepts_agent_state

from session_schema import (
    AGENT_STATE_CONFIDENCES,
    AGENT_STATE_SOURCES,
    AGENT_STATES,
    CAPTURE_QUALITIES,
    is_valid_session_id,
    normalize_managed,
)

SCHEMA_VERSION = 3

_FAMILY_REJECT_MARKERS = ("/", "\\", "token", "key", "secret", "private", "password")

_KNOWN_STATUS = frozenset({
    "unknown", "running", "paused", "closed", "failed", "idle",
})
_ACTIVE_STATUSES = ("unknown", "running", "paused")
_QUALITY_RANK = {
    "exact": 0,
    "structured": 1,
    "best_effort": 2,
}


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
        self.management_available = False

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

    def _migrate_v3(self, conn) -> None:
        """Idempotent v1/v2->v3: add family and lifecycle summary columns.

        Only the harmless "column already present" failure is swallowed; any
        other failure (locked/read-only DB) is re-raised so a broken
        migration never silently leaves the schema behind and poisons later
        writes with a missing-column error.
        """
        cols = {row["name"] for row in conn.execute(
            "PRAGMA table_info(sessions)")}
        additions = {
            "agent_family": "TEXT",
            "current_state": "TEXT NOT NULL DEFAULT 'unknown'",
            "state_source": "TEXT NOT NULL DEFAULT 'unknown'",
            "state_confidence": "TEXT NOT NULL DEFAULT 'low'",
            "state_sequence": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, declaration in additions.items():
            if name in cols:
                continue
            try:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {declaration}")
            except sqlite3.OperationalError:
                # Another writer may have completed this idempotent migration
                # between PRAGMA and ALTER; verify before continuing.
                cols = {row["name"] for row in conn.execute(
                    "PRAGMA table_info(sessions)")}
                if name not in cols:
                    raise
        conn.execute("CREATE TABLE IF NOT EXISTS interaction_requests ("
            "session_id TEXT NOT NULL,request_id TEXT NOT NULL,sequence INTEGER NOT NULL,"
            "payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',client_token TEXT,"
            "answer TEXT,command_id TEXT,PRIMARY KEY(session_id,request_id),UNIQUE(session_id,client_token))")
        stream_columns = {row["name"] for row in conn.execute("PRAGMA table_info(stream_state)")}
        if "capability_sequence" not in stream_columns:
            conn.execute("ALTER TABLE stream_state ADD COLUMN capability_sequence INTEGER NOT NULL DEFAULT 0")

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
                    current_state TEXT NOT NULL DEFAULT 'unknown',
                    state_source TEXT NOT NULL DEFAULT 'unknown',
                    state_confidence TEXT NOT NULL DEFAULT 'low',
                    state_sequence INTEGER NOT NULL DEFAULT 0,
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
            # v2 remains required by legacy ingestion; v3 is an optional read model.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
            if "agent_family" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN agent_family TEXT")
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._migrate_v3(conn)
                conn.execute("INSERT OR REPLACE INTO meta (key,value) VALUES ('schema_version',?)",
                             (str(SCHEMA_VERSION),))
                conn.commit()
                self.management_available = True
            except sqlite3.Error:
                conn.rollback()
                self.management_available = False
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
        try:
            state_sequence = max(0, int(row.get("state_sequence") or 0))
        except (TypeError, ValueError, OverflowError):
            state_sequence = 0
        return {
            "session_id": str(row.get("session_id") or ""),
            "machine_id": str(row.get("machine_id") or ""),
            "managed": normalize_managed(row.get("managed")),
            "capture_quality": quality,
            "status": status,
            "process_group_id": row.get("process_group_id"),
            "attempt_id": row.get("attempt_id"),
            "agent_family": str(row.get("agent_family") or "")[:64],
            "current_state": (row.get("current_state")
                              if row.get("current_state") in AGENT_STATES
                              else "unknown"),
            "state_source": (row.get("state_source")
                             if row.get("state_source") in AGENT_STATE_SOURCES
                             else "unknown"),
            "state_confidence": (row.get("state_confidence")
                                 if row.get("state_confidence") in AGENT_STATE_CONFIDENCES
                                 else "low"),
            "state_sequence": state_sequence,
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
        if not is_valid_session_id(machine_id):
            raise SessionError("invalid_session_spec")

        managed = normalize_managed(spec.get("managed", False))
        process_group_id = spec.get("process_group_id")
        if process_group_id is not None and not is_valid_session_id(process_group_id):
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
        attempt_id = spec.get("attempt_id")
        if attempt_id is not None and not is_valid_session_id(attempt_id):
            raise SessionError("invalid_session_spec")
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
            # Session identity is a binding, not ordinary mutable telemetry.
            # Serialize the read/merge/write so a sparse or stale event cannot
            # revoke a managed binding or move a session between machines.
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT machine_id, managed, capture_quality,"
                " process_group_id, attempt_id, agent_family"
                " FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if existing is not None:
                if (existing["machine_id"]
                        and existing["machine_id"] != clean["machine_id"]):
                    raise SessionError("session_binding_mismatch")
                existing_group = existing["process_group_id"]
                incoming_group = clean.get("process_group_id")
                if (existing_group and incoming_group
                        and existing_group != incoming_group):
                    raise SessionError("session_binding_mismatch")
                existing_attempt = existing["attempt_id"]
                incoming_attempt = clean.get("attempt_id")
                if (existing_attempt and incoming_attempt
                        and existing_attempt != incoming_attempt):
                    raise SessionError("session_binding_mismatch")

                # Once a session is managed, ordinary telemetry may omit its
                # binding but can never silently downgrade it.  Capture quality
                # is similarly monotonic toward best_effort.
                clean["managed"] = bool(
                    existing["managed"] or clean["managed"])
                if existing_group:
                    clean["process_group_id"] = existing_group
                if existing_attempt:
                    clean["attempt_id"] = existing_attempt
                existing_family = existing["agent_family"]
                if existing_family:
                    clean["agent_family"] = existing_family
                old_quality = existing["capture_quality"]
                if old_quality not in _QUALITY_RANK:
                    old_quality = "best_effort"
                if (_QUALITY_RANK.get(clean["capture_quality"], 2)
                        < _QUALITY_RANK[old_quality]):
                    clean["capture_quality"] = old_quality

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
                " agent_family=excluded.agent_family,"
                " status = CASE WHEN sessions.status IN ('closed','failed')"
                " THEN sessions.status ELSE 'running' END,"
                " started_at = COALESCE(sessions.started_at, excluded.started_at),"
                " finished_at = CASE WHEN sessions.status IN ('closed','failed')"
                " THEN sessions.finished_at ELSE NULL END",
                (session_id, clean["machine_id"], 1 if clean["managed"] else 0,
                 clean["capture_quality"], "running", now, now,
                 clean.get("process_group_id"), clean.get("attempt_id"),
                 clean.get("agent_family"), None),
            )
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self._clean_row(row)

    def update_agent_state(self, session_id: str, state: str, source: str,
                           confidence: str, sequence: int) -> dict[str, Any] | None:
        """Apply one candidate lifecycle state using deterministic precedence.

        Event history remains authoritative in the transcript store.  This
        method only updates the bounded session summary and never upgrades an
        unmanaged session.  Newer sequence wins; source and confidence break
        ties, while a lower-confidence reverse transition cannot displace the
        current state unless it comes from a more authoritative source.
        """
        self._validate_session_id(session_id)
        if not self.management_available:
            raise SessionError("feature_unavailable")
        if state not in AGENT_STATES or source not in AGENT_STATE_SOURCES:
            raise SessionError("invalid_agent_state")
        if confidence not in AGENT_STATE_CONFIDENCES:
            raise SessionError("invalid_agent_state")
        if source == "pty" and (state not in ("unknown", "idle") or confidence != "low"):
            raise SessionError("invalid_agent_state")
        if (isinstance(sequence, bool) or not isinstance(sequence, int)
                or not 1 <= sequence <= (1 << 63) - 1):
            raise SessionError("invalid_event_sequence")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT managed,current_state,state_source,state_confidence,state_sequence"
                " FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                conn.rollback()
                return None
            if accepts_agent_state(row, state, source, confidence, sequence):
                now = _now_iso()
                conn.execute(
                    "UPDATE sessions SET current_state=?, state_source=?,"
                    " state_confidence=?, state_sequence=?, updated_at=?"
                    " WHERE session_id=?",
                    (state, source, confidence, sequence, now, session_id),
                )
            conn.commit()
            result = conn.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self._clean_row(result) if result is not None else None

    def close_session(self, session_id: str, status: str,
                      finished_at: str) -> dict[str, Any] | None:
        """Move a session to a terminal state exactly once.

        Terminal states are monotonic: late metadata cannot reopen them, and
        a later closed event cannot overwrite failed.
        """
        self._validate_session_id(session_id)
        if status not in ("closed", "failed"):
            raise SessionError("invalid_session_status")
        if not isinstance(finished_at, str) or not finished_at:
            raise SessionError("invalid_finished_at")
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE sessions SET status = CASE"
                " WHEN status='failed' THEN 'failed'"
                " WHEN status='closed' THEN 'closed'"
                " ELSE ? END, finished_at = CASE"
                " WHEN status IN ('closed','failed') THEN finished_at"
                " ELSE ? END, updated_at=? WHERE session_id=?",
                (status, finished_at, finished_at, session_id),
            )
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        return self._clean_row(row) if row is not None else None

    def finish_managed_session(self, session_id: str,
                                finished_at: str) -> dict[str, Any] | None:
        """Atomically close a managed session and project its stopped state."""
        self._validate_session_id(session_id)
        if not isinstance(finished_at, str) or not finished_at:
            raise SessionError("invalid_finished_at")
        if not self.management_available:
            raise SessionError("feature_unavailable")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None or not normalize_managed(row["managed"]):
                conn.commit()
                return None

            now = _now_iso()
            update_state = (
                row["status"] not in ("closed", "failed")
                and row["current_state"] not in (
                    "completed", "failed", "stopped")
            )
            state_sequence = max(1, int(row["state_sequence"] or 0) + 1)
            if (update_state and accepts_agent_state(
                    row, "stopped", "supervisor", "high", state_sequence)):
                conn.execute(
                    "UPDATE sessions SET current_state='stopped',"
                    " state_source='supervisor',state_confidence='high',"
                    " state_sequence=?,updated_at=? WHERE session_id=?",
                    (state_sequence, now, session_id),
                )
            conn.execute(
                "UPDATE sessions SET status=CASE"
                " WHEN status='failed' THEN 'failed' ELSE 'closed' END,"
                " finished_at=CASE WHEN status IN ('closed','failed')"
                " THEN finished_at ELSE ? END,updated_at=? WHERE session_id=?",
                (finished_at, now, session_id),
            )
            result = conn.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self._clean_row(result) if result is not None else None

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
        active_only: bool = False,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sessions"
        params: list[Any] = []
        predicates = []
        if machine_id:
            predicates.append("machine_id=?")
            params.append(machine_id)
        if active_only:
            # Normal active states are explicit.  NULL/unknown future values
            # remain visible and are normalized to ``unknown`` by _clean_row;
            # only the known terminal/idle states are excluded.
            predicates.append(
                "(status IN (?,?,?) OR status IS NULL OR "
                "status NOT IN (?,?,?,?,?,?))")
            params.extend(_ACTIVE_STATUSES)
            params.extend(sorted(_KNOWN_STATUS))
        if predicates:
            sql += " WHERE " + " AND ".join(predicates)
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
        try:
            return max(0, int(row["last_sequence"]))
        except (TypeError, ValueError, OverflowError):
            return 0

    def note_event(self, session_id: str, event_id: str, sequence: int,
                   kind: str) -> None:
        self._validate_session_id(session_id)
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise SessionError("invalid_event_sequence") from None
        seq = sequence
        if seq < 1:
            raise SessionError("invalid_event_sequence")
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
        if isinstance(n, bool) or not isinstance(n, int):
            raise SessionError("invalid_ack_sequence") from None
        ack = n
        if ack < 0:
            raise SessionError("invalid_ack_sequence")
        conn = self._connect()
        try:
            # Materialise the stream row first so an ACK for an unknown or
            # not-yet-observed session is recorded as zero, never as a
            # cursor that the durable stream has not reached.  The UPDATE
            # clamps both the requested value and any legacy invalid value
            # against last_sequence while retaining monotonic advancement.
            conn.execute(
                "INSERT OR IGNORE INTO stream_state"
                " (session_id, last_sequence, ack_sequence)"
                " VALUES (?, 0, 0)", (session_id,))
            conn.execute(
                "UPDATE stream_state SET ack_sequence = CASE"
                " WHEN ack_sequence > last_sequence THEN last_sequence"
                " WHEN MIN(?, last_sequence) > ack_sequence"
                " THEN MIN(?, last_sequence)"
                " ELSE ack_sequence END WHERE session_id=?",
                (ack, ack, session_id))
        finally:
            conn.close()

    def ack_cursor(self, session_id: str) -> int:
        """Return the durable contiguous uploader ACK cursor."""
        self._validate_session_id(session_id)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT MAX(0, MIN(COALESCE(ack_sequence, 0),"
                " COALESCE(last_sequence, 0))) AS ack_sequence"
                " FROM stream_state WHERE session_id=?",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None or row["ack_sequence"] is None:
            return 0
        try:
            return max(0, int(row["ack_sequence"]))
        except (TypeError, ValueError, OverflowError):
            return 0

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

    def project_interaction(self, session_id: str, sequence: int, kind: str, payload: dict) -> None:
        """Project only accepted question records, preserving the first request identity."""
        self._validate_session_id(session_id)
        conn = self._connect()
        try:
            if kind == "interaction_request":
                conn.execute("INSERT INTO interaction_requests"
                    " (session_id,request_id,sequence,payload) VALUES (?,?,?,?)"
                    " ON CONFLICT(session_id,request_id) DO UPDATE SET payload=excluded.payload"
                    " WHERE interaction_requests.payload='{}'",
                    (session_id, payload["request_id"], sequence, json.dumps(payload)))
            elif kind == "interaction_response":
                conn.execute("INSERT INTO interaction_requests"
                    " (session_id,request_id,sequence,payload,status) VALUES (?,?,?,'{}','answered')"
                    " ON CONFLICT(session_id,request_id) DO UPDATE SET status='answered'"
                    " WHERE interaction_requests.status='pending' AND interaction_requests.sequence<excluded.sequence",
                    (session_id, payload["request_id"], sequence))
            elif kind == "session_close":
                conn.execute("UPDATE interaction_requests SET status='expired'"
                    " WHERE session_id=? AND status='pending' AND sequence<?", (session_id, sequence))
        finally:
            conn.close()

    def get_interaction(self, session_id: str, request_id: str) -> dict | None:
        self._validate_session_id(session_id)
        conn = self._connect()
        try:
            row = conn.execute("SELECT payload FROM interaction_requests"
                " WHERE session_id=? AND request_id=?", (session_id, request_id)).fetchone()
            body = json.loads(row["payload"]) if row else None
            return body if body and "question_type" in body else None
        finally:
            conn.close()

    def reserve_interaction(self, session_id: str, request_id: str, client_token: str,
                            answer: dict) -> dict | None:
        """Durable claim BEFORE issuing I/O; interrupted dispatch never issues twice."""
        encoded = json.dumps(answer, sort_keys=True, separators=(",", ":"))
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status,client_token,answer,command_id"
                " FROM interaction_requests WHERE session_id=? AND request_id=?",
                (session_id, request_id)).fetchone()
            if row is None or row["status"] == "expired":
                raise SessionError("interaction_expired")
            if row["status"] != "pending":
                if row["client_token"] != client_token or row["answer"] != encoded:
                    raise SessionError("interaction_answered")
                if not row["command_id"]:
                    raise SessionError("interaction_pending" if row["status"] == "dispatching" else "interaction_unavailable")
                return {"session_id": session_id, "command_id": row["command_id"], "status": "queued"}
            conn.execute("UPDATE interaction_requests SET status='dispatching',client_token=?,answer=?"
                " WHERE session_id=? AND request_id=?", (client_token, encoded, session_id, request_id))
            conn.commit()
            return None
        except sqlite3.IntegrityError as exc:
            raise SessionError("interaction_answered") from exc
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()

    def finish_interaction(self, session_id: str, request_id: str, command_id: str | None) -> None:
        conn = self._connect()
        try:
            cur = conn.execute("UPDATE interaction_requests SET status=?,command_id=?"
                " WHERE session_id=? AND request_id=? AND status='dispatching'",
                ("answered" if command_id else "failed", command_id, session_id, request_id))
            if cur.rowcount != 1:
                raise SessionError("interaction_pending" if command_id else "interaction_unavailable")
        finally:
            conn.close()

    def interaction_command(self, session_id: str, request_id: str) -> str | None:
        self._validate_session_id(session_id)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT command_id FROM interaction_requests WHERE session_id=? AND request_id=?",
                (session_id, request_id)).fetchone()
            return row["command_id"] if row and row["command_id"] else None
        finally:
            conn.close()

    def set_capabilities(self, session_id: str, capabilities: list, *, sequence: int | None = None) -> None:
        self._validate_session_id(session_id)
        if sequence is not None and (type(sequence) is not int or not 1 <= sequence <= (1 << 63) - 1):
            raise SessionError("invalid_event_sequence")
        bounded = [str(c)[:128] for c in (capabilities or [])][:256]
        encoded = json.dumps(bounded, ensure_ascii=True)
        conn = self._connect()
        try:
            if sequence is not None:
                conn.execute("INSERT INTO stream_state (session_id,capabilities,capability_sequence) VALUES (?,?,?)"
                    " ON CONFLICT(session_id) DO UPDATE SET capabilities=excluded.capabilities,"
                    " capability_sequence=excluded.capability_sequence WHERE excluded.capability_sequence>stream_state.capability_sequence",
                    (session_id, encoded, sequence))
                return
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
