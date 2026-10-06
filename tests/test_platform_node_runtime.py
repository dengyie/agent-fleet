from pathlib import Path
import os

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.platform_command import PlatformCommand
from tools.platform.node_executor import (
    BROWSER_TOOLS,
    SERVICE_LOGS_POSTCHECK_ACTION,
    SERVICE_POSTCHECK_ACTION,
    NodeToolExecutor,
)
from tools.platform.node_runtime import (
    NODE_RUNTIME_CAPABILITIES,
    NodeRuntime,
    NodeRuntimeConfig,
)


from hub.domain.control import generate_ed25519_keypair

PRIVATE_KEY, PUBLIC_KEY = generate_ed25519_keypair()
OWNER = "owner@example.test"


def _log_command(*, adapter="systemd", alias="api.service", command_id="logs-runtime"):
    return {
        "command_id": command_id,
        "action": SERVICE_LOGS_POSTCHECK_ACTION,
        "resource_id": "workspace",
        "arguments": {
            "adapter": adapter,
            "target_alias": alias,
            "service_version": 1,
            "action": "logs",
            "window_s": 60,
            "max_bytes": 1024,
        },
    }


def _inspect_command(*, adapter="systemd", alias="api.service", command_id="inspect-runtime"):
    return {
        "command_id": command_id,
        "action": SERVICE_POSTCHECK_ACTION,
        "resource_id": "api",
        "arguments": {
            "adapter": adapter, "target_alias": alias,
            "service_version": 1, "action": "inspect",
        },
    }


def test_node_executor_capability_gate_runs_before_log_reader():
    calls = []
    executor = NodeToolExecutor(
        None,
        log_reader=lambda command: calls.append(command) or {"text": "should-not-run"},
        allowed_postcheck_actions=set(),
    )

    result = executor(_log_command())

    assert result["state"] == "failed"
    assert result["error_code"] == "capability_unavailable"
    assert calls == []


def test_node_executor_explicit_log_capability_accepts_exact_envelope():
    executor = NodeToolExecutor(
        None,
        log_reader=lambda command: {"text": "safe", "observed_at": 1},
        allowed_postcheck_actions={SERVICE_LOGS_POSTCHECK_ACTION},
    )

    result = executor(_log_command(command_id="logs-capability"))

    assert result["state"] == "succeeded"
    assert result["result"]["text"] == "safe"


def test_runtime_config_rejects_unknown_capabilities_and_redacts_manifest(tmp_path):
    with pytest.raises(ValueError, match="unknown capability"):
        NodeRuntimeConfig(
            node_id="node-a", credential="node-a:secret",
            workspace_root=tmp_path / "workspace",
            journal_path=tmp_path / "journal.db",
            hub_url="https://hub.invalid", public_key=PUBLIC_KEY,
            capabilities=frozenset({"host.root"}),
        )

    config = NodeRuntimeConfig(
        node_id="node-a", credential="node-a:secret",
        workspace_root=tmp_path / "workspace",
        journal_path=tmp_path / "journal.db",
        hub_url="https://hub.invalid", public_key=PUBLIC_KEY,
        capabilities=frozenset({SERVICE_LOGS_POSTCHECK_ACTION}),
    )
    runtime = NodeRuntime(config, transport=object())
    manifest = runtime.manifest()

    assert manifest == {
        "node_id": "node-a",
        "worker_id": "node-a",
        "capabilities": {SERVICE_LOGS_POSTCHECK_ACTION: True},
    }
    assert "secret" not in str(manifest)
    assert str(tmp_path) not in str(manifest)


