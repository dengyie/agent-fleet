"""SQLite read model for bounded, redacted session transcript search.

The search store is deliberately independent from the transcript store.  It
accepts already-redacted event envelopes, keeps only a bounded text projection,
and never reads or stores encrypted/raw transcript data.  Search is an
optional feature: a disabled gate or an unavailable FTS5 build is represented
by a stable error code and cannot affect session ingest.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.session.redact import Redactor

SCHEMA_VERSION = 2
MIN_QUERY_CHARS = 2
MAX_QUERY_CHARS = 128
MAX_PAGE_SIZE = 50
MAX_SNIPPET_CHARS = 400
MAX_INDEX_TEXT_CHARS = 8192
MAX_CURSOR_CHARS = 4096
MAX_SQLITE_SEQUENCE = (1 << 63) - 1
MAX_QUERY_STEPS = 1000000
MAX_OPAQUE_ID_CHARS = 256
MAX_PENDING_EVENTS = 4096
MAX_PENDING_BYTES = 32 * 1024 * 1024

INDEXABLE_KINDS = frozenset(
    {"user_message", "assistant_message", "tool_call", "tool_result"}
)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_SECRET_RE = re.compile(
    r"(?i)\b(?:bearer|token|api[_-]?key|secret|password|authorization)"
    r"\s*[:=]\s*[^\s,;]+"
)
_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|"
    r"github_pat_[A-Za-z0-9_]{8,}|xox[baprs]-[A-Za-z0-9-]{8,}|"
    r"AKIA[0-9A-Z]{12,})(?![A-Za-z0-9])"
)
_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:/[A-Za-z0-9_.~+@%-]+)+(?:/[A-Za-z0-9_.~+@%-]+)*"
    r"|(?<![A-Za-z0-9])[A-Za-z]:\\[^\s\"'`]+"
)


class SessionSearchRepositoryError(RuntimeError):
    """Stable repository error without paths, SQL, or exception text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


def _rollback_quietly(conn: sqlite3.Connection | None) -> bool:
    if conn is None:
        return True
    try:
        conn.rollback()
    except sqlite3.Error:
        return False
    return True


def _env_enabled() -> bool:
    value = os.environ.get("AGENT_FLEET_SESSION_SEARCH_ENABLED", "")
    return value.strip().lower() in _TRUE_VALUES


def _safe_identifier(value: Any, *, required: bool = False) -> str | None:
    if not isinstance(value, str) or not value or len(value) > MAX_OPAQUE_ID_CHARS:
        if required:
            raise SessionSearchRepositoryError("invalid_event")
        return None
    if (any(char in "/\\" or char.isspace() or ord(char) < 32
            or 0xD800 <= ord(char) <= 0xDFFF for char in value)
            or _TOKEN_RE.search(value) or _SECRET_RE.search(value)):
        if required:
            raise SessionSearchRepositoryError("invalid_event")
        return None
    return value


def _safe_timestamp(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise SessionSearchRepositoryError("invalid_event")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise SessionSearchRepositoryError("invalid_event") from exc
    if parsed.tzinfo is None:
        raise SessionSearchRepositoryError("invalid_event")
    try:
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError) as exc:
        raise SessionSearchRepositoryError("invalid_event") from exc


def _sanitize_text(value: Any) -> str:
    """Bound and defense-in-depth redact a text projection.

    The upstream session schema already redacts payloads.  These replacements
    protect this independent read model if a malformed integration hands it a
    secret-shaped value anyway; the replacements are deterministic and keep
    the raw value out of both the FTS and bigram tables.
    """

    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFC", value)[:MAX_INDEX_TEXT_CHARS]
    text = _SECRET_RE.sub("[REDACTED]", text)
    text = _TOKEN_RE.sub("[REDACTED]", text)
    text = _ABSOLUTE_PATH_RE.sub("[REDACTED]", text)
    return "".join(ch for ch in text if (ch in "\t\n\r" or ord(ch) >= 32)
                   and not 0xD800 <= ord(ch) <= 0xDFFF)


def _payload_text(event: Mapping[str, Any], kind: str) -> str:
    redacted, _ = Redactor(passthrough=False).redact_event(event)
    payload = redacted.get("payload")
    if not isinstance(payload, Mapping):
        return ""
    if kind in {"user_message", "assistant_message"}:
        return _sanitize_text(payload.get("text"))
    # Tool payloads are already normalized by session_schema.  Include only
    # their bounded public fields; never stringify a nested/raw object.
    fields = ("tool_name", "arguments", "result", "digest", "status")
    return _sanitize_text(" ".join(
        str(payload[field])[:MAX_INDEX_TEXT_CHARS]
        for field in fields
        if isinstance(payload.get(field), (str, int, float))
    ))


