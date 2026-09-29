from pathlib import Path
import sqlite3

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.platform_command import PlatformCommand
from tools.platform.journal import NodeJournal
from tools.platform.node_client import NodeClient
from tools.platform.node_executor import NodeToolExecutor


OWNER = "owner@example.test"


def _app(tmp_path: Path):
    return create_app(FleetConfig.from_root(
        tmp_path, dev_operator=OWNER, platform_enabled=True,
        service_monitoring_enabled=True, service_actions_enabled=True,
    ))


class _Transport:
    def __init__(self, client):
        self.client = client

    def post_json(self, url, body, headers):
        path = "/" + url.split("/", 3)[-1]
        response = self.client.post(path, json=body, headers=headers)
        return response.status_code, response.get_json()


def _unknown_logs(app, *, command_id="logs-1"):
    repo = app.extensions["fleet"]["platform_repository"]
    commands = app.extensions["fleet"]["platform_commands"]
    repo.upsert_node(OWNER, {"node_id": "node-a", "label": "A"})
    health = app.extensions["fleet"]["services"]["service_health"]
    health.register(OWNER, {
        "service_id": "api", "node_id": "node-a",
        "adapter": "systemd", "target_alias": "api.service",
        "allowed_actions": ["inspect"],
    })
    service = health.repository.get_service(OWNER, "api")
    command = PlatformCommand.create(
        command_id=command_id, target_node="node-a", owner_id=OWNER,
        action="tool.service.read_logs", resource_id="workspace",
        arguments={
            "service_id": "api", "window_s": 60, "max_bytes": 1024,
            "adapter": service["adapter"], "target_alias": service["target_alias"],
            "service_version": service["version"],
        },
        retry_class="read_only", expires_at=9_999_999_999,
    )
    commands.enqueue(command, idempotency_key=command_id)
    commands.mark_unknown(command_id, reason="receipt_timeout")
    return commands, service


def _node(app, tmp_path, reader):
    repo = app.extensions["fleet"]["platform_repository"]
    repo.provision_node_credential(OWNER, "node-a", secret="a" * 40)
    journal = NodeJournal(tmp_path / "node.db")
    journal.init()
    return NodeClient(
        journal, executor=NodeToolExecutor(None, log_reader=reader),
        node_id="node-a", credential="node-a:" + "a" * 40,
        hub_url="https://hub.invalid", transport=_Transport(app.test_client()),
        worker_id="node-worker",
    )


def _mutate_service_identity_preserving_version(app, **values):
    repository = app.extensions["fleet"]["services"]["service_health"].repository
    assignments = ", ".join(f"{key} = ?" for key in values)
    params = list(values.values()) + [OWNER, "api"]
    with sqlite3.connect(repository.db_path) as connection:
        connection.execute(
            f"UPDATE service_definitions SET {assignments} WHERE owner_id = ? AND service_id = ?",
            params,
        )


def test_service_logs_postcheck_redacts_and_bounds_current_evidence(tmp_path):
    app = _app(tmp_path)
    commands, _service = _unknown_logs(app)
    requested = app.test_client().post("/api/platform/v1/commands/logs-1/postcheck")
    assert requested.status_code == 202
    payload = requested.get_json()["postcheck"]
    assert payload["kind"] == "service_logs"
    check = commands.get(payload["check_command_id"])
    assert check["action"] == "reconcile.service.logs"
    assert check["arguments"] == {
        "adapter": "systemd", "target_alias": "api.service",
        "service_version": 1, "action": "logs",
        "window_s": 60.0, "max_bytes": 1024,
    }

    node = _node(app, tmp_path, lambda command: {
        "text": "token=secret-value password=correct horse /Users/mango/private.txt",
        "truncated": False, "observed_at": 10,
    })
    assert node.poll_once() == {"ok": True, "commands": 1, "receipts": 1}
    result = app.test_client().get("/api/platform/v1/commands/logs-1/postcheck")
    assert result.status_code == 200
    postcheck = result.get_json()["postcheck"]
    assert postcheck["state"] == "matched"
    assert "secret-value" not in postcheck["result"]["text"]
    assert "correct horse" not in postcheck["result"]["text"]
    assert "/Users/mango/private.txt" not in postcheck["result"]["text"]
    assert len(postcheck["result"]["text"].encode()) <= 1024
    assert "secret-value" not in result.get_data(as_text=True)
    assert commands.get("logs-1")["status"] == "unknown"


def test_service_logs_postcheck_truncates_multibyte_payload_to_byte_limit(tmp_path):
    app = _app(tmp_path)
    commands, _service = _unknown_logs(app, command_id="logs-truncate")
    client = app.test_client()
    assert client.post("/api/platform/v1/commands/logs-truncate/postcheck").status_code == 202
    node = _node(app, tmp_path, lambda command: {
        "text": "日志行" * 1000,
        "truncated": False,
        "observed_at": 10,
    })
    assert node.poll_once()["receipts"] == 1
    result = client.get("/api/platform/v1/commands/logs-truncate/postcheck")
    assert result.status_code == 200
    payload = result.get_json()["postcheck"]["result"]
    assert payload["truncated"] is True
    assert len(payload["text"].encode("utf-8")) <= 1024


