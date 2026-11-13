"""Review regression: repositories share one SQLite connection factory."""
import importlib
import sqlite3
import threading
import time

import pytest

from hub.infrastructure import sqlite as shared

REPOS = [
    ("approval_repository", "ApprovalRepository", "wal"),
    ("command_repository", "CommandRepository", "wal"),
    ("incident_repository", "IncidentRepository", "wal"),
    ("platform_scheduler_repository", "PlatformSchedulerRepository", "wal"),
    ("service_repository", "ServiceRepository", "wal"),
    ("platform_schedule_repository", "PlatformScheduleRepository", "wal"),
    ("platform_memory_repository", "PlatformMemoryRepository", "wal"),
    ("usage_repository", "UsageRepository", "wal"),
    ("session_search_repository", "SessionSearchRepository", "wal"),
    ("session_repository", "SessionRepository", "wal"),
    ("supervisor_command_repository", "SupervisorCommandRepository", "wal"),
    ("browser_capture_gc", "BrowserCaptureGcRepository", "delete"),
    ("browser_repository", "BrowserRepository", "delete"),
]


@pytest.mark.parametrize("module_name,class_name,journal", REPOS)
def test_repository_connect_uses_shared_helper(tmp_path, monkeypatch, module_name, class_name, journal):
    module = importlib.import_module(f"hub.infrastructure.{module_name}")
    calls = []
    real = module.sqlite_connect

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, "sqlite_connect", spy)
    tmp_path.chmod(0o700)
    cls = getattr(module, class_name)
    extra = (tmp_path / "artifacts",) if class_name == "BrowserCaptureGcRepository" else ()
    repo = cls(tmp_path / "platform.db", *extra)
    conn = repo._connect()
    try:
        assert calls, "repository bypassed hub.infrastructure.sqlite.connect"
        assert conn.row_factory is sqlite3.Row
        assert conn.isolation_level is None
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == journal
    finally:
        conn.close()


def test_wal_setup_waits_out_a_transient_lock(tmp_path):
    path = tmp_path / "locked.db"
    holder = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    holder.execute("CREATE TABLE t(x)")
    holder.execute("BEGIN EXCLUSIVE")
    release = threading.Timer(0.3, lambda: holder.execute("COMMIT"))
    release.start()
    try:
        started = time.monotonic()
        conn = shared.connect(path, timeout=5)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        finally:
            conn.close()
        assert time.monotonic() - started >= 0.2
    finally:
        release.join()
        holder.close()


def test_failed_setup_closes_connection(tmp_path, monkeypatch):
    closed = []

    class Conn(sqlite3.Connection):
        def execute(self, sql, *args):
            if "journal_mode" in sql:
                raise sqlite3.DatabaseError("file is not a database")
            return super().execute(sql, *args)

        def close(self):
            closed.append(True)
            super().close()

    real_connect = sqlite3.connect
    monkeypatch.setattr(shared.sqlite3, "connect",
                        lambda *a, **k: real_connect(*a, factory=Conn, **k))
    with pytest.raises(sqlite3.DatabaseError):
        shared.connect(tmp_path / "bad.db")
    assert closed == [True]
