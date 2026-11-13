"""Upgrade actual pre-v4 databases, including interrupted table replacement."""

import contextlib
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hub.infrastructure import task_repository
from hub.infrastructure.task_repository import SqliteTaskRepository


@pytest.fixture
def old_database(tmp_path: Path) -> Path:
    path = tmp_path / "fleet.db"
    schema = Path(__file__).with_name("fixtures").joinpath("task_schema_pre_v4.sql")
    with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.executescript(schema.read_text())
        for name, state in (("pending", "queued"), ("active", "running"), ("done", "succeeded")):
            conn.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"t-{name}", f"c-{name}", "mac-local", "codex", "agent-fleet",
                 f"keep {name}", "owner@example.test", state, f"a-{name}",
                 "2026-09-08T00:00:00Z", "2099-01-01T00:00:00Z"),
            )
        conn.execute("INSERT INTO leases VALUES (?,?,?,?,?,?)", (
            "a-active", "t-active", "r1", "fixture-nonce", "2026-09-08T00:00:00Z", "2099-01-01T00:00:00Z"))
        conn.execute("INSERT INTO results VALUES (?,?,?,?,?,?,?)", (
            "a-done", "t-done", 0, "old output", "old diff", 1.0, "2026-09-08T00:00:01Z"))
        conn.execute("INSERT INTO result_files VALUES (?,?,?,?,?,?,?)", (
            "a-done", "t-done", "README.md", "saved", 0, 0, 5))
        conn.execute("INSERT INTO audit (ts, actor, action, task_id) VALUES (?,?,?,?)", (
            "2026-09-08T00:00:01Z", "owner@example.test", "create_task", "t-done"))
    return path


def _snapshot(path: Path) -> str:
    with contextlib.closing(sqlite3.connect(path)) as conn:
        return "\n".join(conn.iterdump())


def _assert_upgrade_preserves_data(repo: SqliteTaskRepository) -> None:
    with contextlib.closing(repo._connect()) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        assert [tuple(row) for row in conn.execute(
            "SELECT task_id, instruction, state FROM tasks ORDER BY task_id"
        )] == [("t-active", "keep active", "running"), ("t-done", "keep done", "succeeded"),
               ("t-pending", "keep pending", "queued")]
        result = conn.execute("SELECT * FROM results WHERE attempt_id='a-done'").fetchone()
        assert result["log_summary"] == "old output"
        assert result["diff_patch"] is None
        assert result["test_summary"] is None
        assert conn.execute("SELECT nonce FROM leases WHERE attempt_id='a-active'").fetchone()[0] == "fixture-nonce"
        assert conn.execute("SELECT content FROM result_files WHERE task_id='t-done'").fetchone()[0] == "saved"
        assert conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_gates").fetchone()[0] == 0
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='tasks_v4'").fetchone() is None
    row, changed = repo.pause_task("t-pending", "owner@example.test")
    assert changed and row["state"] == "paused"
    row, changed = repo.continue_task("t-pending", "owner@example.test")
    assert changed and row["state"] == "queued" and row["attempt_id"] != "a-pending"


def test_old_schema_upgrade_preserves_rows_and_supports_pause(old_database: Path) -> None:
    repo = SqliteTaskRepository(old_database)
    repo.init()
    upgraded = _snapshot(old_database)
    repo.init()
    assert _snapshot(old_database) == upgraded
    _assert_upgrade_preserves_data(repo)


@pytest.mark.parametrize("failure", ["copy", "drop", "rename"])
def test_failed_upgrade_rolls_back_and_can_retry(
    old_database: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    repo = SqliteTaskRepository(old_database)
    original = _snapshot(old_database)
    connect = repo._connect
    denied: list[bool] = []

    def authorize(action: int, first: str | None, second: str | None,
                  database: str | None, source: str | None) -> int:
        matches = {
            "copy": action == sqlite3.SQLITE_INSERT and first == "tasks_v4",
            "drop": action == sqlite3.SQLITE_DROP_TABLE and first == "tasks",
            "rename": action == sqlite3.SQLITE_ALTER_TABLE and second == "tasks_v4",
        }
        if matches[failure]:
            denied.append(True)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def failing_connect() -> sqlite3.Connection:
        conn = connect()
        conn.set_authorizer(authorize)
        return conn

    monkeypatch.setattr(repo, "_connect", failing_connect)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        repo.init()
    assert denied
    assert _snapshot(old_database) == original
    monkeypatch.setattr(repo, "_connect", connect)
    repo.init()
    _assert_upgrade_preserves_data(repo)


def test_process_exit_during_upgrade_recovers_old_database(old_database: Path) -> None:
    original = _snapshot(old_database)
    script = """
import os, sys
from pathlib import Path
from hub.infrastructure.task_repository import SqliteTaskRepository
repo = SqliteTaskRepository(Path(sys.argv[1]))
connect = repo._connect
def crash_before_rename(sql):
    if sql.startswith('ALTER TABLE tasks_v4 RENAME'):
        os._exit(73)
def crashing_connect():
    conn = connect()
    conn.set_trace_callback(crash_before_rename)
    return conn
repo._connect = crashing_connect
repo.init()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(old_database)],
        cwd=Path(task_repository.__file__).resolve().parents[2],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 73, result.stderr
    assert _snapshot(old_database) == original
    repo = SqliteTaskRepository(old_database)
    repo.init()
    _assert_upgrade_preserves_data(repo)


def test_concurrent_initializers_share_one_atomic_upgrade(old_database: Path) -> None:
    ready = threading.Barrier(4)

    def initialize() -> None:
        ready.wait(timeout=10)
        SqliteTaskRepository(old_database).init()

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(initialize) for _ in range(4)]
        for future in futures:
            future.result(timeout=20)
    _assert_upgrade_preserves_data(SqliteTaskRepository(old_database))


def test_invalid_foreign_keys_prevent_upgrade_commit(old_database: Path) -> None:
    with contextlib.closing(sqlite3.connect(old_database, isolation_level=None)) as conn:
        conn.execute("UPDATE results SET task_id='missing' WHERE attempt_id='a-done'")
    original = _snapshot(old_database)
    with pytest.raises(sqlite3.IntegrityError, match="foreign key"):
        SqliteTaskRepository(old_database).init()
    assert _snapshot(old_database) == original
