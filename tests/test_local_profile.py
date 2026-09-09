"""Node-local cc-switch registry (Phase 5).

The executor may only flip ``is_current`` and never copies ``settings_config``
or credentials onto list rows, receipts, or Hub payloads.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from tools.local_profile import apply_local_profile, list_local_profiles


def _make_db(path: Path, rows) -> Path:
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE providers ("
        "id TEXT PRIMARY KEY, app_type TEXT, name TEXT, is_current INTEGER, "
        "settings_config TEXT, api_key TEXT)")
    con.executemany(
        "INSERT INTO providers (id, app_type, name, is_current, "
        "settings_config, api_key) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()
    return path


def test_list_never_leaks_secrets(tmp_path):
    db = _make_db(tmp_path / "cc.db", [
        ("ok-1", "codex", "Office", 1, '{"api_key":"SECRET"}', "SECRET"),
        ("token-leak", "codex", "Bad", 0, "{}", "SECRET"),
        ("ok-2", "claude", "Home", 0, "{}", None),
        ("skip-me", "unknown", "X", 0, "{}", None),
    ])
    rows = list_local_profiles(db)
    assert rows == [
        {"profile_id": "ok-1", "family": "codex", "label": "Office",
         "origin": "cc_switch", "current": True},
        {"profile_id": "ok-2", "family": "claude_code", "label": "Home",
         "origin": "cc_switch", "current": False},
    ]
    dumped = repr(rows)
    assert "SECRET" not in dumped
    assert "api_key" not in dumped
    assert "settings_config" not in dumped
    assert "token-leak" not in dumped


def test_list_missing_db_is_empty(tmp_path):
    assert list_local_profiles(tmp_path / "missing.db") == []


def test_apply_flips_current_pointer_only(tmp_path):
    db = _make_db(tmp_path / "cc.db", [
        ("p1", "codex", "A", 1, '{"api_key":"SECRET"}', "SECRET"),
        ("p2", "codex", "B", 0, "{}", None),
        ("c1", "claude", "C", 1, "{}", None),
    ])
    assert apply_local_profile("p2", family="codex", db_path=db) == "applied"
    con = sqlite3.connect(str(db))
    rows = {r[0]: r[1] for r in con.execute(
        "SELECT id, is_current FROM providers")}
    secrets = list(con.execute("SELECT api_key, settings_config FROM providers"))
    con.close()
    assert rows == {"p1": 0, "p2": 1, "c1": 1}
    assert ("SECRET", '{"api_key":"SECRET"}') in secrets


def test_apply_unknown_and_mismatch_fail_closed(tmp_path):
    db = _make_db(tmp_path / "cc.db", [
        ("p1", "codex", "A", 1, "{}", None),
    ])
    assert apply_local_profile("missing", family="codex", db_path=db) == \
        "unknown_profile"
    assert apply_local_profile("p1", family="claude_code", db_path=db) == \
        "family_mismatch"
    assert apply_local_profile("token-x", family="codex", db_path=db) == \
        "invalid_payload"
    con = sqlite3.connect(str(db))
    current = con.execute(
        "SELECT is_current FROM providers WHERE id = 'p1'").fetchone()[0]
    con.close()
    assert current == 1
