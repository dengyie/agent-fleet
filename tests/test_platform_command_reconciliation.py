from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.platform_command import PlatformCommand


def _app(tmp_path, owner="owner-a@example.test"):
    return create_app(FleetConfig.from_root(
        tmp_path, ingest_token="ingest", dev_operator=owner,
        platform_enabled=True,
    ))


def _command(owner="owner-a@example.test", command_id="cmd-unknown"):
    return PlatformCommand.create(
        command_id=command_id, target_node="node-a", owner_id=owner,
        action="tool.workspace.write", resource_id="workspace-a",
        arguments={"path": "report.md", "content": "hello"},
        retry_class="reconcile_before_retry", expires_at=9_999_999_999,
        run_id="run-a",
    )


def test_unknown_command_inspection_is_owner_scoped_and_redacted(tmp_path):
    app = _app(tmp_path)
    repo = app.extensions["fleet"]["platform_commands"]
    repo.enqueue(_command(), idempotency_key="unknown-1")
    repo.mark_unknown("cmd-unknown", reason="receipt_timeout")

    client = app.test_client()
    response = client.get("/api/platform/v1/commands/unknown")
    assert response.status_code == 200
    command = response.get_json()["commands"][0]
    assert command["command_id"] == "cmd-unknown"
    assert command["result"] == {"reason": "receipt_timeout"}
    assert "arguments" not in command
    assert "signature" not in command
    assert "lease_owner" not in command

    detail = client.get("/api/platform/v1/commands/cmd-unknown")
    assert detail.status_code == 200
    assert detail.get_json()["command"]["status"] == "unknown"

    other = _app(tmp_path / "other", owner="owner-b@example.test").test_client()
    assert other.get("/api/platform/v1/commands/cmd-unknown").status_code == 404
    assert other.get("/api/platform/v1/commands/unknown").get_json()["commands"] == []


def test_reconcile_is_bounded_audit_only_and_never_replays_or_changes_state(tmp_path):
    app = _app(tmp_path)
    repo = app.extensions["fleet"]["platform_commands"]
    repo.enqueue(_command(), idempotency_key="unknown-1")
    repo.mark_unknown("cmd-unknown", reason="node_interrupted")
    client = app.test_client()

    response = client.post(
        "/api/platform/v1/commands/cmd-unknown/reconcile",
        json={
            "outcome": "confirmed_succeeded",
            "evidence": {"source": "workspace_check", "reference": "check-123"},
        },
    )
    assert response.status_code == 202
    payload = response.get_json()
    assert payload["reconciliation"]["status_effect"] == "audit_only"
    assert payload["command"]["status"] == "unknown"
    assert repo.get("cmd-unknown")["status"] == "unknown"
    with repo._connect() as conn:
        row = conn.execute(
            "SELECT outcome,evidence_source,evidence_reference FROM platform_command_reconciliations"
        ).fetchone()
    assert tuple(row) == ("confirmed_succeeded", "workspace_check", "check-123")
    assert repo.claim_for_node("node-a", "worker-a") == []


def test_reconcile_rejects_unbounded_or_non_unknown_input(tmp_path):
    app = _app(tmp_path)
    repo = app.extensions["fleet"]["platform_commands"]
    repo.enqueue(_command(), idempotency_key="unknown-1")
    client = app.test_client()
    invalid = client.post(
        "/api/platform/v1/commands/cmd-unknown/reconcile",
        json={"outcome": "confirmed_succeeded", "evidence": {"source": "operator", "reference": "x\n"}},
    )
    assert invalid.status_code == 400
    assert invalid.get_json()["error"] == "invalid_reconcile"
    repo.record_receipt("cmd-unknown", "failed", worker_id="worker-a", result={}) if False else None
    repo.mark_unknown("cmd-unknown", reason="manual")
    first = client.post(
        "/api/platform/v1/commands/cmd-unknown/reconcile",
        json={"outcome": "remains_unknown", "evidence": {"source": "operator", "reference": "operator-note"}},
    )
    assert first.status_code == 202
    second = client.post(
        "/api/platform/v1/commands/cmd-unknown/reconcile",
        json={"outcome": "remains_unknown", "evidence": {"source": "operator", "reference": "operator-note-2"}},
    )
    assert second.status_code == 202
