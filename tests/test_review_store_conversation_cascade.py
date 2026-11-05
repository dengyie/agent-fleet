"""Deleting an archived conversation must cascade every execution-window child."""

import sqlite3

from hub.infrastructure.execution_window_repository import (
    EXECUTION_WINDOW_CHILD_TABLES, ExecutionWindowRepository,
)
from hub.infrastructure.platform_db import PlatformRepository

OWNER = "owner@example.test"


def _fk_children_of_execution_windows(db_path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        return {
            table for table in tables
            if any(fk[2] == "execution_windows"
                   for fk in conn.execute(f"PRAGMA foreign_key_list({table})"))
        }


def test_delete_conversation_purges_frame_reads(tmp_path):
    repo = PlatformRepository(tmp_path / "platform.db")
    repo.init()
    windows = ExecutionWindowRepository(repo.db_path)
    windows.init()
    conversation = repo.create_conversation(OWNER, "conv-a", title="A", workspace_id=None)
    repo.append_turn(OWNER, conversation["conversation_id"], "msg-a", "run-a",
                            text="hi", client_token="tok-a", config_snapshot={}, now=1)
    run_id = "run-a"
    window = windows.create_window(OWNER, run_id)["window"]
    other_conv = repo.create_conversation(OWNER, "conv-b", title="B", workspace_id=None)
    repo.append_turn(OWNER, other_conv["conversation_id"], "msg-b", "run-b",
                     text="keep", client_token="tok-b", config_snapshot={}, now=2)
    kept = windows.create_window(OWNER, "run-b")["window"]
    with sqlite3.connect(repo.db_path) as conn:
        for wid in (window["window_id"], kept["window_id"]):
            conn.execute(
                "INSERT INTO execution_window_frame_reads(window_id,owner_id,last_read_at) "
                "VALUES(?,?,?)", (wid, OWNER, 100.0))
        conn.execute("UPDATE runs SET state='succeeded'")
    repo.set_conversation_archived(OWNER, "conv-a", archived=True, now=3)
    repo.delete_archived_conversation(OWNER, "conv-a")
    with sqlite3.connect(repo.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM conversations WHERE conversation_id='conv-a'").fetchone()[0] == 0
        assert conn.execute("SELECT window_id FROM execution_window_frame_reads").fetchall() == [(kept["window_id"],)]
        assert conn.execute("SELECT window_id FROM execution_windows").fetchall() == [(kept["window_id"],)]


def test_child_table_list_covers_every_foreign_key_to_execution_windows(tmp_path):
    db = tmp_path / "platform.db"
    PlatformRepository(db).init()
    ExecutionWindowRepository(db).init()
    assert _fk_children_of_execution_windows(db) <= set(EXECUTION_WINDOW_CHILD_TABLES)
    assert "execution_window_frame_reads" in EXECUTION_WINDOW_CHILD_TABLES
