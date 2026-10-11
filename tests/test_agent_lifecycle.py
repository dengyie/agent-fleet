"""Lifecycle trust boundaries, precedence and durable migration regressions."""
import pytest
import sqlite3

from hub.infrastructure.session_repository import SessionRepository, SessionError
from session_schema import SessionValidationError, validate_event
from tools.session.bridge import SessionBridge
from tools.session.capture.hooks import HookAdapter
from tools.session.spool import LocalSpool


@pytest.fixture
def repo(tmp_path):
    result = SessionRepository(tmp_path / "sessions.db")
    result.init()
    result.upsert_session({"session_id": "s1", "machine_id": "m1",
                           "managed": True, "process_group_id": "g1",
                           "capture_quality": "structured"})
    return result


@pytest.fixture
def bridge_factory(tmp_path):
    spools = []
    def create(managed=True, **manifest):
        spool = LocalSpool(tmp_path / str(len(spools)), "m1", "s1", key=bytes(range(32)))
        spools.append(spool)
        bridge = SessionBridge.open(manifest, {"session_id": "s1", "machine_id": "m1",
            "stream_id": "st1", "managed": managed, "process_group_id": "g1", "spool": spool})
        return bridge, spool
    yield create
    for spool in spools:
        spool.close()


@pytest.mark.parametrize("hook,state", [("SessionStart", "running"),
    ("UserPromptSubmit", "running"), ("PreToolUse", "running"),
    ("PostToolUse", "running"), ("SessionEnd", "completed")])
def test_real_hook_name_and_state_id(hook, state):
    adapter = HookAdapter({"hooks": True})
    raw = {"hook_event_name": hook, "event_id": "evt_123", "prompt": "go", "tool_name": "Read"}
    event = adapter.state_event(raw)
    assert event["payload"]["state"] == state
    assert event["event_id"] != "evt_123"
    assert adapter.state_event(raw)["event_id"] == event["event_id"]


def test_bridge_binds_state_source_and_reason(bridge_factory):
    bridge, spool = bridge_factory(structured_stream=True)
    assert bridge.ingest_one({"agent_state": "running", "state_source": "supervisor",
        "state_reason": "/private/path SECRET=123"}, source="structured_stream")
    state = next(e for e in spool.read_after(0, 100, 1 << 20) if e["kind"] == "agent_state_changed")
    assert state["payload"]["source"] == "structured_stream"
    assert state["payload"]["reason"] == "explicit_state"


def test_unmanaged_state_degrades(bridge_factory):
    bridge, spool = bridge_factory(managed=False, hooks=True)
    bridge.ingest_one({"type": "PermissionRequest"}, source="hooks")
    state = next(e for e in spool.read_after(0, 100, 1 << 20) if e["kind"] == "agent_state_changed")
    assert state["capture_quality"] == "best_effort"
    assert state["payload"]["state"] == "unknown"
    assert state["payload"]["confidence"] == "low"


def test_hook_disconnect_emits_unknown(bridge_factory):
    bridge, spool = bridge_factory(hooks=True)
    bridge._emit_disconnect_gap("hooks", "/sensitive/error")
    states = [e for e in spool.read_after(0, 100, 1 << 20) if e["kind"] == "agent_state_changed"]
    assert states[-1]["payload"] == {"state": "unknown", "source": "hook",
        "confidence": "low", "reason": "source_disconnected"}


def test_priority_replay_and_restart(repo):
    repo.update_agent_state("s1", "running", "hook", "high", 5)
    repo.update_agent_state("s1", "waiting_for_user", "native_transcript", "high", 6)
    assert repo.get_session("s1")["current_state"] == "running"
    repo.update_agent_state("s1", "completed", "supervisor", "high", 4)
    assert repo.get_session("s1")["current_state"] == "running"
    repo.update_agent_state("s1", "completed", "supervisor", "high", 7)
    repo.init()
    assert repo.get_session("s1")["current_state"] == "completed"
    assert repo.get_session("s1")["state_sequence"] == 7


def test_pty_cannot_assert_wait_or_high_confidence(repo):
    with pytest.raises(SessionError):
        repo.update_agent_state("s1", "waiting_for_user", "pty", "high", 1)


def test_state_sequence_is_sqlite_bounded(repo):
    with pytest.raises(SessionError):
        repo.update_agent_state("s1", "running", "hook", "high", 1 << 63)


def test_structured_tool_sets_running_without_text_inference(bridge_factory):
    bridge, spool = bridge_factory(structured_stream=True)
    bridge.ingest_one({"kind": "tool_call", "payload": {"tool_name": "Read", "arguments": "src/app.py"}}, source="structured_stream")
    states = [e for e in spool.read_after(0, 100, 1 << 20) if e["kind"] == "agent_state_changed"]
    assert states[-1]["payload"]["state"] == "running"
    assert states[-1]["payload"]["source"] == "structured_stream"


def test_new_migration_failure_keeps_legacy_session_store(tmp_path, monkeypatch):
    repo = SessionRepository(tmp_path / "sessions.db")
    def fail(_):
        raise sqlite3.OperationalError("private migration error")
    monkeypatch.setattr(repo, "_migrate_v3", fail)
    repo.init()
    assert not repo.management_available
    row = repo.upsert_session({"session_id": "s1", "machine_id": "m1", "managed": False})
    assert row["current_state"] == "unknown"
    with pytest.raises(SessionError, match="feature_unavailable"):
        repo.update_agent_state("s1", "running", "hook", "high", 1)
