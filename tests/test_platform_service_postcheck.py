from pathlib import Path

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.platform_command import PlatformCommand
from tools.platform.node_client import NodeClient
from tools.platform.node_executor import NodeToolExecutor
from tools.platform.journal import NodeJournal
from tools.platform.services.actions import ServiceActionExecutor


OWNER = "owner@example.test"


def _app(tmp_path: Path):
    return create_app(FleetConfig.from_root(
        tmp_path, dev_operator=OWNER, platform_enabled=True,
        service_monitoring_enabled=True, service_actions_enabled=True,
    ))


class FlaskTransport:
    def __init__(self, client):
        self.client = client

    def post_json(self, url, body, headers):
        path = "/" + url.split("/", 3)[-1]
        response = self.client.post(path, json=body, headers=headers)
        return response.status_code, response.get_json()


def _unknown_inspect(app, *, command_id="svc-inspect"):
    repo = app.extensions["fleet"]["platform_repository"]
    commands = app.extensions["fleet"]["platform_commands"]
    repo.upsert_node(OWNER, {"node_id": "node-a", "label": "A"})
    app.extensions["fleet"]["services"]["service_health"].register(OWNER, {
        "service_id": "api", "node_id": "node-a", "adapter": "systemd",
        "target_alias": "api.service", "allowed_actions": ["inspect"],
    })
    service = app.extensions["fleet"]["services"]["service_health"].repository.get_service(OWNER, "api")
    command = PlatformCommand.create(
        command_id=command_id, target_node="node-a", owner_id=OWNER,
        action="service.inspect", resource_id="api",
        arguments={"adapter": service["adapter"], "target_alias": service["target_alias"],
                   "service_version": service["version"], "action": "inspect"},
        retry_class="read_only", expires_at=9_999_999_999,
    )
    commands.enqueue(command, idempotency_key=command_id)
    commands.mark_unknown(command_id, reason="receipt_timeout")
    return repo, commands, service


def test_service_inspect_postcheck_records_process_evidence_without_raw_output(tmp_path):
    app = _app(tmp_path)
    repo, commands, service = _unknown_inspect(app)
    repo.provision_node_credential(OWNER, "node-a", secret="a" * 40)
    client = app.test_client()
    requested = client.post("/api/platform/v1/commands/svc-inspect/postcheck")
    assert requested.status_code == 202
    payload = requested.get_json()["postcheck"]
    assert payload["kind"] == "service_inspect"
    assert payload["service_id"] == "api"
    assert "output" not in requested.get_data(as_text=True)
    check_id = payload["check_command_id"]
    check = commands.get(check_id)
    assert check["action"] == "reconcile.service.inspect"
    assert check["arguments"] == {
        "adapter": "systemd", "target_alias": "api.service",
        "service_version": service["version"], "action": "inspect",
    }

    def runner(argv, **kwargs):
        return type("Result", (), {
            "stdout": "active SECRET_SHOULD_NOT_LEAK", "stderr": "", "returncode": 0,
        })()

    journal = NodeJournal(tmp_path / "node.db")
    journal.init()
    node = NodeClient(
        journal,
        executor=NodeToolExecutor(
            None, service_executor=ServiceActionExecutor(runner=runner),
        ),
        node_id="node-a", credential="node-a:" + "a" * 40,
        hub_url="https://hub.invalid", transport=FlaskTransport(client),
        worker_id="node-worker",
    )
    assert node.poll_once() == {"ok": True, "commands": 1, "receipts": 1}
    assert commands.get(check_id)["status"] == "succeeded"
    checked = client.get("/api/platform/v1/commands/svc-inspect/postcheck")
    assert checked.status_code == 200
    postcheck = checked.get_json()["postcheck"]
    assert postcheck["state"] == "matched"
    assert postcheck["result"]["state"] == "healthy"
    assert "output" not in checked.get_data(as_text=True)
    assert commands.get("svc-inspect")["status"] == "unknown"
    evidence = app.extensions["fleet"]["services"]["service_health"].repository.list_evidence(OWNER, "api")
    assert any(item["source"] == "service_postcheck" and item["state"] == "healthy" for item in evidence)
    with commands._connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM platform_command_reconciliations WHERE command_id=?",
            ("svc-inspect",),
        ).fetchone()[0]
    client.get("/api/platform/v1/commands/svc-inspect/postcheck")
    with commands._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM platform_command_reconciliations WHERE command_id=?",
            ("svc-inspect",),
        ).fetchone()[0] == count == 1