def _bigrams(text: str) -> set[str]:
    return {text[index:index + 2] for index in range(max(0, len(text) - 1))}


def _encode_cursor(sort_key: tuple[str, int, str, str]) -> str:
    payload = json.dumps(list(sort_key), ensure_ascii=False, separators=(",", ":"))
    token = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    if len(token) > MAX_CURSOR_CHARS:
        raise SessionSearchRepositoryError("invalid_cursor")
    return token


def _decode_cursor(value: Any) -> tuple[str, int, str, str]:
    if not isinstance(value, str) or not value or len(value) > MAX_CURSOR_CHARS:
        raise SessionSearchRepositoryError("invalid_cursor")
    try:
        padded = value + "=" * (-len(value) % 4)
        parsed = json.loads(base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True))
        timestamp, sequence, session_id, event_id = parsed
        if not isinstance(timestamp, str) or isinstance(sequence, bool) or not isinstance(sequence, int):
            raise ValueError
        if not isinstance(session_id, str) or not isinstance(event_id, str):
            raise ValueError
    except (ValueError, TypeError, IndexError, UnicodeError, binascii.Error,
            json.JSONDecodeError) as exc:
        raise SessionSearchRepositoryError("invalid_cursor") from exc
    if not 1 <= sequence <= MAX_SQLITE_SEQUENCE or not _safe_identifier(session_id) or not _safe_identifier(event_id):
        raise SessionSearchRepositoryError("invalid_cursor")
    try:
        timestamp = _safe_timestamp(timestamp)
    except SessionSearchRepositoryError as exc:
        raise SessionSearchRepositoryError("invalid_cursor") from exc
    return timestamp, sequence, session_id, event_id


