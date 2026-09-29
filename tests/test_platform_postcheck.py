from pathlib import Path

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.platform_command import PlatformCommand
from tools.platform.backends.directory import DirectoryBackend
from tools.platform.node_client import NodeClient
from tools.platform.node_executor import NodeToolExecutor
from tools.platform.journal import NodeJournal
from tools.platform.backends.directory import MAX_DIGEST_BYTES


OWNER = "owner@example.test"


def _app(tmp_path: Path):
    return create_app(FleetConfig.from_root(
        tmp_path, ingest_token="ingest", dev_operator=OWNER,
        platform_enabled=True,
    ))


def _unknown_write(app, *, command_id="cmd-write", path="report.md", content="hello"):
    repo = app.extensions["fleet"]["platform_repository"]
    commands = app.extensions["fleet"]["platform_commands"]
    repo.upsert_node(OWNER, {"node_id": "node-a", "label": "A"})
    command = PlatformCommand.create(
        command_id=command_id, target_node="node-a", owner_id=OWNER,
        action="tool.workspace.write", resource_id="workspace-a",
        arguments={"path": path, "content": content},
        retry_class="reconcile_before_retry", expires_at=9_999_999_999,
        run_id="run-a",
    )
    commands.enqueue(command, idempotency_key=command_id)
    commands.mark_unknown(command_id, reason="receipt_timeout")
    return repo, commands


class FlaskTransport:
    def __init__(self, client):
        self.client = client
        self.requests = []

    def post_json(self, url, body, headers):
        path = "/" + url.split("/", 3)[-1]
        self.requests.append((path, dict(body)))
        response = self.client.post(path, json=body, headers=headers)
        return response.status_code, response.get_json()


def test_postcheck_is_derived_from_private_command_and_is_idempotent(tmp_path):
    app = _app(tmp_path)
    repo, commands = _unknown_write(app)
    client = app.test_client()
    first = client.post("/api/platform/v1/commands/cmd-write/postcheck", json={"path": "attacker.txt"})
    second = client.post("/api/platform/v1/commands/cmd-write/postcheck")
    assert first.status_code == second.status_code == 202
    one = first.get_json()["postcheck"]
    two = second.get_json()["postcheck"]
    assert one["check_command_id"] == two["check_command_id"]
    assert one["path"] == "report.md"
    assert "expected_sha256" not in first.get_data(as_text=True)
    assert "content" not in first.get_data(as_text=True)
    queued = commands.get(one["check_command_id"])
    assert queued["action"] == "reconcile.workspace.digest"
    assert queued["arguments"] == {"path": "report.md"}
    assert queued["target_node"] == "node-a"
    assert repo.get_run(OWNER, "missing") is None


def test_postcheck_http_node_digest_records_match_without_changing_unknown(tmp_path):
    app = _app(tmp_path)
    repo, commands = _unknown_write(app, path="report.md", content="hello")
    repo.provision_node_credential(OWNER, "node-a", secret="a" * 40)
    workspace = tmp_path / "node-workspace"
    workspace.mkdir()
    (workspace / "report.md").write_text("hello", encoding="utf-8")
    client = app.test_client()
    requested = client.post("/api/platform/v1/commands/cmd-write/postcheck")
    check_id = requested.get_json()["postcheck"]["check_command_id"]

    journal = NodeJournal(tmp_path / "node.db")
    journal.init()
    transport = FlaskTransport(client)
    node = NodeClient(
        journal,
        executor=NodeToolExecutor(DirectoryBackend(workspace)),
        node_id="node-a", credential="node-a:" + "a" * 40,
        hub_url="https://hub.invalid", transport=transport,
        worker_id="node-worker",
    )
    result = node.poll_once()
    assert result == {"ok": True, "commands": 1, "receipts": 1}
    assert commands.get(check_id)["status"] == "succeeded"
    checked = client.get("/api/platform/v1/commands/cmd-write/postcheck")
    assert checked.status_code == 200
    payload = checked.get_json()["postcheck"]
    assert payload["state"] == "matched"
    assert payload["result"]["path"] == "report.md"
    assert payload["result"]["size"] == 5
    assert len(payload["result"]["sha256"]) == 64
    assert commands.get("cmd-write")["status"] == "unknown"
    with commands._connect() as conn:
        audit = conn.execute(
            "SELECT outcome,evidence_source FROM platform_command_reconciliations "
            "WHERE command_id=? ORDER BY created_at DESC LIMIT 1", ("cmd-write",)
        ).fetchone()
    assert tuple(audit) == ("confirmed_succeeded", "workspace_check")
    assert "hello" not in checked.get_data(as_text=True)
    # Refreshing an already terminal check is read-idempotent and does not
    # append another reconciliation audit row.
    client.get("/api/platform/v1/commands/cmd-write/postcheck")
    with commands._connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM platform_command_reconciliations WHERE command_id=?",
            ("cmd-write",),
        ).fetchone()[0]
    assert count == 1


