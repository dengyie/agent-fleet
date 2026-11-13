"""Task 2 tests: probe payload publishes sanitized instances additively.

`build_payload` adds the ``instances`` list while keeping the legacy
``machine/agents/system/ts`` shape identical.
"""

from tools import probe_collectors


def test_build_payload_has_instances(monkeypatch):
    monkeypatch.setattr(probe_collectors, "discover_instances", lambda: [])
    monkeypatch.setattr(probe_collectors, "collect_local_profiles", lambda: [])
    payload = probe_collectors.build_payload("m1", ("codex",))
    assert payload["instances"] == []
    assert set(payload) == {
        "machine", "agents", "instances", "local_profiles", "system", "ts",
    }


def test_build_payload_publishes_discovered_instances(monkeypatch):
    instance = {
        "pid": 10, "pgid": 8, "exe_path": "/bin/codex", "cmdline": "codex",
        "agent_family": "codex", "native_file_path": "/tmp/native.jsonl",
        "started_at": "2026-08-30T00:00:00Z", "attachable": True,
        "secret": "drop",
    }
    monkeypatch.setattr(probe_collectors, "discover_instances", lambda: [instance])
    monkeypatch.setattr(probe_collectors, "collect_local_profiles", lambda: [])
    payload = probe_collectors.build_payload("m1", ("codex",))
    assert payload["instances"] == [{
        "pid": 10, "pgid": 8, "exe_path": "/bin/codex", "cmdline": "codex",
        "agent_family": "codex", "native_file_path": "/tmp/native.jsonl",
        "started_at": "2026-08-30T00:00:00Z", "attachable": True,
    }]
    # Legacy additive shape unchanged (same keys as before, plus instances).
    assert set(payload) == {
        "machine", "agents", "instances", "local_profiles", "system", "ts",
    }


def test_build_payload_keeps_legacy_keys(monkeypatch):
    monkeypatch.setattr(probe_collectors, "discover_instances", lambda: [])
    monkeypatch.setattr(probe_collectors, "collect_local_profiles", lambda: [])
    payload = probe_collectors.build_payload("m1", ("codex",))
    assert payload["machine"] == "m1"
    assert isinstance(payload["agents"], dict)
    assert isinstance(payload["system"], dict)
    assert isinstance(payload["ts"], int)
    assert payload["local_profiles"] == []


def test_build_payload_runs_discovery_via_sanitize(monkeypatch):
    # discover_instances returning raw dicts still sanitizes through allowlist.
    monkeypatch.setattr(probe_collectors, "discover_instances", lambda: [{
        "pid": 1, "pgid": 1, "exe_path": "/bin/true", "cmdline": "true",
        "agent_family": "generic", "native_file_path": None,
        "started_at": "", "attachable": False, "junk": "x",
    }])
    monkeypatch.setattr(probe_collectors, "collect_local_profiles", lambda: [])
    payload = probe_collectors.build_payload("m1", ["generic"])
    assert payload["instances"] == [{
        "pid": 1, "pgid": 1, "exe_path": "/bin/true", "cmdline": "true",
        "agent_family": "generic", "native_file_path": None,
        "started_at": "", "attachable": False,
    }]