def test_node_runtime_does_not_advertise_browser_without_driver(tmp_path):
    config = NodeRuntimeConfig(
        node_id="node-a", credential="node-a:secret",
        workspace_root=tmp_path / "workspace",
        journal_path=tmp_path / "journal.db",
        hub_url="https://hub.invalid", public_key=PUBLIC_KEY,
        capabilities=frozenset({"browser.session"}), browser_enabled=True,
    )
    runtime = NodeRuntime(config, transport=object())

    manifest = runtime.manifest()
    assert manifest["browser"] == {
        "enabled": False, "backend": "unavailable",
    }
    assert "browser.session" not in manifest["capabilities"]
    assert not (runtime.executor.allowed_tools & BROWSER_TOOLS)

    command = PlatformCommand.create(
        command_id="browser-no-driver", target_node="node-a",
        action="tool.browser.open", resource_id="workspace",
        arguments={"url": "http://127.0.0.1:3000"},
        retry_class="manual_only", expires_at=9_999_999_999,
        run_id="run-a",
    ).signed(PRIVATE_KEY).as_dict()
    result = runtime.client.handle(command)
    assert result["status"] == "failed"
    assert result["result"]["error_code"] == "backend_unavailable"
    assert runtime.journal.get("browser-no-driver")["state"] == "failed"


def test_node_runtime_forces_browser_external_network_off_without_egress_proof(tmp_path):
    from tools.platform.browser_backend import BrowserBackendError

    class Driver:
        def open(self, url):
            raise AssertionError("external URL must be rejected before driver access")

    config = NodeRuntimeConfig(
        node_id="node-a", credential="node-a:secret",
        workspace_root=tmp_path / "workspace",
        journal_path=tmp_path / "journal.db",
        hub_url="https://hub.invalid", public_key=PUBLIC_KEY,
        capabilities=frozenset({"browser.session"}), browser_enabled=True,
        browser_network_enabled=True,
        browser_allowed_origins=("https://one.example",),
        browser_driver_factory=Driver,
    )
    runtime = NodeRuntime(config, transport=object())

    assert runtime.browser_backend.network_enabled is False
    with pytest.raises(BrowserBackendError, match="network_disabled"):
        runtime.browser_backend.execute(
            "browser.open", {"url": "https://one.example/"}, run_id="run-a",
        )


class _Transport:
    def __init__(self, client):
        self.client = client
        self.poll_commands = []

    def post_json(self, url, body, headers):
        path = "/" + url.split("/", 3)[-1]
        response = self.client.post(path, json=body, headers=headers)
        payload = response.get_json()
        if path.endswith("/poll"):
            self.poll_commands.extend(payload.get("commands", []))
        return response.status_code, payload


