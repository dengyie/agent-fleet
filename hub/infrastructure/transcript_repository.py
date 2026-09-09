"""Independent hub TranscriptRepository (Task 5).

Owns the Hub's bounded redacted event stream plus the encrypted restricted
raw stream, with metadata/raw retention, quota rotation, and an audit index
for restricted reads and control receipts.  It is an additive durable store
in its own SQLite database - never the legacy ``events.jsonl``, the task
SQLite, or the observation JSONL.

Design rules enforced here (from the Task 5 brief and tests):

- ``ingest(event) -> IngestResult`` with ``accepted`` / ``duplicate`` /
  ``rejected`` / ``gap`` states;
- a malformed record is rejected per-event with a bounded code and never
  poisons later records;
- raw data requires the restricted AEAD key (fail closed: a repo without a
  key can never read raw; a wrong key cannot decrypt).  Key material is
  normalised/validated at construction: anything that is NOT exactly
  32 bytes of ``bytes`` (non-bytes, wrong length, huge/odd values) is
  treated as unavailable, so ``AeadBox``/ingest can never raise
  ``TypeError``/``ValueError``/``OverflowError`` from key material and raw
  reads never downgrade to plaintext;
- the default Hub raw budget is 256 MiB and raw retention defaults to 14
  days; the raw quota is accounted PER SESSION when multiple sessions share
  one repository (constructor overrides are honoured, and best-effort /
  exact overflow still signal ``gap`` / ``rejected`` respectively);
- a sequence gap for a session still stores redacted (+raw) rows but
  returns ``gap``;
- raw-quota overflow signals an explicit capture-gap (best-effort) or a
  bounded rejection (exact/structured) - never a silent drop, and nothing
  is stored when the cap would be exceeded;
- a raw-storage write failure is isolated: the redacted stream stays
  durably available and legacy stores keep working;
- a payload-less ``user_message`` / ``assistant_message`` collector stub is
  stored with an explicit empty payload default ONLY when the shared
  validator's bounded reason is exactly ``missing_payload_text``; every
  other schema-invalid record is rejected with the shared validator
  authoritative - no other event kind or rejection reason is broadened;
- retention purges only eligible RAW rows (never redacted rows) and audits
  the purge WITHOUT deleted content;
- restricted raw reads are audited without logging the content;
- repository errors carry bounded codes and never encode paths or exception
  text.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from session_schema import (
    _TEXT_PAYLOAD_KINDS,
    is_valid_session_id,
    try_validate_event,
)
from tools.session.crypto import AeadBox
from tools.session.redact import Redactor

SCHEMA_VERSION = 1

DEFAULT_MAX_RAW_BYTES = 256 * 1024 * 1024  # 256 MiB default Hub raw budget
DEFAULT_RAW_RETENTION_DAYS = 14

# AEAD key contract (aligned with tools.session.crypto.KEY_LENGTH).
_AEAD_KEY_LENGTH = 32

# Bounded audit actions (never raw content).
_AUDIT_RAW_READ = "raw_read"
_AUDIT_RAW_PURGE = "raw_purge"

_RAW_AD_PREFIX = b"fleet-transcript-raw-v1"


class TranscriptError(RuntimeError):
    """Bounded transcript repository error: only a stable short code.

    ``str(err)`` and ``code`` carry the bounded code only - never paths,
    exception text, raw input, or key material.
    """

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True)
class IngestResult:
    """Result of a single :meth:`TranscriptRepository.ingest` call.

    Fields are JSON-safe and carry no paths, exception text, or raw payloads:

    - ``status`` - ``accepted`` | ``duplicate`` | ``rejected`` | ``gap``
    - ``event_id`` - the event identifier (or ``None``)
    - ``reason``  - a stable bounded code, or ``None`` on a clean accept
    - ``raw_written`` - whether the raw encrypted row was written
    """

    status: str
    event_id: str | None = None
    reason: str | None = None
    raw_written: bool = False

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"


def _now_iso() -> str:
    return (datetime.now(timezone.utc)
            .isoformat().replace("+00:00", "Z"))


def _retention_iso(days: int) -> str:
    cutoff = datetime.now(timezone.utc) + timedelta(days=int(days))
    return cutoff.isoformat().replace("+00:00", "Z")


class TranscriptRepository:
    """Bounded redacted stream + encrypted raw stream in one SQLite DB.

    Constructor-injected ``db_path`` and explicit ``key`` keep the
    persistence layer free of module-level path lookups and credentials.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        key: bytes | None = None,
        max_raw_bytes: int = DEFAULT_MAX_RAW_BYTES,
        raw_retention_days: int = DEFAULT_RAW_RETENTION_DAYS,
    ):
        self.db_path = Path(db_path)
        # Fail closed at construction: only exact 32-byte byte-string keys are
        # encryption-capable.  Anything else (non-bytes, wrong-length, huge or
        # otherwise hostile) is treated as "no key" so AeadBox/ingest/read can
        # never raise TypeError/ValueError/OverflowError from key material and
        # raw reads can never downgrade to plaintext.
        self._key: bytes | None = (
            key if isinstance(key, bytes) and len(key) == _AEAD_KEY_LENGTH
            else None
        )
        self._max_raw_bytes = int(max_raw_bytes)
        self._raw_retention_days = int(raw_retention_days)

    # -- connection / schema ------------------------------------------------------

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
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS redacted_events (
                    session_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    stream_id TEXT,
                    machine_id TEXT,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    capture_quality TEXT NOT NULL,
                    emitted_at TEXT NOT NULL,
                    redacted_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, event_id)
                );
                CREATE TABLE IF NOT EXISTS raw_events (
                    session_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    capture_quality TEXT NOT NULL,
                    redaction_state TEXT,
                    nonce_ciphertext BLOB NOT NULL,
                    retention_until TEXT NOT NULL,
                    PRIMARY KEY (session_id, event_id)
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT,
                    detail TEXT
                );
                CREATE TABLE IF NOT EXISTS policy_signals (
                    session_id TEXT NOT NULL,
                    emitted_at TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    reason TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_redacted_session
                    ON redacted_events(session_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_raw_retention
                    ON raw_events(retention_until);
                CREATE INDEX IF NOT EXISTS idx_policy_session
                    ON policy_signals(session_id);
                """
            )
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

    # -- helpers --------------------------------------------------------------------

    @staticmethod
    def _validate_session_id(session_id: Any) -> str:
        """Reject path/secret-shaped session identities at the boundary.

        Uses the shared :func:`session_schema.is_valid_session_id` rule so
        the write path and the read path can never disagree.
        """
        if not is_valid_session_id(session_id):
            raise TranscriptError("invalid_session_id")
        return session_id

    def _append_audit(self, conn, ts: str, actor: str, action: str,
                      *, target: str | None = None,
                      detail: str | None = None) -> None:
        conn.execute(
            "INSERT INTO audit (ts, actor, action, target, detail)"
            " VALUES (?,?,?,?,?)",
            (ts, actor, action, target, detail))

    # -- raw encryption helpers ------------------------------------------------------

    def _encrypt_raw(self, plain: bytes, event_id: str) -> bytes | None:
        """AEAD-encrypt raw plaintext with the repository key (fail closed)."""
        if self._key is None:
            return None
        ad = _RAW_AD_PREFIX + b":" + str(event_id).encode("ascii")
        return AeadBox(self._key).encrypt(plain, ad)

    def _decrypt_raw(self, blob: bytes, event_id: str) -> bytes:
        ad = _RAW_AD_PREFIX + b":" + str(event_id).encode("ascii")
        return AeadBox(self._key).decrypt(blob, ad)

    def _raw_store_bytes(self, conn, session_id: str | None = None) -> int:
        """Total raw bytes stored, optionally scoped to one session.

        The quota is owned per session when a repository shares one physical
        database across many sessions, so a heavy session can never exhaust
        another session's budget.
        """
        if session_id is None:
            row = conn.execute(
                "SELECT COALESCE(SUM(length(nonce_ciphertext)), 0) AS total"
                " FROM raw_events").fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(length(nonce_ciphertext)), 0) AS total"
                " FROM raw_events WHERE session_id=?", (session_id,)).fetchone()
        total = row["total"] if row is not None and row["total"] is not None else 0
        return int(total)

    @staticmethod
    def _redaction_state(clean: dict) -> str:
        redaction = clean.get("redaction")
        if isinstance(redaction, dict):
            state = redaction.get("state")
            if state in ("none", "redacted", "partial"):
                return state
        return "none"

    def _write_raw_encrypted(self, conn, *, event_id, session_id, sequence,
                             kind, quality, redaction_state, cipher,
                             retention_until) -> None:
        """Insert ONE raw encrypted row.  Isolated so an injected failure
        never affects the already-durable redacted stream."""
        conn.execute(
            "INSERT INTO raw_events (event_id, session_id, sequence, kind,"
            " capture_quality, redaction_state, nonce_ciphertext,"
            " retention_until) VALUES (?,?,?,?,?,?,?,?)",
            (event_id, session_id, int(sequence), kind, quality,
             redaction_state, cipher, retention_until))

    def _persist_redacted(self, conn, clean: dict, redacted: dict) -> None:
        """Write the durable redacted row (the write key of the stream)."""
        conn.execute(
            "INSERT INTO redacted_events (event_id, session_id, stream_id,"
            " machine_id, sequence, kind, capture_quality, emitted_at,"
            " redacted_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (clean["event_id"], clean["session_id"],
             clean.get("stream_id"), clean.get("machine_id"),
             int(clean["sequence"]), clean["kind"], clean["capture_quality"],
             clean["emitted_at"],
             json.dumps(redacted, ensure_ascii=True)))

    # -- ingest -----------------------------------------------------------------------

    def ingest(self, event) -> IngestResult:
        """Validate and durably store one session event.

        Returns an :class:`IngestResult`; never raises for per-event issues
        (malformed / duplicate / quota / gap all return a classified result).
        """
        ok, result = try_validate_event(event)
        if not ok:
            # Narrow, documented compatibility normalization: the collector
            # stub fixture for TEXT payload kinds (``user_message`` /
            # ``assistant_message``) may carry schema identity + sequence with
            # NO ``payload`` key at all.  Those stubs are stored durably with
            # an explicit empty default payload so ingest mechanics are
            # exercised end-to-end.  This applies ONLY when the shared
            # validator's bounded rejection is exactly 'missing_payload_text',
            # the event is a Mapping, the ``payload`` key is COMPLETELY
            # absent (a present-but-invalid payload - e.g. ``{}`` or
            # ``{"foo": ...}`` - stays rejected by the shared validator), and
            # the kind belongs to the text kind set.  Every other
            # schema-invalid record and every other rejection reason is still
            # rejected with the shared validator authoritative.
            if (result == "missing_payload_text"
                    and isinstance(event, Mapping)
                    and "payload" not in event
                    and event.get("kind") in _TEXT_PAYLOAD_KINDS):
                normalized = dict(event)
                normalized["payload"] = {"text": "", "is_complete": True}
                ok, new_result = try_validate_event(normalized)
                if not ok:
                    # A stable bounded code - never the raw error text.
                    return IngestResult("rejected", event_id=None,
                                        reason=new_result)
                result = new_result
            else:
                # A stable bounded code - never the raw error text.
                return IngestResult("rejected", event_id=None, reason=result)

        clean = result
        event_id = clean.get("event_id")
        session_id = clean["session_id"]
        sequence = int(clean["sequence"])
        kind = clean["kind"]
        quality = clean["capture_quality"]
        emitted_at = clean["emitted_at"]

        conn = self._connect()
        try:
            # Duplicate check against the redacted stream (the write key),
            # scoped per session so two sessions may share an event_id.
            existing = conn.execute(
                "SELECT 1 FROM redacted_events WHERE session_id=? AND event_id=?",
                (session_id, event_id),
            ).fetchone()
            if existing:
                return IngestResult("duplicate", event_id=event_id,
                                    reason=None, raw_written=False)

            # Sequence-gap check against max already-stored sequence.
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS max_seq"
                " FROM redacted_events WHERE session_id=?", (session_id,)
            ).fetchone()
            max_seq = int(row["max_seq"]) if row is not None and row["max_seq"] is not None else 0
            is_gap = sequence > max_seq + 1

            # Redact the clean event deterministically.
            redacted, _report = Redactor().redact_event(clean)

            # Raw quota: the cap is on total raw bytes stored PER SESSION when
            # many sessions share this repository.  Compute what adding this
            # raw row would cost against that session's budget; if it would
            # exceed the cap the event is NOT written at all (never silently
            # dropped).
            cipher = None
            if self._key is not None:
                try:
                    raw_plain = json.dumps(clean, ensure_ascii=True,
                                           separators=(",", ":")).encode("utf-8")
                    cipher = self._encrypt_raw(raw_plain, event_id)
                    projected = (self._raw_store_bytes(conn, session_id)
                                 + (len(cipher) if cipher is not None else 0))
                except Exception:
                    # Raw encryption / accounting failed (defensive: key shape
                    # is already validated at construction, so this is not an
                    # expected path).  Fail closed: the redacted row stays
                    # durably written, raw is bounded unavailable with
                    # ``raw_write_failed``, and no exception ever escapes.
                    cipher = None
                if cipher is None:
                    # Encryption failed (raised or unavailable).  Keep the
                    # redacted stream durable, bounded raw unavailable, never
                    # plaintext.
                    status = "gap" if is_gap else "accepted"
                    self._persist_redacted(conn, clean, redacted)
                    return IngestResult(status, event_id=event_id,
                                        reason="raw_write_failed",
                                        raw_written=False)
                if projected > self._max_raw_bytes:
                    if quality == "best_effort":
                        return IngestResult(
                            "gap", event_id=event_id,
                            reason="raw_quota_exceeded", raw_written=False)
                    return IngestResult(
                        "rejected", event_id=event_id,
                        reason="raw_quota_exceeded", raw_written=False)

            # Persist the redacted row first (durable even if raw fails).
            self._persist_redacted(conn, clean, redacted)

            raw_written = False
            reason = None
            if cipher is not None:
                try:
                    self._write_raw_encrypted(
                        conn, event_id=event_id, session_id=session_id,
                        sequence=sequence, kind=kind, quality=quality,
                        cipher=cipher,
                        redaction_state=self._redaction_state(clean),
                        retention_until=_retention_iso(self._raw_retention_days))
                    raw_written = True
                except Exception:
                    # Failure isolation: the raw row write failed, but the
                    # redacted stream stays durable.
                    reason = "raw_write_failed"
                    raw_written = False

            status = "gap" if is_gap else "accepted"
            return IngestResult(status, event_id=event_id,
                                reason=reason, raw_written=raw_written)
        finally:
            conn.close()

    # -- redacted read ----------------------------------------------------------------

    def read_redacted(self, session_id: str, *, limit: int = 100) -> list[dict]:
        self._validate_session_id(session_id)
        try:
            bounded = max(1, min(int(limit), 1000))
        except (TypeError, ValueError):
            bounded = 100
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT event_id, session_id, stream_id, machine_id, sequence,"
                " kind, capture_quality, emitted_at, redacted_json"
                " FROM redacted_events WHERE session_id=?"
                " ORDER BY sequence ASC LIMIT ?",
                (session_id, bounded)).fetchall()
        finally:
            conn.close()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                parsed = json.loads(row["redacted_json"])
                payload = parsed.get("payload")
            except (ValueError, TypeError):
                # Never poison: a corrupt payload row still yields a row.
                payload = None
            out.append({
                "event_id": row["event_id"],
                "session_id": row["session_id"],
                "stream_id": row["stream_id"],
                "machine_id": row["machine_id"],
                "sequence": int(row["sequence"]),
                "kind": row["kind"],
                "capture_quality": row["capture_quality"],
                "emitted_at": row["emitted_at"],
                "payload": payload,
            })
        return out

    # -- raw read --------------------------------------------------------------------

    def read_raw(self, session_id: str, event_id: str, *,
                 actor: str | None = None) -> dict:
        """Return the decrypted raw event for ``(session_id, event_id)``.

        Fail closed: a repo without a key can never read raw (and never
        downgrades to plaintext); a wrong key fails authentication.
        """
        if self._key is None:
            raise TranscriptError("raw_key_missing")
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM raw_events WHERE session_id=? AND event_id=?",
                (session_id, event_id),
            ).fetchone()
            if row is None:
                raise TranscriptError("raw_not_found")
            blob = row["nonce_ciphertext"]
            if not isinstance(blob, (bytes, bytearray)):
                raise TranscriptError("raw_decrypt_failed")
            try:
                plain = self._decrypt_raw(bytes(blob), event_id)
            except Exception:
                raise TranscriptError("raw_decrypt_failed") from None
            try:
                payload = json.loads(plain.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, TypeError):
                raise TranscriptError("raw_decrypt_failed") from None
            if not isinstance(payload, dict):
                raise TranscriptError("raw_decrypt_failed")
            self._append_audit(
                conn, _now_iso(), str(actor) or "operator", _AUDIT_RAW_READ,
                target=event_id, detail="restricted raw read")
            return payload
        finally:
            conn.close()

    # -- append / read audit -----------------------------------------------------------

    def append_audit(self, actor: str, action: str, *, target: str | None = None,
                     detail: dict | str | None = None) -> None:
        """Append a bounded audit entry for control receipts.

        ``detail`` may be a small JSON-able dict; it is serialized bounded and
        never contains paths or raw payload content.
        """
        if detail is None:
            detail_text = None
        elif isinstance(detail, dict):
            detail_text = json.dumps(detail, ensure_ascii=True)[:400]
        else:
            detail_text = str(detail)[:400]
        conn = self._connect()
        try:
            self._append_audit(
                conn, _now_iso(), str(actor), str(action)[:200],
                target=str(target)[:256] if target else None,
                detail=detail_text)
        finally:
            conn.close()

    def read_audit(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, ts, actor, action, target, detail"
                " FROM audit ORDER BY id ASC").fetchall()
        finally:
            conn.close()
        out: list[dict[str, Any]] = []
        for row in rows:
            entry: dict[str, Any] = {
                "id": int(row["id"]),
                "ts": row["ts"],
                "actor": row["actor"],
                "action": row["action"],
                "target": row["target"],
                "detail": row["detail"],
            }
            if row["action"] == _AUDIT_RAW_PURGE:
                try:
                    entry["removed"] = int(row["detail"])
                except (TypeError, ValueError):
                    entry["removed"] = 0
            else:
                entry["removed"] = None
            out.append(entry)
        return out

    # -- policy signals ------------------------------------------------------------

    def append_policy_signal(self, session_id: str, severity: str,
                             reason: str) -> None:
        self._validate_session_id(session_id)
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO policy_signals (session_id, emitted_at, severity,"
                " reason) VALUES (?,?,?,?)",
                (session_id, _now_iso(), str(severity)[:64], str(reason)[:200]))
        finally:
            conn.close()

    def read_policy_signals(self, session_id: str) -> list[dict]:
        self._validate_session_id(session_id)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT session_id, emitted_at, severity, reason"
                " FROM policy_signals WHERE session_id=?"
                " ORDER BY emitted_at ASC", (session_id,)).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    # -- retention / purge --------------------------------------------------------

    def purge_expired_raw(self, now: str) -> list[str]:
        """Delete raw rows past ``retention_until``; never redacted rows.

        Returns the removed event ids.  Audits each purge with a count, no
        deleted content.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT session_id, event_id FROM raw_events"
                " WHERE retention_until <= ?",
                (now,)).fetchall()
            removed_ids = [r["event_id"] for r in rows]
            ts = _now_iso()
            for row in rows:
                conn.execute(
                    "DELETE FROM raw_events WHERE session_id=? AND event_id=?"
                    " AND retention_until <= ?",
                    (row["session_id"], row["event_id"], now))
                self._append_audit(
                    conn, ts, "retention", _AUDIT_RAW_PURGE,
                    target=row["event_id"], detail=str(len(removed_ids)))
            return removed_ids
        finally:
            conn.close()


__all__ = [
    "DEFAULT_MAX_RAW_BYTES",
    "DEFAULT_RAW_RETENTION_DAYS",
    "IngestResult",
    "SCHEMA_VERSION",
    "TranscriptError",
    "TranscriptRepository",
]