def test_service_inspect_postcheck_fences_service_version_and_owner(tmp_path):
    app = _app(tmp_path)
    _repo, commands, _service = _unknown_inspect(app, command_id="svc-version")
    health = app.extensions["fleet"]["services"]["service_health"]
    health.register(OWNER, {
        "service_id": "api", "node_id": "node-a", "adapter": "systemd",
        "target_alias": "api.service", "allowed_actions": ["inspect"],
    })
    client = app.test_client()
    response = client.post("/api/platform/v1/commands/svc-version/postcheck")
    assert response.status_code == 409
    assert response.get_json()["error"] == "service_version_conflict"
    other = client.post(
        "/api/platform/v1/commands/svc-version/postcheck",
        headers={"Cf-Access-Authenticated-User-Email": "other@example.test"},
    )
    assert other.status_code == 404


def test_service_inspect_postcheck_keeps_unknown_when_executor_is_interrupted(tmp_path):
    app = _app(tmp_path)
    _repo, commands, _service = _unknown_inspect(app, command_id="svc-unknown")
    repo = app.extensions["fleet"]["platform_repository"]
    repo.provision_node_credential(OWNER, "node-a", secret="a" * 40)
    client = app.test_client()
    requested = client.post("/api/platform/v1/commands/svc-unknown/postcheck")
    assert requested.status_code == 202

    def interrupted(*_args, **_kwargs):
        raise RuntimeError("collector interrupted")

    journal = NodeJournal(tmp_path / "node-unknown.db")
    journal.init()
    node = NodeClient(
        journal,
        executor=NodeToolExecutor(
            None, service_executor=ServiceActionExecutor(runner=interrupted),
        ),
        node_id="node-a", credential="node-a:" + "a" * 40,
        hub_url="https://hub.invalid", transport=FlaskTransport(client),
        worker_id="node-worker",
    )
    assert node.poll_once()["receipts"] == 1
    checked = client.get("/api/platform/v1/commands/svc-unknown/postcheck")
    assert checked.status_code == 200
    assert checked.get_json()["postcheck"]["state"] == "remains_unknown"
    assert commands.get("svc-unknown")["status"] == "unknown"


def test_service_inspect_postcheck_rechecks_version_before_health_evidence(tmp_path):
    app = _app(tmp_path)
    _repo, commands, _service = _unknown_inspect(app, command_id="svc-race")
    repo = app.extensions["fleet"]["platform_repository"]
    repo.provision_node_credential(OWNER, "node-a", secret="a" * 40)
    client = app.test_client()
    requested = client.post("/api/platform/v1/commands/svc-race/postcheck")
    assert requested.status_code == 202
    health = app.extensions["fleet"]["services"]["service_health"]
    health.register(OWNER, {
        "service_id": "api", "node_id": "node-a", "adapter": "systemd",
        "target_alias": "api.service", "allowed_actions": ["inspect"],
    })

    def runner(argv, **kwargs):
        return type("Result", (), {"stdout": "active", "stderr": "", "returncode": 0})()

    journal = NodeJournal(tmp_path / "node-race.db")
    journal.init()
    node = NodeClient(
        journal,
        executor=NodeToolExecutor(None, service_executor=ServiceActionExecutor(runner=runner)),
        node_id="node-a", credential="node-a:" + "a" * 40,
        hub_url="https://hub.invalid", transport=FlaskTransport(client),
        worker_id="node-worker",
    )
    assert node.poll_once()["receipts"] == 1
    checked = client.get("/api/platform/v1/commands/svc-race/postcheck")
    assert checked.status_code == 200
    postcheck = checked.get_json()["postcheck"]
    assert postcheck["state"] == "remains_unknown"
    assert postcheck["result"]["error_code"] == "service_version_conflict"
    evidence = health.repository.list_evidence(OWNER, "api")
    assert not any(item["source"] == "service_postcheck" for item in evidence)
    assert commands.get("svc-race")["status"] == "unknown"
