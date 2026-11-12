"""Platform initialization serializes schema inspection with additive migration."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from hub.infrastructure.platform_db import PlatformRepository, PlatformRepositoryError


ROOT = Path(__file__).resolve().parents[1]
MIGRATOR = """
import json, sqlite3, sys, time
from pathlib import Path
from hub.infrastructure.platform_db import PlatformRepository

database, signals, role, target = sys.argv[1:]
signals = Path(signals)
original_connect = sqlite3.connect

class ControlledConnection(sqlite3.Connection):
    def execute(self, sql, *args, **kwargs):
        if role == "first" and sql == target:
            (signals / "first-paused").write_text("ready", encoding="utf-8")
            deadline = time.monotonic() + 15
            while not (signals / "release-first").exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError("parent did not release migration")
                time.sleep(0.01)
        if role == "second" and sql == "BEGIN IMMEDIATE":
            # Signal before acquiring the lock: the first migration may own it.
            (signals / "second-requesting-lock").write_text("ready", encoding="utf-8")
        try:
            return super().execute(sql, *args, **kwargs)
        except sqlite3.Error as exc:
            print(json.dumps({"sql": sql, "error": str(exc)}), file=sys.stderr)
            raise

def connect(*args, **kwargs):
    return original_connect(*args, factory=ControlledConnection, **kwargs)

sqlite3.connect = connect
PlatformRepository(Path(database)).init()
print(json.dumps({"ok": True}))
"""


def wait_for_signal(path: Path, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 15
    while not path.exists():
        assert process.poll() is None, process.communicate(timeout=5)
        assert time.monotonic() < deadline, f"No migration signal: {path.name}"
        time.sleep(0.01)


@pytest.mark.parametrize("target", [
    "ALTER TABLE runs ADD COLUMN request_summary TEXT",
    "ALTER TABLE node_credentials ADD COLUMN fast_digest BLOB",
])
def test_platform_init_serializes_additive_inspection_across_processes(tmp_path: Path, target: str) -> None:
    database = tmp_path / "platform.db"
    processes = []
    try:
        first = subprocess.Popen([sys.executable, "-c", MIGRATOR, str(database), str(tmp_path), "first", target],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(first)
        wait_for_signal(tmp_path / "first-paused", first)
        second = subprocess.Popen([sys.executable, "-c", MIGRATOR, str(database), str(tmp_path), "second", target],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(second)
        wait_for_signal(tmp_path / "second-requesting-lock", second)
        (tmp_path / "release-first").write_text("ready", encoding="utf-8")
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stdout + stderr
            assert json.loads(stdout) == {"ok": True}
    finally:
        (tmp_path / "release-first").write_text("ready", encoding="utf-8")
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert "request_summary" in {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
        assert "fast_digest" in {row[1] for row in connection.execute("PRAGMA table_info(node_credentials)")}
    assert PlatformRepository(database).schema_version() == 3


def test_platform_init_rolls_back_additive_columns_when_later_migration_fails(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "platform.db"
    original_connect = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == "ALTER TABLE messages ADD COLUMN turn_sequence INTEGER NOT NULL DEFAULT 0":
                raise sqlite3.OperationalError("injected migration failure")
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: original_connect(*a, factory=FailingConnection, **kw))
    with pytest.raises(PlatformRepositoryError, match="platform_store"):
        PlatformRepository(database).init()
    with original_connect(database) as connection:
        assert "request_summary" not in {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
        assert "fast_digest" not in {row[1] for row in connection.execute("PRAGMA table_info(node_credentials)")}
        assert connection.execute("SELECT COUNT(*) FROM meta").fetchone()[0] == 0
    monkeypatch.setattr(sqlite3, "connect", original_connect)
    PlatformRepository(database).init()
    assert PlatformRepository(database).schema_version() == 3
