"""Shared SQLite connection factory for the Hub repositories.

Every repository used to carry its own ``_connect`` copy with slightly
different WAL / busy-timeout / cleanup behaviour.  This module is the single
place that opens a connection:

* ``timeout`` is the SQLite busy timeout and also the overall deadline for the
  connection setup retry below;
* ``journal_mode=WAL`` negotiation can fail *immediately* with
  ``database is locked`` despite the busy timeout (it needs an exclusive lock
  when switching modes).  The idempotent setup is retried until the deadline
  after releasing every lock, instead of surfacing a spurious failure;
* a connection that fails during setup is always closed (never leaks a
  half-initialised handle, even on ``KeyboardInterrupt``).

Per-repository behaviour (row factory, foreign keys, isolation level, WAL) is
kept explicit through keyword arguments.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

#: Sentinel meaning "use the sqlite3 module default isolation level" (deferred
#: implicit transactions), as opposed to ``None`` (autocommit).
DEFAULT_ISOLATION = object()

_LOCKED_MESSAGES = ("database is locked", "database table is locked")


def is_locked_error(exc: BaseException) -> bool:
    """True for SQLITE_BUSY / SQLITE_LOCKED (transient lock contention)."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return (code & 0xFF) in (5, 6)
    return str(exc) in _LOCKED_MESSAGES


def connect(
    path: str | Path,
    *,
    timeout: float = 10.0,
    row_factory=sqlite3.Row,
    wal: bool = True,
    foreign_keys: bool = True,
    isolation_level=None,
    busy_timeout_ms: int | None = None,
    retry_locked: bool = True,
    uri: bool = False,
) -> sqlite3.Connection:
    """Open one configured connection; the caller owns closing it.

    ``isolation_level=None`` (the Hub default) is autocommit with explicit
    ``BEGIN IMMEDIATE`` transactions; pass :data:`DEFAULT_ISOLATION` for the
    sqlite3 module default.
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        kwargs = {
            "timeout": max(0.0, deadline - time.monotonic()) if retry_locked else timeout,
            "uri": uri,
        }
        if isolation_level is not DEFAULT_ISOLATION:
            kwargs["isolation_level"] = isolation_level
        conn = sqlite3.connect(str(path), **kwargs)
        try:
            if row_factory is not None:
                conn.row_factory = row_factory
            if busy_timeout_ms is not None:
                conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            if wal:
                conn.execute("PRAGMA journal_mode=WAL")
            if foreign_keys:
                conn.execute("PRAGMA foreign_keys=ON")
            return conn
        except sqlite3.OperationalError as exc:
            conn.close()
            remaining = deadline - time.monotonic()
            if not retry_locked or not is_locked_error(exc) or remaining <= 0:
                raise
            # Retry only this idempotent setup, after releasing all locks.
            time.sleep(min(0.01, remaining))
            if time.monotonic() >= deadline:
                raise
        except BaseException:
            conn.close()
            raise


__all__ = ["DEFAULT_ISOLATION", "connect", "is_locked_error"]
