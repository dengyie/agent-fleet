"""Task 2 tests: sanitized instance metadata for the probe payload.

These tests pin the ``sanitize_instances`` contract from the
agent-discovery-adoption plan:

- output rows keep exactly the fixed allowlisted fields
  (``pid, pgid, exe_path, cmdline, agent_family, native_file_path,
  started_at, attachable``);
- unknown keys are dropped; bounded strings are truncated;
- malformed entries are rejected individually;
- ``build_payload`` publishes the sanitized list under an additive
  ``instances`` key.
"""

from report_schema import sanitize_instances


def test_sanitize_instances_allowlists_and_bounds():
    result = sanitize_instances([{
        "pid": 10, "pgid": 8, "exe_path": "/bin/codex",
        "cmdline": "x" * 500, "agent_family": "codex",
        "native_file_path": "/tmp/native.jsonl",
        "started_at": "2026-08-30T00:00:00Z", "attachable": True,
        "secret": "token=drop",
    }])
    assert set(result[0]) == {
        "pid", "pgid", "exe_path", "cmdline", "agent_family",
        "native_file_path", "started_at", "attachable",
    }
    assert len(result[0]["cmdline"]) <= 200
    assert "secret" not in result[0]


def test_sanitize_instances_rejects_non_dict_and_malformed_rows():
    assert sanitize_instances(None) == []
    assert sanitize_instances("x") == []
    assert sanitize_instances([None, 3, "s"]) == []
    # Missing required integer/pid fields -> row dropped.
    assert sanitize_instances([{
        "pid": "abc", "pgid": 1, "exe_path": "/bin/codex",
        "cmdline": "", "agent_family": "codex",
    }]) == []
    # Unknown agent_family -> row dropped.
    assert sanitize_instances([{
        "pid": 5, "pgid": 5, "exe_path": "/bin/codex", "cmdline": "",
        "agent_family": "editor", "native_file_path": None,
        "started_at": "", "attachable": True,
    }]) == []


def test_sanitize_instances_normalizes_int_bool_family():
    result = sanitize_instances([{
        "pid": "7", "pgid": "6", "exe_path": "/bin/codex", "cmdline": "codex",
        "agent_family": "codex", "native_file_path": "/tmp/n",
        "started_at": "2026-08-30T00:00:00Z", "attachable": 1,
    }])
    assert result[0]["pid"] == 7
    assert result[0]["pgid"] == 6
    assert result[0]["attachable"] is True
    for family in ("codex", "claude_code", "hermes", "generic"):
        row = sanitize_instances([{
            "pid": 1, "pgid": 1, "exe_path": "/bin/anything",
            "cmdline": "", "agent_family": family,
            "native_file_path": None, "started_at": "", "attachable": False,
        }])[0]
        assert row["agent_family"] == family


def test_sanitize_instances_truncates_bounded_strings():
    result = sanitize_instances([{
        "pid": 1, "pgid": 1, "exe_path": "/x" * 400, "cmdline": "y" * 300,
        "agent_family": "codex", "native_file_path": "/z" * 300,
        "started_at": "s" * 60, "attachable": True,
    }])
    row = result[0]
    assert len(row["exe_path"]) <= 200
    assert len(row["cmdline"]) <= 200
    assert len(row["native_file_path"]) <= 200
    assert len(row["started_at"]) <= 32


def test_sanitize_instances_drops_negative_pid_and_pgid_rows():
    # The frontend parseInstanceRow rejects pid < 0 / pgid < 0 and aborts the
    # whole machine DTO; the backend sanitizer must drop such rows at the
    # boundary so they never cross the wire.
    assert sanitize_instances([{
        "pid": -1, "pgid": 1, "exe_path": "/bin/bad", "cmdline": "",
        "agent_family": "codex", "native_file_path": None,
        "started_at": "", "attachable": True,
    }]) == []
    assert sanitize_instances([{
        "pid": 1, "pgid": -1, "exe_path": "/bin/bad", "cmdline": "",
        "agent_family": "codex", "native_file_path": None,
        "started_at": "", "attachable": True,
    }]) == []
    # String coercion of a negative value is rejected too.
    assert sanitize_instances([{
        "pid": "-2", "pgid": 1, "exe_path": "/bin/bad", "cmdline": "",
        "agent_family": "codex", "native_file_path": None,
        "started_at": "", "attachable": True,
    }]) == []


def test_sanitize_instances_keeps_good_rows_when_dropping_negative_subset():
    rows = sanitize_instances([
        {"pid": -1, "pgid": 1, "exe_path": "/bin/bad", "cmdline": "",
         "agent_family": "codex", "native_file_path": None,
         "started_at": "", "attachable": True},
        {"pid": 5, "pgid": 5, "exe_path": "/bin/ok", "cmdline": "",
         "agent_family": "codex", "native_file_path": None,
         "started_at": "", "attachable": True},
    ])
    assert len(rows) == 1
    assert rows[0]["pid"] == 5


def test_sanitize_instances_drops_unknown_keys_even_when_bool_present():
    row = sanitize_instances([{
        "pid": 1, "pgid": 1, "exe_path": "/bin/codex", "cmdline": "",
        "agent_family": "codex", "native_file_path": None,
        "started_at": "", "attachable": False, "secret_key": True,
        "process_handle": 99,
    }])[0]
    assert set(row) == {
        "pid", "pgid", "exe_path", "cmdline", "agent_family",
        "native_file_path", "started_at", "attachable",
    }