def _write_shim(directory: Path, name: str, record: Path):
    path = directory / name
    path.write_text(
        "#!/bin/sh\n"
        f"printf '%s\n' \"$0|$@\" >> {str(record)!r}\n"
        "printf 'token=runtime-secret /tmp/runtime-private\n'\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


@pytest.mark.parametrize(
    ("adapter", "alias", "shim"),
    [
        ("systemd", "api.service", "journalctl"),
        ("supervisor", "api", "supervisorctl"),
        ("docker", "api", "docker"),
    ],
)
def test_runtime_executes_fixed_log_adapter_through_hub_and_journal(
    tmp_path, adapter, alias, shim
):
    app = create_app(FleetConfig.from_root(
        tmp_path, dev_operator=OWNER, platform_enabled=True,
        service_monitoring_enabled=True, service_actions_enabled=True,
        platform_command_signing_raw=PRIVATE_KEY, platform_require_command_signature=True,
    ))
    repo = app.extensions["fleet"]["platform_repository"]
    commands = app.extensions["fleet"]["platform_commands"]
    repo.upsert_node(OWNER, {"node_id": "node-a", "label": "A"})
    repo.provision_node_credential(OWNER, "node-a", secret="a" * 40)
    health = app.extensions["fleet"]["services"]["service_health"]
    health.register(OWNER, {
        "service_id": "api", "node_id": "node-a",
        "adapter": adapter, "target_alias": alias,
        "allowed_actions": ["inspect"],
    })
    original = PlatformCommand.create(
        command_id="logs-original", target_node="node-a", owner_id=OWNER,
        action="tool.service.read_logs", resource_id="workspace",
        arguments={
            "service_id": "api", "window_s": 60, "max_bytes": 1024,
            "adapter": adapter, "target_alias": alias,
            "service_version": 1,
        },
        retry_class="read_only", expires_at=9_999_999_999,
    )
    commands.enqueue(original, idempotency_key=original.command_id)
    commands.mark_unknown(original.command_id, reason="receipt_timeout")

    requested = app.test_client().post(
        "/api/platform/v1/commands/logs-original/postcheck"
    )
    assert requested.status_code == 202

    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    record = tmp_path / "argv.txt"
    _write_shim(shim_dir, shim, record)
    runtime = NodeRuntime(NodeRuntimeConfig(
        node_id="node-a", credential="node-a:" + "a" * 40,
        workspace_root=tmp_path / "workspace",
        journal_path=tmp_path / "node-journal.db",
        hub_url="https://hub.invalid", public_key=PUBLIC_KEY,
        worker_id="fixture-worker",
        capabilities=frozenset({SERVICE_LOGS_POSTCHECK_ACTION}),
        log_path_prefix=(shim_dir,),
    ), transport=_Transport(app.test_client()))
    # Replace the runtime transport with a handle we can inspect.
    transport = _Transport(app.test_client())
    runtime = NodeRuntime(runtime.config, transport=transport)

    first = runtime.poll_once()
    assert first == {"ok": True, "commands": 1, "receipts": 1}
    assert record.read_text(encoding="utf-8").count(shim) == 1
    argv_text = record.read_text(encoding="utf-8")
    assert shim in argv_text and alias in argv_text

    checked = app.test_client().get(
        "/api/platform/v1/commands/logs-original/postcheck"
    )
    assert checked.status_code == 200
    payload = checked.get_json()["postcheck"]
    assert payload["state"] == "matched"
    assert "runtime-secret" not in checked.get_data(as_text=True)
    assert commands.get("logs-original")["status"] == "unknown"

    command = transport.poll_commands[0]
    duplicate = runtime.client.handle(command)
    assert duplicate["status"] == "succeeded"
    assert record.read_text(encoding="utf-8").count(shim) == 1


def test_runtime_without_log_capability_does_not_start_subprocess(tmp_path):
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    record = tmp_path / "argv.txt"
    _write_shim(shim_dir, "journalctl", record)
    runtime = NodeRuntime(NodeRuntimeConfig(
        node_id="node-a", credential="node-a:secret",
        workspace_root=tmp_path / "workspace",
        journal_path=tmp_path / "node-journal.db",
        hub_url="https://hub.invalid", public_key=PUBLIC_KEY,
        capabilities=frozenset(), log_path_prefix=(shim_dir,),
    ), transport=object())

    command = PlatformCommand.create(
        command_id="disabled-logs", target_node="node-a", owner_id=OWNER,
        action=SERVICE_LOGS_POSTCHECK_ACTION, resource_id="workspace",
        arguments=_log_command(command_id="disabled-logs")["arguments"],
        retry_class="read_only", expires_at=9_999_999_999,
    )
    result = runtime.client.handle(command.signed(PRIVATE_KEY).signed(PRIVATE_KEY).as_dict())

    assert result["status"] == "failed"
    assert runtime.journal.get("disabled-logs")["result"]["error_code"] == "capability_unavailable"
    assert not record.exists()


def test_runtime_without_inspect_capability_does_not_start_subprocess(tmp_path):
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    record = tmp_path / "argv.txt"
    _write_shim(shim_dir, "systemctl", record)
    runtime = NodeRuntime(NodeRuntimeConfig(
        node_id="node-a", credential="node-a:secret",
        workspace_root=tmp_path / "workspace",
        journal_path=tmp_path / "node-journal.db",
        hub_url="https://hub.invalid", public_key=PUBLIC_KEY,
        capabilities=frozenset(), service_path_prefix=(shim_dir,),
    ), transport=object())

    command = PlatformCommand.create(
        command_id="disabled-inspect", target_node="node-a", owner_id=OWNER,
        action=SERVICE_POSTCHECK_ACTION, resource_id="api",
        arguments=_inspect_command(command_id="disabled-inspect")["arguments"],
        retry_class="read_only", expires_at=9_999_999_999,
    )
    result = runtime.client.handle(command.signed(PRIVATE_KEY).signed(PRIVATE_KEY).as_dict())

    assert result["status"] == "failed"
    assert runtime.journal.get("disabled-inspect")["result"]["error_code"] == "capability_unavailable"
    assert not record.exists()


@pytest.mark.parametrize(
    ("adapter", "alias", "shim"),
    [
        ("systemd", "api.service", "systemctl"),
        ("supervisor", "api", "supervisorctl"),
        ("docker", "api", "docker"),
    ],
)
def test_runtime_executes_fixed_inspect_adapter_through_subprocess_and_journal(
    tmp_path, adapter, alias, shim
):
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    record = tmp_path / "argv.txt"
    _write_shim(shim_dir, shim, record)
    config = NodeRuntimeConfig(
        node_id="node-a", credential="node-a:secret",
        workspace_root=tmp_path / "workspace",
        journal_path=tmp_path / "node-journal.db",
        hub_url="https://hub.invalid", public_key=PUBLIC_KEY, worker_id="inspect-fixture",
        capabilities=frozenset({SERVICE_POSTCHECK_ACTION}),
        service_path_prefix=(shim_dir,),
    )
    runtime = NodeRuntime(config, transport=object())

    command = _inspect_command(adapter=adapter, alias=alias)
    result = runtime.client.handle(PlatformCommand.create(
        command_id=command["command_id"], target_node="node-a", owner_id=OWNER,
        action=command["action"], resource_id=command["resource_id"],
        arguments=command["arguments"], retry_class="read_only",
        expires_at=9_999_999_999,
    ).signed(PRIVATE_KEY).as_dict())

    assert result["status"] == "succeeded"
    assert result["result"]["result"]["state"] == "healthy"
    argv_text = record.read_text(encoding="utf-8")
    assert shim in argv_text and alias in argv_text
    assert "runtime-secret" not in str(result)
    assert str(tmp_path) not in str(result)

    duplicate = runtime.client.handle(PlatformCommand.create(
        command_id=command["command_id"], target_node="node-a", owner_id=OWNER,
        action=command["action"], resource_id=command["resource_id"],
        arguments=command["arguments"], retry_class="read_only",
        expires_at=9_999_999_999,
    ).signed(PRIVATE_KEY).as_dict())
    assert duplicate["status"] == "succeeded"
    assert record.read_text(encoding="utf-8").count(shim) == 1


def test_runtime_inspect_capability_closes_hub_postcheck_and_health_evidence(tmp_path):
    app = create_app(FleetConfig.from_root(
        tmp_path, dev_operator=OWNER, platform_enabled=True,
        service_monitoring_enabled=True, service_actions_enabled=True,
        platform_command_signing_raw=PRIVATE_KEY, platform_require_command_signature=True,
    ))
    repo = app.extensions["fleet"]["platform_repository"]
    commands = app.extensions["fleet"]["platform_commands"]
    repo.upsert_node(OWNER, {"node_id": "node-a", "label": "A"})
    repo.provision_node_credential(OWNER, "node-a", secret="a" * 40)
    health = app.extensions["fleet"]["services"]["service_health"]
    health.register(OWNER, {
        "service_id": "api", "node_id": "node-a", "adapter": "systemd",
        "target_alias": "api.service", "allowed_actions": ["inspect"],
    })
    service = health.repository.get_service(OWNER, "api")
    original = PlatformCommand.create(
        command_id="inspect-hub", target_node="node-a", owner_id=OWNER,
        action="service.inspect", resource_id="api",
        arguments={
            "adapter": service["adapter"], "target_alias": service["target_alias"],
            "service_version": service["version"], "action": "inspect",
        }, retry_class="read_only", expires_at=9_999_999_999,
    )
    commands.enqueue(original, idempotency_key=original.command_id)
    commands.mark_unknown(original.command_id, reason="receipt_timeout")
    client = app.test_client()
    requested = client.post("/api/platform/v1/commands/inspect-hub/postcheck")
    assert requested.status_code == 202

    shim_dir = tmp_path / "inspect-shims"
    shim_dir.mkdir()
    record = tmp_path / "inspect-argv.txt"
    _write_shim(shim_dir, "systemctl", record)
    transport = _Transport(client)
    runtime = NodeRuntime(NodeRuntimeConfig(
        node_id="node-a", credential="node-a:" + "a" * 40,
        workspace_root=tmp_path / "workspace",
        journal_path=tmp_path / "inspect-journal.db",
        hub_url="https://hub.invalid", public_key=PUBLIC_KEY, worker_id="inspect-hub-fixture",
        capabilities=frozenset({SERVICE_POSTCHECK_ACTION}),
        service_path_prefix=(shim_dir,),
    ), transport=transport)

    assert runtime.poll_once() == {"ok": True, "commands": 1, "receipts": 1}
    checked = client.get("/api/platform/v1/commands/inspect-hub/postcheck")
    assert checked.status_code == 200
    payload = checked.get_json()["postcheck"]
    assert payload["state"] == "matched"
    assert payload["result"]["state"] == "healthy"
    assert "runtime-secret" not in checked.get_data(as_text=True)
    assert str(tmp_path) not in checked.get_data(as_text=True)
    assert commands.get("inspect-hub")["status"] == "unknown"
    evidence = health.repository.list_evidence(OWNER, "api")
    assert any(item["source"] == "service_postcheck" and item["state"] == "healthy" for item in evidence)
    assert record.read_text(encoding="utf-8").count("systemctl") == 1


def test_runtime_rejects_unsigned_commands_without_touching_workspace(tmp_path):
    runtime = NodeRuntime(NodeRuntimeConfig(
        node_id='node-a', credential='node-a:secret',
        workspace_root=tmp_path / 'workspace', journal_path=tmp_path / 'journal.db',
        hub_url='https://hub.invalid', capabilities=frozenset({'workspace.write'}),
    ))
    command = PlatformCommand.create(
        command_id='unsigned-write', target_node='node-a', owner_id=OWNER,
        action='tool.workspace.write', resource_id='workspace',
        arguments={'path': 'unsigned.txt', 'content': 'untrusted'},
        expires_at=9_999_999_999,
    )
    assert runtime.client.handle(command.as_dict())['status'] == 'rejected'
    assert runtime.journal.get(command.command_id) is None
    assert not (tmp_path / 'workspace' / 'unsigned.txt').exists()


@pytest.mark.parametrize('mutation', ['unsigned', 'arguments', 'wrong_key', 'wrong_target', 'expired'])
def test_runtime_trusted_key_rejects_invalid_envelopes(tmp_path, mutation):
    runtime = NodeRuntime(NodeRuntimeConfig(
        node_id='node-a', credential='node-a:secret', public_key=PUBLIC_KEY,
        workspace_root=tmp_path / 'workspace', journal_path=tmp_path / 'journal.db',
        hub_url='https://hub.invalid', capabilities=frozenset({'workspace.write'}),
    ))
    command = PlatformCommand.create(
        command_id='signed-write', target_node='node-a', owner_id=OWNER,
        action='tool.workspace.write', resource_id='workspace',
        arguments={'path': 'signed.txt', 'content': 'trusted'},
        expires_at=9_999_999_999,
    )
    invalid = command.signed(PRIVATE_KEY).as_dict()
    if mutation == 'unsigned': invalid['signature'] = None
    elif mutation == 'arguments': invalid['arguments']['content'] = 'tampered'
    elif mutation == 'wrong_key': invalid = command.signed(generate_ed25519_keypair()[0]).as_dict()
    elif mutation == 'wrong_target': invalid['target_node'] = 'node-b'
    elif mutation == 'expired': invalid['expires_at'] = 1
    assert runtime.client.handle(invalid)['status'] == 'rejected'
    assert runtime.journal.get(command.command_id) is None
    assert not (tmp_path / 'workspace' / 'signed.txt').exists()
    assert runtime.client.handle(command.signed(PRIVATE_KEY).as_dict())['status'] == 'succeeded'
    assert (tmp_path / 'workspace' / 'signed.txt').read_text() == 'trusted'


def test_signed_command_cannot_bypass_empty_node_capabilities(tmp_path):
    runtime = NodeRuntime(NodeRuntimeConfig(
        node_id='node-a', credential='node-a:secret', public_key=PUBLIC_KEY,
        workspace_root=tmp_path / 'workspace', journal_path=tmp_path / 'journal.db',
        hub_url='https://hub.invalid', capabilities=frozenset(),
    ))
    command = PlatformCommand.create(
        command_id='no-capability', target_node='node-a', owner_id=OWNER,
        action='tool.workspace.write', resource_id='workspace',
        arguments={'path': 'denied.txt', 'content': 'deny'}, expires_at=9_999_999_999,
    )
    assert runtime.client.handle(command.signed(PRIVATE_KEY).as_dict())['status'] == 'failed'
    assert not (tmp_path / 'workspace' / 'denied.txt').exists()


def test_node_runtime_submit_gate_requires_browser_and_default_off(tmp_path):
    from tools.platform.browser_backend import BrowserBackendError

    class Driver:
        def __init__(self):
            self.submits = []

        def open(self, _url):
            return None

        def submit(self, selector):
            self.submits.append(selector)
            return {"state": "submitted"}

    with pytest.raises(ValueError, match="browser submit requires browser"):
        NodeRuntimeConfig(
            node_id="node-a", credential="node-a:secret",
            workspace_root=tmp_path / "workspace",
            journal_path=tmp_path / "journal.db",
            hub_url="https://hub.invalid", public_key=PUBLIC_KEY,
            capabilities=frozenset(), browser_enabled=False,
            browser_submit_enabled=True,
        )

    config = NodeRuntimeConfig(
        node_id="node-a", credential="node-a:secret",
        workspace_root=tmp_path / "workspace",
        journal_path=tmp_path / "journal.db",
        hub_url="https://hub.invalid", public_key=PUBLIC_KEY,
        capabilities=frozenset({"browser.session"}), browser_enabled=True,
        browser_driver_factory=Driver,
    )
    runtime = NodeRuntime(config, transport=object())
    assert "browser.submit" not in runtime.executor.allowed_tools
    assert runtime.browser_backend.submit_enabled is False

    submit_config = NodeRuntimeConfig(
        node_id="node-a", credential="node-a:secret",
        workspace_root=tmp_path / "workspace-submit",
        journal_path=tmp_path / "journal-submit.db",
        hub_url="https://hub.invalid", public_key=PUBLIC_KEY,
        capabilities=frozenset({"browser.session"}), browser_enabled=True,
        browser_submit_enabled=True, browser_driver_factory=Driver,
    )
    submit_runtime = NodeRuntime(submit_config, transport=object())
    assert "browser.submit" in submit_runtime.executor.allowed_tools
    assert submit_runtime.browser_backend.submit_enabled is True
    opened = submit_runtime.browser_backend.execute(
        "browser.open", {"url": "http://localhost:3000"}, run_id="run-a")
    receipt = submit_runtime.browser_backend.execute(
        "browser.submit",
        {"session_id": opened["session_id"], "selector": "#go"}, run_id="run-a")
    assert receipt["result"] == {"state": "submitted"}
