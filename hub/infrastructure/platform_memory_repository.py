"""Owner-scoped durable MemoryItem persistence and bounded FTS search."""
from __future__ import annotations

import json
import math
import secrets
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from platform_schema import validate_id, validate_owner_id

MEMORY_KINDS = frozenset({"fact", "preference", "decision", "note"})
MAX_TITLE = 160
MAX_CONTENT_BYTES = 16 * 1024
MAX_TAGS = 16
MAX_TAG_BYTES = 64
MAX_SOURCE = 256
MAX_QUERY = 512
MAX_RESULT = 100


class PlatformMemoryRepositoryError(RuntimeError):
    def __init__(self, code: str):
        self.code = str(code)[:120]
        super().__init__(self.code)

    def __str__(self) -> str:
        return self.code


def _text(value: Any, *, field: str, limit: int, required: bool = True) -> str:
    if not isinstance(value, str) or (required and not value.strip()):
        raise PlatformMemoryRepositoryError(f"invalid_{field}")
    if len(value.encode("utf-8")) > limit:
        raise PlatformMemoryRepositoryError("value_too_large")
    return value.strip()


def _tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or len(value) > MAX_TAGS:
        raise PlatformMemoryRepositoryError("invalid_tags")
    result: list[str] = []
    for item in value:
        tag = _text(item, field="tag", limit=MAX_TAG_BYTES)
        if len(tag) > MAX_TAG_BYTES:
            raise PlatformMemoryRepositoryError("value_too_large")
        if tag not in result:
            result.append(tag)
    return result


def _normalize(data: Mapping[str, Any], *, memory_id: str | None = None) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise PlatformMemoryRepositoryError("invalid_memory")
    if memory_id is None:
        raw_id = data.get("memory_id") or f"memory_{secrets.token_hex(12)}"
    else:
        raw_id = memory_id
    try:
        normalized_id = validate_id(raw_id, "memory_id")
    except ValueError:
        raise PlatformMemoryRepositoryError("invalid_id") from None
    kind = data.get("kind")
    if kind not in MEMORY_KINDS:
        raise PlatformMemoryRepositoryError("invalid_kind")
    title = _text(data.get("title"), field="title", limit=MAX_TITLE)
    content = _text(data.get("content"), field="content", limit=MAX_CONTENT_BYTES)
    source = _text(data.get("source") or "manual", field="source", limit=MAX_SOURCE)
    return {
        "memory_id": normalized_id,
        "kind": kind,
        "title": title,
        "content": content,
        "tags": _tags(data.get("tags")),
        "source": source,
        "enabled": bool(data.get("enabled", True)),
    }


def _query_terms(query: Any) -> str:
    query = _text(query, field="query", limit=MAX_QUERY)
    # FTS5 operators and punctuation are deliberately removed. Search is a
    # conjunction of plain terms, so caller input cannot alter the MATCH AST.
    terms = []
    for raw in query.split():
        token = "".join(ch for ch in raw if ch.isalnum() or ch in "_-" )[:64]
        if token:
            terms.append(token.replace('"', ""))
    if not terms:
        raise PlatformMemoryRepositoryError("invalid_query")
    return " AND ".join(f'"{term}"' for term in terms[:16])