def test_postcheck_mismatch_remains_unknown_and_is_owner_scoped(tmp_path):
    app = _app(tmp_path)
    repo, commands = _unknown_write(app, path="report.md", content="expected")
    repo.provision_node_credential(OWNER, "node-a", secret="a" * 40)
    workspace = tmp_path / "node-workspace"
    workspace.mkdir()
    (workspace / "report.md").write_text("different", encoding="utf-8")
    client = app.test_client()
    client.post("/api/platform/v1/commands/cmd-write/postcheck")
    node = NodeClient(
        NodeJournal(tmp_path / "node.db"),
        executor=NodeToolExecutor(DirectoryBackend(workspace)),
        node_id="node-a", credential="node-a:" + "a" * 40,
        hub_url="https://hub.invalid", transport=FlaskTransport(client),
        worker_id="node-worker",
    )
    node.journal.init()
    assert node.poll_once()["receipts"] == 1
    payload = client.get("/api/platform/v1/commands/cmd-write/postcheck").get_json()["postcheck"]
    assert payload["state"] == "mismatch"
    assert commands.get("cmd-write")["status"] == "unknown"

    other = client.post(
        "/api/platform/v1/commands/cmd-write/postcheck",
        headers={"Cf-Access-Authenticated-User-Email": "other@example.test"},
    )
    assert other.status_code == 404


def test_postcheck_rejects_non_write_and_missing_descriptor(tmp_path):
    app = _app(tmp_path)
    repo = app.extensions["fleet"]["platform_repository"]
    commands = app.extensions["fleet"]["platform_commands"]
    repo.upsert_node(OWNER, {"node_id": "node-a"})
    read = PlatformCommand.create(
        command_id="cmd-read", target_node="node-a", owner_id=OWNER,
        action="tool.workspace.read", resource_id="workspace-a",
        arguments={"path": "a"}, retry_class="read_only", expires_at=9_999_999_999,
    )
    commands.enqueue(read, idempotency_key="cmd-read")
    commands.mark_unknown("cmd-read", reason="timeout")
    response = app.test_client().post("/api/platform/v1/commands/cmd-read/postcheck")
    assert response.status_code == 409
    assert response.get_json()["error"] == "postcheck_not_supported"


def test_digest_is_bounded_and_large_current_file_remains_unknown(tmp_path):
    app = _app(tmp_path)
    repo, commands = _unknown_write(app, path="report.md", content="small")
    repo.provision_node_credential(OWNER, "node-a", secret="a" * 40)
    workspace = tmp_path / "node-workspace"
    workspace.mkdir()
    with (workspace / "report.md").open("wb") as handle:
        handle.write(b"x" * (MAX_DIGEST_BYTES + 1))
    client = app.test_client()
    client.post("/api/platform/v1/commands/cmd-write/postcheck")
    journal = NodeJournal(tmp_path / "node.db")
    journal.init()
    node = NodeClient(
        journal, executor=NodeToolExecutor(DirectoryBackend(workspace)),
        node_id="node-a", credential="node-a:" + "a" * 40,
        hub_url="https://hub.invalid", transport=FlaskTransport(client),
        worker_id="node-worker",
    )
    assert node.poll_once()["receipts"] == 1
    payload = client.get("/api/platform/v1/commands/cmd-write/postcheck").get_json()["postcheck"]
    assert payload["state"] == "remains_unknown"
    assert payload["result"]["error_code"] == "digest_too_large"