def test_service_logs_postcheck_rejects_forged_envelope_and_keeps_unknown(tmp_path):
    app = _app(tmp_path)
    commands, _service = _unknown_logs(app, command_id="logs-forge")
    client = app.test_client()
    requested = client.post("/api/platform/v1/commands/logs-forge/postcheck")
    assert requested.status_code == 202
    check_id = requested.get_json()["postcheck"]["check_command_id"]

    from tools.platform.node_executor import SERVICE_LOGS_POSTCHECK_ACTION
    executor = NodeToolExecutor(None, log_reader=lambda command: {"text": "safe"})
    forged = executor({
        "command_id": check_id, "action": SERVICE_LOGS_POSTCHECK_ACTION,
        "resource_id": "workspace",
        "arguments": {
            "adapter": "systemd", "target_alias": "api.service",
            "service_version": 1, "action": "logs",
            "window_s": 60, "max_bytes": 1024, "argv": ["cat"],
        },
    })
    assert forged["error_code"] == "invalid_arguments"
    assert commands.get("logs-forge")["status"] == "unknown"


def test_service_logs_postcheck_version_fence_owner_and_duplicate_reads(tmp_path):
    app = _app(tmp_path)
    commands, _service = _unknown_logs(app, command_id="logs-version")
    client = app.test_client()
    assert client.post("/api/platform/v1/commands/logs-version/postcheck").status_code == 202
    health = app.extensions["fleet"]["services"]["service_health"]
    health.register(OWNER, {
        "service_id": "api", "node_id": "node-a",
        "adapter": "systemd", "target_alias": "api.service",
        "allowed_actions": ["inspect"],
    })
    node = _node(app, tmp_path, lambda command: {"text": "safe", "observed_at": 10})
    assert node.poll_once()["receipts"] == 1
    fenced = client.get("/api/platform/v1/commands/logs-version/postcheck")
    assert fenced.status_code == 200
    assert fenced.get_json()["postcheck"]["state"] == "remains_unknown"
    assert fenced.get_json()["postcheck"]["result"]["error_code"] == "service_version_conflict"
    other = client.get("/api/platform/v1/commands/logs-version/postcheck", headers={"Cf-Access-Authenticated-User-Email": "other@example.test"})
    assert other.status_code == 404
    assert client.get("/api/platform/v1/commands/logs-version/postcheck").get_json()["postcheck"]["state"] == "remains_unknown"
    assert commands.get("logs-version")["status"] == "unknown"


def test_service_logs_postcheck_rejects_adapter_or_alias_change_before_enqueue(tmp_path):
    for field, value in (("adapter", "supervisor"), ("target_alias", "api-v2.service")):
        app = _app(tmp_path / field)
        _unknown_logs(app, command_id="logs-identity-" + field)
        _mutate_service_identity_preserving_version(app, **{field: value})
        response = app.test_client().post(
            "/api/platform/v1/commands/logs-identity-" + field + "/postcheck"
        )
        assert response.status_code == 409
        assert response.get_json()["error"] == "service_version_conflict"


def test_service_logs_postcheck_rejects_adapter_or_alias_change_before_evidence(tmp_path):
    for field, value in (("adapter", "supervisor"), ("target_alias", "api-v2.service")):
        app = _app(tmp_path / ("refresh-" + field))
        command_id = "logs-refresh-" + field
        commands, _service = _unknown_logs(app, command_id=command_id)
        client = app.test_client()
        assert client.post(f"/api/platform/v1/commands/{command_id}/postcheck").status_code == 202
        _mutate_service_identity_preserving_version(app, **{field: value})
        node = _node(app, tmp_path / ("node-" + field), lambda command: {"text": "safe"})
        assert node.poll_once()["receipts"] == 1
        payload = client.get(f"/api/platform/v1/commands/{command_id}/postcheck").get_json()["postcheck"]
        assert payload["state"] == "remains_unknown"
        assert payload["result"]["error_code"] == "service_version_conflict"
        assert commands.get(command_id)["status"] == "unknown"


def test_service_logs_postcheck_interrupted_reader_remains_unknown(tmp_path):
    app = _app(tmp_path)
    commands, _service = _unknown_logs(app, command_id="logs-unknown")
    client = app.test_client()
    assert client.post("/api/platform/v1/commands/logs-unknown/postcheck").status_code == 202

    def interrupted(_command):
        raise RuntimeError("reader interrupted")

    node = _node(app, tmp_path, interrupted)
    assert node.poll_once()["receipts"] == 1
    checked = client.get("/api/platform/v1/commands/logs-unknown/postcheck")
    assert checked.get_json()["postcheck"]["state"] == "remains_unknown"
    assert commands.get("logs-unknown")["status"] == "unknown"