class PlatformMemoryRepository:
    def __init__(self, db_path: Path, *, clock=time.time):
        self.db_path = Path(db_path)
        self.clock = clock

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = None
        try:
            conn = self._connect()
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS platform_memory_items (
                    owner_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '[]',
                    source TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(owner_id, memory_id)
                );
                CREATE INDEX IF NOT EXISTS idx_platform_memory_owner
                    ON platform_memory_items(owner_id, enabled, updated_at DESC);
                CREATE VIRTUAL TABLE IF NOT EXISTS platform_memory_fts USING fts5(
                    memory_id UNINDEXED, owner_id UNINDEXED,
                    title, content, tags, source
                );
            """)
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).lower():
                raise PlatformMemoryRepositoryError("memory_search_unavailable") from None
            raise PlatformMemoryRepositoryError("memory_store") from None
        except (sqlite3.Error, OSError):
            raise PlatformMemoryRepositoryError("memory_store") from None
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        try:
            tags = json.loads(row["tags"] or "[]")
        except (TypeError, ValueError):
            tags = []
        return {
            "memory_id": row["memory_id"], "kind": row["kind"],
            "title": row["title"], "content": row["content"],
            "tags": list(tags) if isinstance(tags, list) else [],
            "source": row["source"], "enabled": bool(row["enabled"]),
            "revision": int(row["revision"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    @staticmethod
    def _owner(owner_id: Any) -> str:
        try:
            return validate_owner_id(owner_id)
        except ValueError:
            raise PlatformMemoryRepositoryError("invalid_owner") from None

    def create(self, owner_id: str, data: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
        owner_id = self._owner(owner_id)
        normalized = _normalize(data)
        timestamp = float(self.clock() if now is None else now)
        if not math.isfinite(timestamp):
            raise PlatformMemoryRepositoryError("invalid_time")
        encoded_tags = json.dumps(normalized["tags"], ensure_ascii=False, separators=(",", ":"))
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO platform_memory_items(owner_id,memory_id,kind,title,content,tags,source,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (owner_id, normalized["memory_id"], normalized["kind"], normalized["title"], normalized["content"], encoded_tags, normalized["source"], int(normalized["enabled"]), timestamp, timestamp),
            )
            conn.execute(
                "INSERT INTO platform_memory_fts(memory_id,owner_id,title,content,tags,source) VALUES(?,?,?,?,?,?)",
                (normalized["memory_id"], owner_id, normalized["title"], normalized["content"], " ".join(normalized["tags"]), normalized["source"]),
            )
            row = conn.execute("SELECT * FROM platform_memory_items WHERE owner_id=? AND memory_id=?", (owner_id, normalized["memory_id"])).fetchone()
            conn.execute("COMMIT")
            return self._public(row)
        except sqlite3.IntegrityError:
            if conn is not None:
                conn.execute("ROLLBACK")
            raise PlatformMemoryRepositoryError("memory_conflict") from None
        except (sqlite3.Error, OSError):
            if conn is not None:
                conn.execute("ROLLBACK")
            raise PlatformMemoryRepositoryError("memory_store") from None
        finally:
            if conn is not None:
                conn.close()

    def get(self, owner_id: str, memory_id: str) -> dict[str, Any] | None:
        owner_id = self._owner(owner_id)
        try:
            memory_id = validate_id(memory_id, "memory_id")
        except ValueError:
            raise PlatformMemoryRepositoryError("invalid_id") from None
        conn = None
        try:
            conn = self._connect()
            row = conn.execute("SELECT * FROM platform_memory_items WHERE owner_id=? AND memory_id=?", (owner_id, memory_id)).fetchone()
            return self._public(row) if row else None
        except sqlite3.Error:
            raise PlatformMemoryRepositoryError("memory_store") from None
        finally:
            if conn is not None:
                conn.close()

    def list(self, owner_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        owner_id = self._owner(owner_id)
        try:
            limit = max(1, min(int(limit), MAX_RESULT))
        except (TypeError, ValueError):
            raise PlatformMemoryRepositoryError("invalid_limit") from None
        conn = None
        try:
            conn = self._connect()
            rows = conn.execute("SELECT * FROM platform_memory_items WHERE owner_id=? ORDER BY updated_at DESC, memory_id LIMIT ?", (owner_id, limit)).fetchall()
            return [self._public(row) for row in rows]
        except sqlite3.Error:
            raise PlatformMemoryRepositoryError("memory_store") from None
        finally:
            if conn is not None:
                conn.close()

    def update(self, owner_id: str, memory_id: str, data: Mapping[str, Any], *, expected_revision: int, now: float | None = None) -> dict[str, Any]:
        current = self.get(owner_id, memory_id)
        if current is None:
            raise PlatformMemoryRepositoryError("memory_not_found")
        if type(expected_revision) is not int or expected_revision < 0:
            raise PlatformMemoryRepositoryError("invalid_revision")
        merged = dict(current)
        merged.update(dict(data or {}))
        merged["memory_id"] = memory_id
        normalized = _normalize(merged, memory_id=memory_id)
        timestamp = float(self.clock() if now is None else now)
        encoded_tags = json.dumps(normalized["tags"], ensure_ascii=False, separators=(",", ":"))
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            result = conn.execute(
                "UPDATE platform_memory_items SET kind=?,title=?,content=?,tags=?,source=?,enabled=?,revision=revision+1,updated_at=? WHERE owner_id=? AND memory_id=? AND revision=?",
                (normalized["kind"], normalized["title"], normalized["content"], encoded_tags, normalized["source"], int(normalized["enabled"]), timestamp, owner_id, memory_id, expected_revision),
            )
            if result.rowcount != 1:
                conn.execute("ROLLBACK")
                raise PlatformMemoryRepositoryError("revision_conflict")
            conn.execute("DELETE FROM platform_memory_fts WHERE owner_id=? AND memory_id=?", (owner_id, memory_id))
            conn.execute("INSERT INTO platform_memory_fts(memory_id,owner_id,title,content,tags,source) VALUES(?,?,?,?,?,?)", (memory_id, owner_id, normalized["title"], normalized["content"], " ".join(normalized["tags"]), normalized["source"]))
            row = conn.execute("SELECT * FROM platform_memory_items WHERE owner_id=? AND memory_id=?", (owner_id, memory_id)).fetchone()
            conn.execute("COMMIT")
            return self._public(row)
        except PlatformMemoryRepositoryError:
            raise
        except sqlite3.Error:
            if conn is not None:
                conn.execute("ROLLBACK")
            raise PlatformMemoryRepositoryError("memory_store") from None
        finally:
            if conn is not None:
                conn.close()

    def delete(self, owner_id: str, memory_id: str, *, expected_revision: int) -> bool:
        owner_id = self._owner(owner_id)
        try:
            memory_id = validate_id(memory_id, "memory_id")
        except ValueError:
            raise PlatformMemoryRepositoryError("invalid_id") from None
        if type(expected_revision) is not int or expected_revision < 0:
            raise PlatformMemoryRepositoryError("invalid_revision")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT 1 FROM platform_memory_items WHERE owner_id=? AND memory_id=? AND revision=?", (owner_id, memory_id, expected_revision)).fetchone()
            if row is None:
                exists = conn.execute("SELECT 1 FROM platform_memory_items WHERE owner_id=? AND memory_id=?", (owner_id, memory_id)).fetchone()
                conn.execute("ROLLBACK")
                if exists:
                    raise PlatformMemoryRepositoryError("revision_conflict")
                return False
            conn.execute("DELETE FROM platform_memory_items WHERE owner_id=? AND memory_id=? AND revision=?", (owner_id, memory_id, expected_revision))
            conn.execute("DELETE FROM platform_memory_fts WHERE owner_id=? AND memory_id=?", (owner_id, memory_id))
            conn.execute("COMMIT")
            return True
        except PlatformMemoryRepositoryError:
            raise
        except sqlite3.Error:
            if conn is not None:
                conn.execute("ROLLBACK")
            raise PlatformMemoryRepositoryError("memory_store") from None
        finally:
            if conn is not None:
                conn.close()

    def search(self, owner_id: str, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        owner_id = self._owner(owner_id)
        match = _query_terms(query)
        try:
            limit = max(1, min(int(limit), MAX_RESULT))
        except (TypeError, ValueError):
            raise PlatformMemoryRepositoryError("invalid_limit") from None
        conn = None
        try:
            conn = self._connect()
            rows = conn.execute(
                "SELECT m.* FROM platform_memory_fts JOIN platform_memory_items m ON m.owner_id=platform_memory_fts.owner_id AND m.memory_id=platform_memory_fts.memory_id WHERE platform_memory_fts.owner_id=? AND m.enabled=1 AND platform_memory_fts MATCH ? ORDER BY bm25(platform_memory_fts), m.updated_at DESC LIMIT ?",
                (owner_id, match, limit),
            ).fetchall()
            return [self._public(row) for row in rows]
        except sqlite3.OperationalError as exc:
            if "fts" in str(exc).lower() or "match" in str(exc).lower():
                raise PlatformMemoryRepositoryError("invalid_query") from None
            raise PlatformMemoryRepositoryError("memory_store") from None
        except sqlite3.Error:
            raise PlatformMemoryRepositoryError("memory_store") from None
        finally:
            if conn is not None:
                conn.close()
