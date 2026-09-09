"""Task 3 tests: hub 保存/返回 sanitized ``instances``（只读，不执行 adopt）。

The probe (Task 2) publishes discovered agent rows under an additive
``instances`` key.  The hub must:

- persist exactly the allowlisted metadata rows (never raw ``conversation``
  content, process seeds, or unknown keys);
- keep the old agents/system snapshot fields intact;
- expose the sanitized list through the public machine-detail read model
  (covered in ``test_machine_domain``) without any side effect
  (no session, transcript, control command, or signal is created).

These tests drive the real HTTP ingest endpoint through a sandboxed state dir
and assert on the persisted repository row, matching the Task 3 brief.
"""

import tempfile
from pathlib import Path

import pytest

from hub import events
from hub import state as store
from hub import task_store


@pytest.fixture()
def client():
    temp_dir = Path(tempfile.mkdtemp())
    old_state = store.STATE_DIR
    old_events = events.EVENT_LOG
    old_db = task_store.DB_PATH
    store.STATE_DIR = temp_dir
    events.EVENT_LOG = temp_dir / "events.jsonl"
    task_store.DB_PATH = temp_dir / "fleet.db"
    task_store.init_db()
    store.save_snapshot("m1", {
        "machine": "m1", "source": "ingest", "reachable": True,
        "agents": {"codex": {"installed": True}}, "system": {},
    })
    from hub import web
    app = web.make_app(
        ingest_token="test-only",
        dev_operator="op@example.com",
        runner_credentials={"m1": "test-only"},
        project_whitelist={"m1": ["agent-fleet"]},
    )
    test_client = app.test_client()
    yield test_client
    store.STATE_DIR = old_state
    events.EVENT_LOG = old_events
    task_store.DB_PATH = old_db


def ingest_header():
    return {"X-Agent-Fleet-Token": "test-only"}


def valid_payload(**extra):
    payload = {
        "machine": "m1",
        "agent": {"codex": {"installed": True}},
        "system": {"platform": "darwin"},
        "instances": [],
    }
    payload.update(extra)
    return payload


def read_current(machine="m1"):
    return store.read_current(machine)


INSTANCE_KEYS = {
    "pid", "pgid", "exe_path", "cmdline", "agent_family",
    "native_file_path", "started_at", "attachable",
}


def test_ingest_keeps_only_sanitized_instance_fields(client):
    response = client.post("/api/ingest", json=valid_payload(instances=[{
        "pid": 7, "pgid": 6, "exe_path": "/bin/codex", "cmdline": "codex",
        "agent_family": "codex", "native_file_path": None,
        "started_at": "2026-08-30T00:00:00Z", "attachable": True,
        "conversation": "drop",
    }]), headers=ingest_header())
    assert response.status_code == 200
    row = read_current("m1")["instances"][0]
    assert row["pid"] == 7
    assert row["pgid"] == 6
    assert "conversation" not in row


def test_ingest_persists_exact_instance_allowlist(client):
    response = client.post("/api/ingest", json=valid_payload(instances=[{
        "pid": "11", "pgid": "10", "exe_path": "/bin/codex", "cmdline": "codex",
        "agent_family": "claude_code", "native_file_path": "/tmp/native.jsonl",
        "started_at": "2026-08-30T00:00:00Z", "attachable": 0,
        "process_token": 99, "secret_key": True,
    }]), headers=ingest_header())
    assert response.status_code == 200
    rows = read_current("m1")["instances"]
    assert len(rows) == 1
    assert set(rows[0]) == INSTANCE_KEYS
    assert rows[0]["pid"] == 11
    assert rows[0]["attachable"] is False
    assert "process_token" not in rows[0]


def test_ingest_defaults_missing_instances_to_empty_list(client):
    payload = valid_payload()
    payload.pop("instances", None)
    response = client.post("/api/ingest", json=payload, headers=ingest_header())
    assert response.status_code == 200
    assert read_current("m1")["instances"] == []


def test_ingest_drops_malformed_instance_rows_individually(client):
    response = client.post("/api/ingest", json=valid_payload(instances=[
        {
            "pid": 1, "pgid": 1, "exe_path": "/bin/ok", "cmdline": "",
            "agent_family": "hermes", "native_file_path": None,
            "started_at": "", "attachable": True,
        },
        {
            "pgid": 2, "exe_path": "/bin/bad", "cmdline": "",
            "agent_family": "codex",
        },  # missing pid -> dropped
        {
            "pid": "x", "pgid": 3, "exe_path": "/bin/bad2", "cmdline": "",
            "agent_family": "codex",
        },  # non-numeric pid -> dropped
        {
            "pid": 4, "pgid": 4, "exe_path": "", "cmdline": "",
            "agent_family": "codex",
        },  # empty exe -> dropped
        {
            "pid": -1, "pgid": 1, "exe_path": "/bin/bad3", "cmdline": "",
            "agent_family": "codex",
        },  # negative pid -> dropped (frontend aborts the whole DTO)
        {
            "pid": "9", "pgid": -9, "exe_path": "/bin/bad4", "cmdline": "",
            "agent_family": "codex",
        },  # negative pgid (string) -> dropped
    ]), headers=ingest_header())
    assert response.status_code == 200
    rows = read_current("m1")["instances"]
    assert len(rows) == 1
    assert rows[0]["pid"] == 1
    assert rows[0]["agent_family"] == "hermes"