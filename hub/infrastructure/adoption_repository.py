"""Independent hub AdoptionRepository (Task 4).

Owns agent adoption records ("纳管记录") in their own SQLite database.  This
store is separate from the legacy observation JSONL, the ``events.jsonl`` log,
the session store, and the task SQLite, and it never issues commands.

Design rules enforced here:

- WAL mode, ``timeout=10``, ``isolation_level=None`` and a fail-closed
  connection exactly like :mod:`hub.infrastructure.session_repository`;
- a schema-version table plus an ``adoptions`` table whose CHECK constraints
  mirror the domain's bounded-value rules at the storage layer;
- upsert is idempotent on ``session_id`` (one row per adopted session) and
  validates the whole payload before any SQL is executed;
- status transitions are atomic: a single ``UPDATE`` whose WHERE clause
  permits only ``pending -> adopted -> revoked`` plus idempotent same-status;
  a revoked row is retained and is terminal;
- revocation is a CAS: ``revoke_cas`` transitions ``adopted -> revoked`` in one
  bounded UPDATE and reports exactly which caller won the transition, so the
  service can issue the one detach for a seat exactly once under races (a
  concurrent operator ``revoke`` and a probe-guard ``drift`` auto-revoke can
  never enqueue a duplicate detach);
- session ids are gatekept by the shared ``is_valid_session_id`` boundary;
- every sqlite failure is translated to a bounded code-only
  :class:`AdoptionRepositoryError` that never embeds paths, exception text,
  or raw input.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from session_schema import CAPTURE_QUALITIES, is_valid_session_id

from hub.domain.adoption import (
    Adoption,
    allowed_old_statuses,
    normalize_adoption_spec,
)

SCHEMA_VERSION = 1

_NOW_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


class AdoptionRepositoryError(RuntimeError):
    """Bounded adoption repository error: a stable short code only.

    ``str(err)`` and ``code`` carry the bounded code only  - never paths,
    exception text, raw input, or key material.
    """

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime(_NOW_FMT)


def _sql_row(row: Mapping[str, Any] | sqlite3.Row) -> Adoption:
    """Decode a row into a frozen :class:`Adoption`."""
    return Adoption(
        adoption_id=str(row["adoption_id"]),
        machine_id=str(row["machine_id"]),
        session_id=str(row["session_id"]),
        pid=int(row["pid"]),
        pgid=int(row["pgid"]) if row["pgid"] is not None else None,
        started_at=str(row["started_at"] or ""),
        exe_path=str(row["exe_path"] or ""),
        agent_family=str(row["agent_family"] or ""),
        native_file_path=(str(row["native_file_path"])
                          if row["native_file_path"] is not None else None),
        status=str(row["status"] or ""),
        capture_quality=str(row["capture_quality"] or ""),
        actor=str(row["actor"] or ""),
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
    )


class AdoptionRepository:
    """Durable, isolated adoption records for the discover/adopt surface.

    Constructor-injected ``db_path`` keeps the persistence layer free of
    module-level path lookups.  ``path`` aliases ``db_path`` so callers and
    tests can assert the store is separate from the legacy state tree.
    """

    SCHEMA_VERSION = SCHEMA_VERSION

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        # Public alias used by the isolation test in the brief.
        self.path = self.db_path

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

    def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = None
        try:
            conn = self._connect()
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS adoptions (
                    adoption_id TEXT NOT NULL UNIQUE,
                    machine_id TEXT NOT NULL,
                    session_id TEXT PRIMARY KEY,
                    pid INTEGER NOT NULL,
                    pgid INTEGER,
                    started_at TEXT NOT NULL,
                    exe_path TEXT NOT NULL,
                    agent_family TEXT NOT NULL DEFAULT '',
                    native_file_path TEXT,
                    status TEXT NOT NULL,
                    capture_quality TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (adoption_id <> ''),
                    CHECK (machine_id <> ''),
                    CHECK (session_id <> ''),
                    CHECK (pid > 0),
                    CHECK (pgid IS NULL OR pgid > 0),
                    CHECK (status IN ('pending', 'adopted', 'revoked')),
                    CHECK (capture_quality IN
                           ('exact', 'structured', 'best_effort'))
                );
                -- Fix I2: dedupe any pre-existing active rows (a store created
                -- before the uniqueness invariant existed may already hold
                -- several pending/adopted rows for one identity) so the unique
                -- partial index below can be built.  Keep the EARLIEST row per
                -- (machine_id, pid, started_at); the rest are removed.
                DELETE FROM adoptions
                 WHERE status IN ('pending', 'adopted')
                   AND rowid NOT IN (
                       SELECT MIN(rowid) FROM adoptions
                        WHERE status IN ('pending', 'adopted')
                        GROUP BY machine_id, pid, started_at
                   );
                -- ONE active adoption per process identity.  Partial: a
                -- pre-existing ``revoked`` row for that identity never blocks
                -- a fresh adopt.
                CREATE UNIQUE INDEX IF NOT EXISTS uq_adoption_active_identity
                    ON adoptions (machine_id, pid, started_at)
                    WHERE status IN ('pending', 'adopted');
                CREATE INDEX IF NOT EXISTS idx_adoptions_machine
                    ON adoptions(machine_id);
                CREATE INDEX IF NOT EXISTS idx_adoptions_session
                    ON adoptions(session_id);
                """
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value)"
                " VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),))
        except sqlite3.Error:
            raise AdoptionRepositoryError("adoption_store") from None
        finally:
            if conn is not None:
                conn.close()

    def schema_version(self) -> int:
        conn = None
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
        except sqlite3.Error:
            raise AdoptionRepositoryError("adoption_store") from None
        finally:
            if conn is not None:
                conn.close()
        if row is None:
            return 0
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return 0

    # -- internal helpers ------------------------------------------------------

    def _validate_session_id(self, session_id: Any) -> str:
        if not is_valid_session_id(session_id):
            raise AdoptionRepositoryError("invalid_adoption")
        return str(session_id)

    def _fetch_row(self, conn, session_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM adoptions WHERE session_id=?", (session_id,)
        ).fetchone()

    def _current_row(self, session_id: str) -> sqlite3.Row | None:
        conn = self._connect()
        try:
            return self._fetch_row(conn, session_id)
        finally:
            conn.close()

    # -- public API ---------------------------------------------------------------

    def upsert(self, adoption: Adoption | Mapping[str, Any]) -> Adoption:
        """Validate and upsert an adoption; returns the stored record.

        Idempotent on ``session_id``: a second upsert for the same session
        updates the existing row rather than duplicating it.  Any invalid
        payload (bad session id, unknown status/capture quality, non-positive
        pid) raises ``AdoptionRepositoryError("invalid_adoption")``.
        """
        try:
            clean = normalize_adoption_spec(adoption)
        except (ValueError, TypeError):
            raise AdoptionRepositoryError("invalid_adoption") from None
        session_id = clean["session_id"]
        now = _now_iso()
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO adoptions (adoption_id, machine_id, session_id,"
                " pid, pgid, started_at, exe_path, agent_family,"
                " native_file_path, status, capture_quality, actor, created_at,"
                " updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                " adoption_id=excluded.adoption_id,"
                " machine_id=excluded.machine_id,"
                " pid=excluded.pid,"
                " pgid=excluded.pgid,"
                " started_at=excluded.started_at,"
                " exe_path=excluded.exe_path,"
                " agent_family=excluded.agent_family,"
                " native_file_path=excluded.native_file_path,"
                " status=excluded.status,"
                " capture_quality=excluded.capture_quality,"
                " actor=excluded.actor,"
                " updated_at=?"
                " WHERE adoptions.status = excluded.status"
                "    OR (adoptions.status = 'pending'"
                "        AND excluded.status = 'adopted')"
                "    OR (adoptions.status = 'adopted'"
                "        AND excluded.status = 'revoked')",
                (clean["adoption_id"], clean["machine_id"], session_id,
                 clean["pid"], clean["pgid"], clean["started_at"],
                 clean["exe_path"], clean["agent_family"],
                 clean["native_file_path"], clean["status"],
                 clean["capture_quality"], clean["actor"],
                 clean["created_at"], clean["updated_at"], now),
            )
            row = self._fetch_row(conn, session_id)
        except sqlite3.IntegrityError:
            raise AdoptionRepositoryError("invalid_adoption") from None
        except sqlite3.Error:
            raise AdoptionRepositoryError("adoption_store") from None
        finally:
            conn.close()
        if row is None:
            raise AdoptionRepositoryError("adoption_store")
        # A conflicting session row whose status would regress or skip a link
        # of the pending -> adopted -> revoked chain leaves the DO UPDATE
        # skipped by its WHERE guard (rowcount == 0) while the stored status
        # stays unchanged; surface the same bounded code as update_status() so
        # an upsert can never bypass the transition chain.
        if cur.rowcount == 0 and row["status"] != clean["status"]:
            raise AdoptionRepositoryError("invalid_status_transition")
        return _sql_row(row)

    def get(self, session_id: str) -> Adoption | None:
        self._validate_session_id(session_id)
        conn = self._connect()
        try:
            row = self._fetch_row(conn, session_id)
        except sqlite3.Error:
            raise AdoptionRepositoryError("adoption_store") from None
        finally:
            conn.close()
        if row is None:
            return None
        return _sql_row(row)

    def list(self, machine_id: str) -> list[Adoption]:
        """All records for a machine (including revoked, retained)."""
        if not isinstance(machine_id, str) or not machine_id:
            raise AdoptionRepositoryError("invalid_adoption")
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM adoptions WHERE machine_id=?"
                " ORDER BY updated_at DESC, session_id",
                (machine_id,)).fetchall()
        except sqlite3.Error:
            raise AdoptionRepositoryError("adoption_store") from None
        finally:
            conn.close()
        return [_sql_row(row) for row in rows]

    def list_active(self, machine_id: str) -> list[Adoption]:
        """Only currently-in-force (``adopted``) records for a machine."""
        if not isinstance(machine_id, str) or not machine_id:
            raise AdoptionRepositoryError("invalid_adoption")
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM adoptions WHERE machine_id=?"
                " AND status='adopted' ORDER BY created_at DESC, session_id",
                (machine_id,)).fetchall()
        except sqlite3.Error:
            raise AdoptionRepositoryError("adoption_store") from None
        finally:
            conn.close()
        return [_sql_row(row) for row in rows]

    def update_status(self, session_id: str, new_status: str) -> Adoption:
        """Atomically transition a record to ``new_status``.

        Only ``pending -> adopted -> revoked`` (plus idempotent same-status) is
        permitted; the transition is a single parameterized UPDATE whose WHERE
        clause carries the allowed predecessor set, so a disallowed or unknown
        transition changes nothing and raises a bounded code:
        ``invalid_status_transition`` for an illegal/unknown transition and
        ``invalid_adoption`` for a missing session row.
        """
        session_id = self._validate_session_id(session_id)
        if not isinstance(new_status, str) or not new_status:
            raise AdoptionRepositoryError("invalid_status_transition")
        try:
            old_allowed = allowed_old_statuses(new_status)
        except ValueError:
            raise AdoptionRepositoryError("invalid_status_transition") from None
        now = _now_iso()
        conn = self._connect()
        try:
            placeholders = ",".join("?" for _ in old_allowed)
            cur = conn.execute(
                "UPDATE adoptions SET"
                " status=?, updated_at=?"
                " WHERE session_id=? AND status IN (%s)" % placeholders,
                (new_status, now, session_id, *old_allowed),
            )
            if cur.rowcount == 1:
                row = self._fetch_row(conn, session_id)
            else:
                row = None
        except sqlite3.Error:
            raise AdoptionRepositoryError("adoption_store") from None
        finally:
            conn.close()
        if row is None:
            # Either the session does not exist, or the transition was
            # disallowed; distinguish so the caller gets a stable code.
            current = self._current_row(session_id)
            if current is None:
                raise AdoptionRepositoryError("invalid_adoption")
            raise AdoptionRepositoryError("invalid_status_transition")
        return _sql_row(row)

    def revoke_cas(self, session_id: str) -> tuple[Adoption, bool]:
        """Atomically capture the ``adopted -> revoked`` transition (CAS).

        The transition is ONE bounded ``UPDATE ... WHERE status='adopted'``.
        Returns ``(row, captured)`` where ``captured`` is True ONLY for the
        caller whose UPDATE actually performed the transition; a concurrent
        or later caller sees the already-revoked row with ``captured is
        False``.  The adoption service uses that flag to issue the one
        ``detach`` side effect exactly once per seat — two racing revokes /
        a racing operator revoke + probe-guard drift can never double-enqueue
        a detach.

        - missing session  -> ``invalid_adoption`` (nothing changed);
        - still-``pending`` row -> ``invalid_status_transition`` (the domain
          forbids ``pending -> revoked``; nothing changed);
        - already-``revoked`` row -> ``(row, False)`` (idempotent, no change);
        - a fresh ``adopted`` row -> ``(row, True)``.

        SQLite serializes the writes, so under concurrency exactly one caller
        observes ``captured is True`` regardless of interleaving.
        """
        session_id = self._validate_session_id(session_id)
        now = _now_iso()
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE adoptions SET status='revoked', updated_at=?"
                " WHERE session_id=? AND status='adopted'",
                (now, session_id),
            )
            if cur.rowcount == 1:
                # This caller captured the transition — return the post-state.
                row = self._fetch_row(conn, session_id)
                if row is None:
                    raise AdoptionRepositoryError("adoption_store")
                return _sql_row(row), True
            # No transition captured: report the current settled state.
            row = self._fetch_row(conn, session_id)
            if row is None:
                raise AdoptionRepositoryError("invalid_adoption")
            if row["status"] == "revoked":
                # Idempotent re-confirmation: some other call won the CAS.
                return _sql_row(row), False
            # A still-pending row is not revocable; nothing was mutated.
            raise AdoptionRepositoryError("invalid_status_transition")
        except sqlite3.Error:
            raise AdoptionRepositoryError("adoption_store") from None
        finally:
            conn.close()

    def delete_pending(self, session_id: str) -> bool:
        """Delete a still-``pending`` row (enqueue-failure compensation).

        Returns True only when a pending row was actually removed.  An
        ``adopted``/``revoked`` row is left untouched and returns False so a
        late compensating delete can never drop a seat that already moved
        forward.  A missing or malformed session id is ``invalid_adoption``.
        """
        session_id = self._validate_session_id(session_id)
        conn = self._connect()
        try:
            cur = conn.execute(
                "DELETE FROM adoptions WHERE session_id=? AND status='pending'",
                (session_id,),
            )
            if cur.rowcount == 1:
                return True
            row = self._fetch_row(conn, session_id)
        except sqlite3.Error:
            raise AdoptionRepositoryError("adoption_store") from None
        finally:
            conn.close()
        if row is None:
            raise AdoptionRepositoryError("invalid_adoption")
        return False

    def update_capture_quality(self, session_id: str, quality: str) -> Adoption:
        """Atomically switch a record's capture quality.

        ``quality`` must be a member of ``CAPTURE_QUALITIES``
        (``exact`` / ``structured`` / ``best_effort``); anything else raises
        ``invalid_capture_quality``.
        """
        session_id = self._validate_session_id(session_id)
        if not isinstance(quality, str) or quality not in CAPTURE_QUALITIES:
            raise AdoptionRepositoryError("invalid_capture_quality")
        now = _now_iso()
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE adoptions SET capture_quality=?, updated_at=?"
                " WHERE session_id=?",
                (quality, now, session_id),
            )
            if cur.rowcount == 1:
                row = self._fetch_row(conn, session_id)
            else:
                row = None
        except sqlite3.Error:
            raise AdoptionRepositoryError("adoption_store") from None
        finally:
            conn.close()
        if row is None:
            if self._current_row(session_id) is None:
                raise AdoptionRepositoryError("invalid_adoption")
            raise AdoptionRepositoryError("adoption_store")
        return _sql_row(row)


__all__ = [
    "SCHEMA_VERSION",
    "AdoptionRepository",
    "AdoptionRepositoryError",
]