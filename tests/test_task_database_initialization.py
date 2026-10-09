"""Transient SQLite failures must never replace a healthy task database."""

import contextlib
import sqlite3
import time
from pathlib import Path

import pytest

from hub.infrastructure.task_repository import SqliteTaskRepository
from hub.infrastructure import task_repository


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "fleet.db"
    with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.executescript(Path(__file__).with_name("fixtures").joinpath("task_schema_pre_v4.sql").read_text())
        conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            "t-old", None, "mac-local", "codex", "agent-fleet", "preserve me",
            "owner@example.test", "queued", "a-old", "2026-09-08T00:00:00Z", "2099-01-01T00:00:00Z"))
    return path


def test_exclusive_lock_does_not_quarantine_healthy_database(
    database: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect = sqlite3.connect
    inode = database.stat().st_ino
    monkeypatch.setattr(task_repository, "SQLITE_CONNECT_TIMEOUT_S", 0.05)

    with contextlib.closing(connect(database, isolation_level=None)) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        try:
            started = time.monotonic()
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                SqliteTaskRepository(database).init()
            assert time.monotonic() - started < 1.0
            assert database.stat().st_ino == inode
            assert not database.with_suffix(".db.corrupt").exists()
        finally:
            writer.execute("ROLLBACK")
    repo = SqliteTaskRepository(database)
    repo.init()
    assert repo.get_task("t-old")["instruction"] == "preserve me"


@pytest.mark.parametrize("code", [5, 6, 8, 10, 13, 14, 23, 10 | (1 << 8), None])
def test_unavailable_database_preserves_original_file_and_error(
    database: Path, monkeypatch: pytest.MonkeyPatch, code: int | None,
) -> None:
    original = database.read_bytes()
    inode = database.stat().st_ino
    # The precise SQLite code wins over message text when the runtime supplies it.
    error = sqlite3.DatabaseError("file is not a database" if code is not None else "disk I/O error")
    if code is not None:
        error.sqlite_errorcode = code
    repo = SqliteTaskRepository(database)

    def unavailable() -> sqlite3.Connection:
        raise error

    monkeypatch.setattr(repo, "_connect", unavailable)
    with pytest.raises(sqlite3.DatabaseError) as failure:
        repo.init()
    assert failure.value is error
    assert database.stat().st_ino == inode
    assert database.read_bytes() == original
    assert not database.with_suffix(".db.corrupt").exists()


@pytest.mark.parametrize("pragma", ["journal_mode", "foreign_keys", "integrity_check"])
def test_failed_initialization_closes_each_open_connection(
    database: Path, monkeypatch: pytest.MonkeyPatch, pragma: str,
) -> None:
    connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []
    closed: list[sqlite3.Connection] = []

    class TrackedConnection(sqlite3.Connection):
        def close(self) -> None:
            closed.append(self)
            super().close()

    def authorize(action: int, first: str | None, second: str | None,
                  name: str | None, source: str | None) -> int:
        if action == sqlite3.SQLITE_PRAGMA and first == pragma:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def tracked_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        conn = connect(*args, **kwargs, factory=TrackedConnection)
        opened.append(conn)
        conn.set_authorizer(authorize)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            SqliteTaskRepository(database).init()
        assert opened and closed == opened
        assert not database.with_suffix(".db.corrupt").exists()
    finally:
        for conn in opened:
            if conn not in closed:
                conn.close()


@pytest.mark.parametrize("message", ["file is not a database", "database disk image is malformed"])
def test_python310_corruption_errors_keep_recovery_compatibility(
    database: Path, monkeypatch: pytest.MonkeyPatch, message: str,
) -> None:
    repo = SqliteTaskRepository(database)
    connect = repo._connect
    calls = 0
    original = database.read_bytes()

    def corrupt_then_connect() -> sqlite3.Connection:
        nonlocal calls
        calls += 1
        if calls == 1:
            # Python 3.10 exceptions have no sqlite_errorcode attribute.
            raise sqlite3.DatabaseError(message)
        return connect()

    monkeypatch.setattr(repo, "_connect", corrupt_then_connect)
    repo.init()
    assert database.with_suffix(".db.corrupt").read_bytes() == original
    assert repo.get_task("t-old") is None


def test_actual_non_database_file_is_preserved_before_rebuild(tmp_path: Path) -> None:
    path = tmp_path / "fleet.db"
    original = b"not a sqlite database at all"
    path.write_bytes(original)
    repo = SqliteTaskRepository(path)
    repo.init()
    assert path.with_suffix(".db.corrupt").read_bytes() == original
    assert repo.get_task("missing") is None


def test_extended_corruption_code_preserves_recovery(
    database: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = SqliteTaskRepository(database)
    connect = repo._connect
    original = database.read_bytes()
    failure = sqlite3.DatabaseError("damaged table")
    failure.sqlite_errorcode = 11 | (1 << 8)

    def corrupt_once() -> sqlite3.Connection:
        monkeypatch.setattr(repo, "_connect", connect)
        raise failure

    monkeypatch.setattr(repo, "_connect", corrupt_once)
    repo.init()
    assert database.with_suffix(".db.corrupt").read_bytes() == original
    assert repo.get_task("t-old") is None


@pytest.mark.parametrize("code,message", [(5, "database is locked"), (6, "database table is locked")])
def test_transient_wal_lock_retries_setup_with_closed_connections(
    database: Path, monkeypatch: pytest.MonkeyPatch, code: int, message: str,
) -> None:
    connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []
    closed: list[sqlite3.Connection] = []
    failures = 0

    class ContendedConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: tuple = ()) -> sqlite3.Cursor:
            nonlocal failures
            if sql == "PRAGMA journal_mode=WAL" and failures < 2:
                failures += 1
                error = sqlite3.OperationalError(message)
                error.sqlite_errorcode = code
                raise error
            return super().execute(sql, parameters)

        def close(self) -> None:
            closed.append(self)
            super().close()

    def contended_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        conn = connect(*args, **kwargs, factory=ContendedConnection)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", contended_connect)
    try:
        repo = SqliteTaskRepository(database)
        repo.init()
        assert failures == 2
        assert closed == opened
        assert not database.with_suffix(".db.corrupt").exists()
        assert repo.get_task("t-old")["instruction"] == "preserve me"
    finally:
        for conn in opened:
            if conn not in closed:
                conn.close()