def _pending_event_key(session_id: str, event_id: str) -> str:
    """Build a collision-free opaque key for the recovery queue.

    Session and event identifiers are opaque and may legally contain ``:``;
    concatenating them with a delimiter would make distinct pairs collide.
    Length-prefixing before hashing keeps the SQLite key bounded and stable.
    """
    raw = f"{len(session_id)}:{session_id}{len(event_id)}:{event_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class SessionSearchRepository:
    """Durable FTS5/bigram index for the operator search surface."""

    def __init__(self, db_path: Path, *, enabled: bool | None = None):
        self.db_path = Path(db_path)
        self._enabled_override = enabled
        self._ready = False
        self._failure_code: str | None = None

    @property
    def enabled(self) -> bool:
        return _env_enabled() if self._enabled_override is None else bool(self._enabled_override)

    @property
    def available(self) -> bool:
        return self.enabled and self._ready and self._failure_code is None

    @property
    def failure_code(self) -> str | None:
        return self._failure_code

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            return conn
        except BaseException:
            conn.close()
            raise

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise SessionSearchRepositoryError("search_disabled")
        if self._failure_code == "search_disabled":
            self._failure_code = None
        if self._failure_code == "search_unavailable":
            self._failure_code = None
            self._ready = False
        if self._failure_code is not None:
            raise SessionSearchRepositoryError(self._failure_code)

    def _ensure_ready(self) -> None:
        self._require_enabled()
        if self._ready:
            return
        self.init()
        if not self._ready:
            raise SessionSearchRepositoryError("search_unavailable")

    def init(self) -> None:
        """Create the read model, failing closed if FTS5 trigram is absent."""

        if not self.enabled:
            self._ready = False
            self._failure_code = "search_disabled"
            return
        conn: sqlite3.Connection | None = None
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = self._connect()
            conn.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS search_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    folded_text TEXT NOT NULL,
                    machine_id TEXT,
                    project_id TEXT,
                    agent_family TEXT,
                    emitted_at TEXT NOT NULL,
                    UNIQUE(session_id, event_id)
                );
                CREATE INDEX IF NOT EXISTS idx_search_entries_sort
                    ON search_entries(emitted_at DESC, sequence DESC,
                                      session_id DESC, event_id DESC);
                CREATE INDEX IF NOT EXISTS idx_search_entries_session
                    ON search_entries(session_id);
                CREATE INDEX IF NOT EXISTS idx_search_entries_filters
                    ON search_entries(machine_id, project_id, agent_family);
                CREATE TABLE IF NOT EXISTS search_bigrams (
                    entry_id INTEGER NOT NULL,
                    gram TEXT NOT NULL,
                    PRIMARY KEY(entry_id, gram),
                    FOREIGN KEY(entry_id) REFERENCES search_entries(id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_search_bigrams_gram
                    ON search_bigrams(gram, entry_id);
                CREATE TABLE IF NOT EXISTS search_pending (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    event_json TEXT NOT NULL,
                    machine_id TEXT,
                    project_id TEXT,
                    agent_family TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            # Trigram support is intentional.  Falling back to a tokenized
            # FTS table would make short/non-Latin queries silently incomplete.
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5("
                "text, tokenize='trigram')"
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(search_entries)")}
            if "folded_text" not in columns:
                conn.execute("ALTER TABLE search_entries ADD COLUMN folded_text TEXT NOT NULL DEFAULT ''")
            version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if version is not None and int(version["value"]) < SCHEMA_VERSION:
                conn.execute("DELETE FROM search_bigrams")
                cursor = conn.execute("SELECT id,text,emitted_at FROM search_entries")
                for row in cursor:
                    text = _payload_text({"payload": {"text": row["text"]}}, "assistant_message")
                    conn.execute("UPDATE search_entries SET text=?,folded_text=?,emitted_at=? WHERE id=?",
                                 (text, text.casefold(), _safe_timestamp(row["emitted_at"]), row["id"]))
                    conn.execute("UPDATE search_fts SET text=? WHERE rowid=?", (text, row["id"]))
                    conn.executemany("INSERT INTO search_bigrams(entry_id,gram) VALUES (?,?)",
                                     ((row["id"], gram) for gram in _bigrams(text.casefold())))
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            conn.commit()
            self._ready = True
            self._failure_code = None
            self._replay_pending()
        except (sqlite3.Error, OSError, ValueError, SessionSearchRepositoryError) as exc:
            _rollback_quietly(conn)
            self._ready = False
            self._failure_code = "search_unavailable"
            raise SessionSearchRepositoryError("search_unavailable") from exc
        finally:
            if conn is not None:
                conn.close()

    def schema_version(self) -> int:
        self._ensure_ready()
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        except (sqlite3.Error, OSError) as exc:
            raise SessionSearchRepositoryError("search_unavailable") from exc
        finally:
            if conn is not None:
                conn.close()
        try:
            return int(row["value"]) if row else 0
        except (TypeError, ValueError) as exc:
            raise SessionSearchRepositoryError("search_unavailable") from exc

    def _queue_pending(self, event: Mapping[str, Any], *, machine_id: str | None,
                       project_id: str | None, agent_family: str | None) -> None:
        """Retain a bounded redacted projection for a later retry."""
        try:
            event_id = _safe_identifier(event.get("event_id"), required=True)
            session_id = _safe_identifier(event.get("session_id"), required=True)
            kind = event.get("kind")
            if kind not in INDEXABLE_KINDS:
                return
            text = _payload_text(event, kind)
            if not text:
                return
            sequence = event.get("sequence")
            if (isinstance(sequence, bool) or not isinstance(sequence, int)
                    or not 1 <= sequence <= MAX_SQLITE_SEQUENCE):
                return
            projection = {
                "event_id": event_id, "session_id": session_id,
                "sequence": sequence, "kind": kind,
                "emitted_at": str(event.get("emitted_at") or ""),
                "payload": ({"text": text} if kind in {"user_message", "assistant_message"}
                            else {"arguments": text}),
            }
            encoded = json.dumps(projection, ensure_ascii=True, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > MAX_PENDING_BYTES:
                return
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                event_key = _pending_event_key(session_id=session_id, event_id=event_id)
                if conn.execute("SELECT 1 FROM search_pending WHERE event_key=?", (event_key,)).fetchone():
                    conn.rollback()
                    return
                # Keep the recovery queue bounded even during a prolonged
                # index outage.  Evict the oldest rows before admitting the
                # newest projection; the authoritative transcript remains
                # available for a later explicit rebuild.
                while True:
                    count_row = conn.execute(
                        "SELECT COUNT(*) AS count, COALESCE(SUM(length(event_json)),0) AS bytes "
                        "FROM search_pending").fetchone()
                    count = int(count_row["count"] or 0)
                    size = int(count_row["bytes"] or 0)
                    if count < MAX_PENDING_EVENTS and size + len(encoded.encode("utf-8")) <= MAX_PENDING_BYTES:
                        break
                    oldest = conn.execute(
                        "SELECT id FROM search_pending ORDER BY id ASC LIMIT 1").fetchone()
                    if oldest is None:
                        break
                    conn.execute("DELETE FROM search_pending WHERE id=?", (oldest["id"],))
                conn.execute(
                    "INSERT OR IGNORE INTO search_pending(event_key,event_json,machine_id,project_id,agent_family,created_at) "
                    "VALUES (?,?,?,?,?,datetime('now'))",
                    (event_key, encoded,
                     _safe_identifier(machine_id), _safe_identifier(project_id), _safe_identifier(agent_family)))
                conn.commit()
            except Exception:
                _rollback_quietly(conn)
                raise
            finally:
                conn.close()
        except (TypeError, ValueError, OSError, sqlite3.Error, SessionSearchRepositoryError):
            return

    def _replay_pending(self) -> None:
        while True:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id,event_json,machine_id,project_id,agent_family FROM search_pending "
                    "ORDER BY id ASC LIMIT 100").fetchall()
            finally:
                conn.close()
            if not rows:
                return
            for row in rows:
                try:
                    event = json.loads(row["event_json"])
                    self.index_event(event, machine_id=row["machine_id"],
                                     project_id=row["project_id"], agent_family=row["agent_family"])
                except (SessionSearchRepositoryError, ValueError, TypeError):
                    return
                conn = self._connect()
                try:
                    conn.execute("DELETE FROM search_pending WHERE id=?", (row["id"],))
                finally:
                    conn.close()
    def index_event(
        self,
        event: Mapping[str, Any],
        *,
        machine_id: str | None = None,
        project_id: str | None = None,
        agent_family: str | None = None,
    ) -> bool:
        """Index one validated redacted event, idempotently.

        Returns ``True`` for a newly indexed document and ``False`` for an
        ignored kind/empty projection/duplicate.  Storage errors use a stable
        exception so callers can isolate the optional index from ingest.
        """

        self._ensure_ready()
        if not isinstance(event, Mapping):
            raise SessionSearchRepositoryError("invalid_event")
        kind = event.get("kind")
        if kind not in INDEXABLE_KINDS:
            return False
        event_id = _safe_identifier(event.get("event_id"), required=True)
        session_id = _safe_identifier(event.get("session_id"), required=True)
        sequence = event.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or not 1 <= sequence <= MAX_SQLITE_SEQUENCE:
            raise SessionSearchRepositoryError("invalid_event")
        emitted_at = _safe_timestamp(event.get("emitted_at"))
        text = _payload_text(event, kind)
        if not text:
            return False
        machine = _safe_identifier(machine_id or event.get("machine_id"))
        project = _safe_identifier(project_id or event.get("project_id"))
        family = _safe_identifier(agent_family or event.get("agent_family"))

        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT id FROM search_entries WHERE session_id=? AND event_id=?",
                (session_id, event_id),
            ).fetchone()
            if existing is not None:
                conn.rollback()
                return False
            cur = conn.execute(
                "INSERT INTO search_entries(session_id,event_id,sequence,kind,text,folded_text,"
                "machine_id,project_id,agent_family,emitted_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (session_id, event_id, sequence, kind, text, text.casefold(), machine, project, family, emitted_at),
            )
            entry_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO search_fts(rowid,text) VALUES (?,?)", (entry_id, text)
            )
            grams = _bigrams(text.casefold())
            if grams:
                conn.executemany(
                    "INSERT INTO search_bigrams(entry_id,gram) VALUES (?,?)",
                    ((entry_id, gram) for gram in grams),
                )
            conn.commit()
            return True
        except SessionSearchRepositoryError:
            raise
        except (sqlite3.Error, OSError) as exc:
            _rollback_quietly(conn)
            self._queue_pending(event, machine_id=machine_id,
                                project_id=project_id, agent_family=agent_family)
            self._failure_code = "search_unavailable"
            self._ready = False
            raise SessionSearchRepositoryError("search_unavailable") from exc
        finally:
            if conn is not None:
                conn.close()

    # Explicit aliases make the accepted-redacted boundary visible to callers.
    index_redacted_event = index_event
    add_event = index_event

    def index_events(self, events: list[Mapping[str, Any]]) -> int:
        self._ensure_ready()
        if not isinstance(events, list) or len(events) > 100:
            raise SessionSearchRepositoryError("invalid_batch")
        count = 0
        for event in events:
            count += int(self.index_event(event))
        return count

    def _sort_after_clause(
        self, after: str | None, params: list[Any]
    ) -> str:
        if not after:
            return ""
        timestamp, sequence, session_id, event_id = _decode_cursor(after)
        params.extend([timestamp, sequence, session_id, event_id])
        return "(e.emitted_at,e.sequence,e.session_id,e.event_id)<(?,?,?,?)"

    @staticmethod
    def _snippet(text: str, query: str) -> tuple[str, list[list[int]]]:
        folded_parts = [char.casefold() for char in text]
        positions = [index for index, part in enumerate(folded_parts) for _ in part]
        folded = "".join(folded_parts)
        needle = query.casefold()
        first = folded.find(needle)
        if first < 0:
            return text[:MAX_SNIPPET_CHARS], []
        start = max(0, positions[first] - (MAX_SNIPPET_CHARS - len(query)) // 2)
        snippet = text[start:start + MAX_SNIPPET_CHARS]
        spans: list[list[int]] = []
        offset = first
        while len(spans) < MAX_SNIPPET_CHARS // 2:
            found = folded.find(needle, offset)
            if found < 0:
                break
            left, right = positions[found], positions[found + len(needle) - 1] + 1
            if right > start + len(snippet):
                break
            if not spans or left - start >= spans[-1][1]:
                spans.append([left - start, right - start])
            offset = found + len(needle)
        return snippet, spans

    def search(
        self,
        query: str,
        *,
        machine_id: str | None = None,
        project_id: str | None = None,
        agent_family: str | None = None,
        from_time: str | None = None,
        to_time: str | None = None,
        limit: int = MAX_PAGE_SIZE,
        after: str | None = None,
    ) -> dict[str, Any]:
        """Search the redacted index with bounded filters and cursor paging."""

        self._ensure_ready()
        if not isinstance(query, str):
            raise SessionSearchRepositoryError("invalid_query")
        query = unicodedata.normalize("NFC", query).strip()
        if len(query) < MIN_QUERY_CHARS or len(query) > MAX_QUERY_CHARS:
            raise SessionSearchRepositoryError("invalid_query")
        if any((ord(char) < 32 and char not in "\t\n\r")
               or 0xD800 <= ord(char) <= 0xDFFF for char in query):
            raise SessionSearchRepositoryError("invalid_query")
        try:
            if isinstance(limit, bool) or (not isinstance(limit, (str, int))):
                raise ValueError
            bounded_limit = max(1, min(int(limit), MAX_PAGE_SIZE))
        except (TypeError, ValueError) as exc:
            raise SessionSearchRepositoryError("invalid_limit") from exc
        def filter_id(value: Any) -> str | None:
            if value is None or value == "":
                return None
            try:
                return _safe_identifier(value, required=True)
            except SessionSearchRepositoryError as exc:
                raise SessionSearchRepositoryError("invalid_filter") from exc

        machine = filter_id(machine_id)
        project = filter_id(project_id)
        family = filter_id(agent_family)
        if from_time is not None:
            try:
                from_time = _safe_timestamp(from_time)
            except SessionSearchRepositoryError as exc:
                raise SessionSearchRepositoryError("invalid_filter") from exc
        if to_time is not None:
            try:
                to_time = _safe_timestamp(to_time)
            except SessionSearchRepositoryError as exc:
                raise SessionSearchRepositoryError("invalid_filter") from exc

        if from_time is not None and to_time is not None and from_time > to_time:
            raise SessionSearchRepositoryError("invalid_filter")

        params: list[Any] = []
        where = ["1=1"]
        if machine:
            where.append("e.machine_id=?")
            params.append(machine)
        if project:
            where.append("e.project_id=?")
            params.append(project)
        if family:
            where.append("e.agent_family=?")
            params.append(family)
        if from_time is not None:
            where.append("e.emitted_at>=?")
            params.append(from_time)
        if to_time is not None:
            where.append("e.emitted_at<=?")
            params.append(to_time)
        use_bigrams = len(query) == 2
        if use_bigrams:
            grams = _bigrams(query.casefold())
            if not grams:
                raise SessionSearchRepositoryError("invalid_query")
            placeholders = ",".join("?" for _ in grams)
            where.append(
                "e.id IN (SELECT entry_id FROM search_bigrams WHERE gram IN ("
                f"{placeholders}) GROUP BY entry_id HAVING COUNT(DISTINCT gram)=?)"
            )
            params.extend(sorted(grams))
            params.append(len(grams))
            where.append("instr(e.folded_text, ?)>0")
            params.append(query.casefold())
        else:
            fts_query = '"' + query.replace('"', '""') + '"'
            where.append("e.id IN (SELECT rowid FROM search_fts WHERE search_fts MATCH ?)")
            params.append(fts_query)
        where.append(self._sort_after_clause(after, params))
        sql = (
            "SELECT e.session_id,e.event_id,e.sequence,e.kind,e.text,e.machine_id,"
            "e.project_id,e.agent_family,e.emitted_at FROM search_entries e WHERE "
            + " AND ".join(part for part in where if part)
            + " ORDER BY e.emitted_at DESC,e.sequence DESC,e.session_id DESC,e.event_id DESC LIMIT ?"
        )
        params.append(bounded_limit + 1)

        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            remaining_steps = MAX_QUERY_STEPS // 1000
            def bounded_query() -> int:
                nonlocal remaining_steps
                remaining_steps -= 1
                return int(remaining_steps <= 0)
            conn.set_progress_handler(bounded_query, 1000)
            rows = conn.execute(sql, params).fetchall()
        except (sqlite3.Error, OSError) as exc:
            raise SessionSearchRepositoryError("search_unavailable") from exc
        finally:
            if conn is not None:
                conn.close()

        has_more = len(rows) > bounded_limit
        rows = rows[:bounded_limit]
        items: list[dict[str, Any]] = []
        for row in rows:
            snippet, highlight = self._snippet(row["text"], query)
            items.append({
                "session_id": row["session_id"],
                "sequence": (int(row["sequence"]) if int(row["sequence"]) <= (1 << 53) - 1
                             else str(row["sequence"])),
                "kind": row["kind"],
                "snippet": snippet[:MAX_SNIPPET_CHARS],
                "highlight": highlight,
                "machine_id": row["machine_id"],
                "project_id": row["project_id"],
                "agent_family": row["agent_family"],
                "emitted_at": row["emitted_at"],
            })
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = _encode_cursor((
                last["emitted_at"], int(last["sequence"]),
                last["session_id"], last["event_id"],
            ))
        return {"items": items, "next_cursor": next_cursor}

    search_events = search

    def purge_before(self, emitted_before: str) -> int:
        """Remove indexed entries older than an RFC3339 retention boundary."""

        self._ensure_ready()
        cutoff = _safe_timestamp(emitted_before)
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM search_fts WHERE rowid IN "
                         "(SELECT id FROM search_entries WHERE emitted_at<?)", (cutoff,))
            removed = conn.execute("DELETE FROM search_entries WHERE emitted_at<?", (cutoff,)).rowcount
            conn.commit()
            return removed
        except (sqlite3.Error, OSError) as exc:
            _rollback_quietly(conn)
            raise SessionSearchRepositoryError("search_unavailable") from exc
        finally:
            if conn is not None:
                conn.close()

    def delete_event(self, session_id: str, event_id: str) -> bool:
        self._ensure_ready()
        sid = _safe_identifier(session_id, required=True)
        eid = _safe_identifier(event_id, required=True)
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id FROM search_entries WHERE session_id=? AND event_id=?",
                (sid, eid),
            ).fetchone()
            if row is None:
                conn.rollback()
                return False
            entry_id = int(row["id"])
            conn.execute("DELETE FROM search_fts WHERE rowid=?", (entry_id,))
            conn.execute("DELETE FROM search_entries WHERE id=?", (entry_id,))
            conn.commit()
            return True
        except (sqlite3.Error, OSError) as exc:
            _rollback_quietly(conn)
            raise SessionSearchRepositoryError("search_unavailable") from exc
        finally:
            if conn is not None:
                conn.close()

    def delete_session(self, session_id: str) -> int:
        self._ensure_ready()
        sid = _safe_identifier(session_id, required=True)
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM search_fts WHERE rowid IN "
                         "(SELECT id FROM search_entries WHERE session_id=?)", (sid,))
            removed = conn.execute("DELETE FROM search_entries WHERE session_id=?", (sid,)).rowcount
            conn.commit()
            return removed
        except (sqlite3.Error, OSError) as exc:
            _rollback_quietly(conn)
            raise SessionSearchRepositoryError("search_unavailable") from exc
        finally:
            if conn is not None:
                conn.close()


__all__ = [
    "INDEXABLE_KINDS",
    "MAX_PAGE_SIZE",
    "MAX_QUERY_CHARS",
    "MAX_SNIPPET_CHARS",
    "MIN_QUERY_CHARS",
    "SessionSearchRepository",
    "SessionSearchRepositoryError",
]
