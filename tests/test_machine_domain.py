"""Task 3 tests: public machine detail exposes metadata-only instances.

``public_machine_detail`` must add the sanitized ``instances`` list to
``current`` without removing any old fields.  The public read model re-runs the
Task 2 allowlist sanitizer so unknown keys (``conversation``), native content,
and private process handles never cross the HTTP boundary.  Missing instance
data degrades to an empty list, and snapshots from before the discovery rollout
remain fully readable.
"""

from hub.domain.machine import public_machine_detail

INSTANCE_KEYS = {
    "pid", "pgid", "exe_path", "cmdline", "agent_family",
    "native_file_path", "started_at", "attachable",
}


def snapshot_with_instances():
    return {
        "machine": "m1",
        "timestamp": "2026-08-30T00:00:00Z",
        "reachable": True,
        "remote_error": None,
        "agents": {"codex": {"installed": True}},
        "system": {"platform": "darwin", "command": "secret"},
        "instances": [{
            "pid": 7, "pgid": 6, "exe_path": "/bin/codex", "cmdline": "codex",
            "agent_family": "codex", "native_file_path": None,
            "started_at": "2026-08-30T00:00:00Z", "attachable": True,
            "conversation": [{"text": "private turn"}, {"text": "secret"}],
            "process_token": "drop-me",
        }],
        "_ts": 123,
    }


def test_machine_detail_exposes_metadata_only():
    detail = public_machine_detail(snapshot_with_instances(), [])
    assert detail["current"]["instances"][0]["agent_family"] == "codex"
    assert "conversation" not in repr(detail)


def test_machine_detail_sanitizes_instance_rows_at_public_boundary():
    detail = public_machine_detail(snapshot_with_instances(), [])
    row = detail["current"]["instances"][0]
    assert set(row) == INSTANCE_KEYS
    assert row["pid"] == 7
    assert row["attachable"] is True
    assert row["native_file_path"] is None
    assert "process_token" not in row


def test_machine_detail_keeps_old_fields_alongside_instances():
    detail = public_machine_detail(snapshot_with_instances(), [])
    # 不删除旧字段：agents/system 继续按既有 allowlist 暴露
    assert detail["current"]["agents"]["codex"]["installed"] is True
    assert detail["current"]["system"]["platform"] == "darwin"
    assert "command" not in detail["current"]["system"]
    assert detail["machine"] == "m1"
    assert detail["history"] == []


def test_machine_detail_defaults_instances_to_empty():
    detail = public_machine_detail({
        "machine": "m1", "reachable": True, "agents": {}, "system": {},
    }, [])
    assert detail["current"]["instances"] == []
    assert detail["current"]["local_profiles"] == []


def test_machine_detail_sanitizes_local_profiles_without_secrets():
    detail = public_machine_detail({
        "machine": "m1", "reachable": True, "agents": {}, "system": {},
        "local_profiles": [
            {"profile_id": "p1", "family": "codex", "label": "ok",
             "origin": "cc_switch", "current": True,
             "api_key": "secret", "settings_config": {"key": "drop"}},
            {"profile_id": "token-leak", "family": "codex", "label": "bad"},
        ],
    }, [])
    rows = detail["current"]["local_profiles"]
    assert rows == [{
        "profile_id": "p1", "family": "codex", "label": "ok",
        "origin": "cc_switch", "current": True,
    }]
    dumped = repr(detail)
    assert "api_key" not in dumped
    assert "secret" not in dumped
    assert "settings_config" not in dumped


def test_machine_detail_drops_negative_pid_and_pgid_rows():
    detail = public_machine_detail({
        "machine": "m1", "reachable": True, "agents": {}, "system": {},
        "instances": [
            {"pid": -3, "pgid": 1, "exe_path": "/bin/neg", "cmdline": "",
             "agent_family": "codex", "native_file_path": None,
             "started_at": "", "attachable": True},
            {"pid": 4, "pgid": -4, "exe_path": "/bin/neg2", "cmdline": "",
             "agent_family": "codex", "native_file_path": None,
             "started_at": "", "attachable": True},
            {"pid": 10, "pgid": 9, "exe_path": "/bin/ok", "cmdline": "",
             "agent_family": "codex", "native_file_path": None,
             "started_at": "", "attachable": True},
        ],
    }, [])
    rows = detail["current"]["instances"]
    assert len(rows) == 1
    assert rows[0]["pid"] == 10


def test_machine_detail_rejects_malformed_rows_but_keeps_good_rows():
    detail = public_machine_detail({
        "machine": "m1", "reachable": True, "agents": {}, "system": {},
        "instances": [
            {"pid": 1, "pgid": 1, "exe_path": "/bin/ok", "cmdline": "",
             "agent_family": "hermes", "native_file_path": None,
             "started_at": "", "attachable": True},
            {"pgid": 2, "exe_path": "/bin/bad", "cmdline": "",
             "agent_family": "codex"},
        ],
    }, [])
    rows = detail["current"]["instances"]
    assert len(rows) == 1
    assert rows[0]["pid"] == 1