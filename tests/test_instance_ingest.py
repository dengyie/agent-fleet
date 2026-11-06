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

import pytest


@pytest.fixture()
def store(hub_stores):
    hub_stores.store.save_snapshot("m1", {
        "machine": "m1", "source": "ingest", "reachable": True,
        "agents": {"codex": {"installed": True}}, "system": {},
    })
    return hub_stores.store


@pytest.fixture()
def client(hub_stores, store):
    app = hub_stores.make_app(
        ingest_token="test-only",
        dev_operator="op@example.com",
        runner_credentials={"m1": "test-only"},
        project_whitelist={"m1": ["agent-fleet"]},
    )
    return app.test_client()


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


INSTANCE_KEYS = {
    "pid", "pgid", "exe_path", "cmdline", "agent_family",
    "native_file_path", "started_at", "attachable",
}


def test_ingest_keeps_only_sanitized_instance_fields(client, store):
    response = client.post("/api/ingest", json=valid_payload(instances=[{
        "pid": 7, "pgid": 6, "exe_path": "/bin/codex", "cmdline": "codex",
        "agent_family": "codex", "native_file_path": None,
        "started_at": "2026-08-30T00:00:00Z", "attachable": True,
        "conversation": "drop",
    }]), headers=ingest_header())
    assert response.status_code == 200
    row = store.read_current("m1")["instances"][0]
    assert row["pid"] == 7
    assert row["pgid"] == 6
    assert "conversation" not in row


def test_ingest_persists_exact_instance_allowlist(client, store):
    response = client.post("/api/ingest", json=valid_payload(instances=[{
        "pid": "11", "pgid": "10", "exe_path": "/bin/codex", "cmdline": "codex",
        "agent_family": "claude_code", "native_file_path": "/tmp/native.jsonl",
        "started_at": "2026-08-30T00:00:00Z", "attachable": 0,
        "process_token": 99, "secret_key": True,
    }]), headers=ingest_header())
    assert response.status_code == 200
    rows = store.read_current("m1")["instances"]
    assert len(rows) == 1
    assert set(rows[0]) == INSTANCE_KEYS
    assert rows[0]["pid"] == 11
    assert rows[0]["attachable"] is False
    assert "process_token" not in rows[0]


def test_ingest_defaults_missing_instances_to_empty_list(client, store):
    payload = valid_payload()
    payload.pop("instances", None)
    response = client.post("/api/ingest", json=payload, headers=ingest_header())
    assert response.status_code == 200
    assert store.read_current("m1")["instances"] == []


def test_replayed_report_id_does_not_append_history_or_publish_duplicate(client, store):
    payload = valid_payload(report_id="replayed-report-1", agent={"codex": {"installed": False}})
    publisher = client.application.extensions["fleet"]["publisher"]

    first = client.post("/api/ingest", json=payload, headers=ingest_header())
    after_first = store.read_history("m1", 20)
    events_after_first = publisher.read_recent(20)
    second = client.post("/api/ingest", json=payload, headers=ingest_header())
    after_second = store.read_history("m1", 20)

    assert first.status_code == second.status_code == 200
    assert len(after_second) == len(after_first)
    assert second.get_json()["duplicate"] is True
    assert "report_id" not in store.read_current("m1")
    assert "_ingest_report_id" not in store.read_current("m1")
    assert first.get_json()["changes"] == ["agents"]
    assert len(events_after_first) == 1
    assert publisher.read_recent(20) == events_after_first


def test_missing_report_id_keeps_legacy_ingest_behavior(client):
    response = client.post("/api/ingest", json=valid_payload(), headers=ingest_header())

    assert response.status_code == 200
    assert "duplicate" not in response.get_json()


def test_invalid_report_id_is_rejected_without_writing_history(client, store):
    before = len(store.read_history("m1", 20))
    response = client.post(
        "/api/ingest", json=valid_payload(report_id=None), headers=ingest_header())

    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_report_id"
    assert len(store.read_history("m1", 20)) == before


def test_ingest_drops_malformed_instance_rows_individually(client, store):
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
    rows = store.read_current("m1")["instances"]
    assert len(rows) == 1
    assert rows[0]["pid"] == 1
    assert rows[0]["agent_family"] == "hermes"
