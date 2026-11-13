"""Local two-identity Hub/Node handoff; this is not two physical machines."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import ctypes
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any, Iterator

import pytest
from werkzeug.serving import make_server

from hub.bootstrap import create_app
from hub.config import FleetConfig
from fleet_core.control import generate_ed25519_keypair
from hub.domain.artifact_receive import RegisteredNodeInputs
from hub.infrastructure.workflow_repository import WorkflowRepositoryError
from tools.platform.artifact_transfer import InputDirectory
from hub.platform.artifacts import ArtifactStore
from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig
from tools.transport import Transport
from test_workflow_coordinator import make_setup
from test_workflow_repository import OWNER, WORKSPACE


@pytest.fixture
def handoff(tmp_path: Path, request: Any) -> Any:
    data = b"x" * (3 * 65536) if getattr(request, "param", None) == "large" else b"immutable source package\n"
    store = ArtifactStore(tmp_path / "artifacts")
    source = tmp_path / "source.tar"
    source.write_bytes(data)
    artifact = store.put_file(OWNER, WORKSPACE, source)
    revision = None
    if getattr(request, "param", None) == "source_bundle":
        from test_workflow_source import repo_with_commit, git
        from tools.platform.workflow_source import SourceExporter
        repo, base, _head, _tree = repo_with_commit(tmp_path)
        (repo / "test_payload.py").write_text('from pathlib import Path\ndef test_payload():\n    assert Path("src.txt").read_text() == "fixed source\\n"\n', encoding="utf-8")
        git(repo, "add", "test_payload.py")
        git(repo, "commit", "-qm", "fixed validation recipe")
        head, tree = git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "HEAD^{tree}")
        exported = asyncio.run(SourceExporter(repo, store).export(OWNER, WORKSPACE,
            repository_id="repo-a", base_commit=base, head_commit=head, tree_id=tree))
        artifact, revision = exported.manifest, exported.revision
        data = store.read(OWNER, WORKSPACE, artifact["artifact_id"])
    reference = {key: artifact[key] for key in ("artifact_id", "sha256", "size")}
    setup = make_setup(tmp_path, source=reference)
    private, public = generate_ed25519_keypair()
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER, platform_enabled=True,
        platform_db=setup.repository.db_path, platform_artifact_root=store.root,
        platform_workflows_enabled=True, platform_workflow_dispatch_enabled=True,
        platform_artifact_transfer_enabled=True, platform_command_signing_raw=private, serve_frontend=False),
        repositories={"workflow_clock": setup.clock, "workflow_steps": setup.registrations,
            "workflow_executors": ((OWNER, WORKSPACE, "node-a", setup.executor),),
            "workflow_node_inputs": (RegisteredNodeInputs(OWNER, WORKSPACE, "node-a", (("source", "source", ".tar"),)),)})
    platform = app.extensions["fleet"]["platform_repository"]
    credentials = {}
    for node in ("node-a", "node-b"):
        platform.upsert_node(OWNER, {"node_id": node, "label": node, "capabilities": {"artifact.receive": True}})
        credentials[node] = platform.provision_node_credential(OWNER, node, secret=node[-1] * 40)["credential"]
    services = app.extensions["fleet"]["services"]
    services["platform_delivery"].repository.clock = setup.clock
    services["platform_delivery"].clock = setup.clock
    inputs = _private_node_directory(tmp_path / "node-inputs")
    def tick() -> Any:
        result = asyncio.run(services["workflow_coordinator"].tick(setup.clock()))
        setup.clock.now += 1
        assert not result.failures, [(e.code, e.__cause__) for e in result.failures]
        return result
    def command() -> dict[str, Any]:
        commands = services["platform_delivery"].repository
        with setup.repository.transaction(write=False) as connection:
            row = connection.execute("SELECT command_id FROM platform_commands WHERE action='artifact.receive' LIMIT 1").fetchone()
        assert row is not None
        return commands.get(row[0])
    return type("Handoff", (), dict(app=app, services=services, setup=setup, data=data, artifact=artifact,
        credentials=credentials, public=public, inputs=inputs, revision=revision, tmp=tmp_path,
        tick=staticmethod(tick), command=staticmethod(command)))()


@contextmanager
def server(app: Any) -> Iterator[str]:
    httpd = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=httpd.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
        assert not thread.is_alive()


def runtime(handoff: Any, url: str, transport: Any = None) -> NodeRuntime:
    runtime_root = _private_node_directory(handoff.tmp / "node-runtime")
    return NodeRuntime(NodeRuntimeConfig(workspace_id=WORKSPACE, node_id="node-a", credential=handoff.credentials["node-a"],
        workspace_root=runtime_root / "workspace", journal_path=runtime_root / "commands.db",
        hub_url=url, public_key=handoff.public, worker_id="receiver",
        capabilities=frozenset({"artifact.receive"}),
        artifact_inputs=(("source", InputDirectory(handoff.inputs, frozenset({".tar"}))),)),
        clock=handoff.setup.clock, transport=transport or Transport(mode="direct", attempts=1))


def _private_node_directory(path: Path) -> Path:
    if os.name != "nt":
        path.mkdir(mode=0o700, exist_ok=True)
        return path
    from tools.platform import artifact_windows

    native = artifact_windows._native()
    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("length", artifact_windows.U32), ("descriptor", artifact_windows.HANDLE), ("inherit", artifact_windows.U32)]

    native._bind(native.kernel, "CreateDirectoryW", artifact_windows.U32,
        [ctypes.c_wchar_p, ctypes.POINTER(SecurityAttributes)])
    with native.private_security() as descriptor:
        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, 0)
        try:
            native.checked(native.kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)))
        except FileExistsError:
            with artifact_windows.root_handle(path):
                pass
    return path


def test_local_http_node_receipt_is_required_before_workflow_ready(handoff: Any) -> None:
    handoff.tick()
    steps = handoff.setup.repository.current_steps(OWNER, "workflow-a")
    assert steps[0].state == "waiting_node"
    assert steps[0].reason_code == "inputs_receiving"
    assert handoff.setup.executor.created == 0
    command = handoff.command()
    with server(handoff.app) as url:
        node = runtime(handoff, url)
        try:
            assert node.manifest()["capabilities"]["artifact.receive"]
            result = node.poll_once(lease_s=60)
            assert result == {"ok": True, "commands": 1, "receipts": 1}
            target = handoff.inputs / (command["grant_id"] + ".tar")
            assert hashlib.sha256(target.read_bytes()).hexdigest() == handoff.artifact["sha256"]
            assert node.artifact_receiver.pending_receipts() == ()
            assert handoff.command()["status"] == "succeeded"
            assert handoff.setup.repository.current_steps(OWNER, "workflow-a")[0].state == "waiting_node"
            handoff.tick()
            assert handoff.setup.repository.current_steps(OWNER, "workflow-a")[0].state == "ready"
            handoff.tick()
            handoff.tick()
            assert handoff.setup.executor.created == 1
        finally:
            node.close_all()


def test_lost_receipt_response_restarts_from_durable_outbox_without_redownload(handoff: Any) -> None:
    class DropReceiptResponse(Transport):
        dropped = False
        downloads = 0
        def post_json(self, url: str, *args: Any, **kwargs: Any) -> Any:
            result = super().post_json(url, *args, **kwargs)
            if url.endswith("/receipts") and not self.dropped:
                self.dropped = True
                raise OSError("response lost after Hub commit")
            return result
        @contextmanager
        def get_stream(self, *args: Any, **kwargs: Any) -> Iterator[Any]:
            self.downloads += 1
            with super().get_stream(*args, **kwargs) as response:
                yield response
    handoff.tick()
    transport = DropReceiptResponse(mode="direct", attempts=1)
    with server(handoff.app) as url:
        node = runtime(handoff, url, transport)
        assert node.poll_once()["receipts"] == 0
        assert len(node.artifact_receiver.pending_receipts()) == 1
        node.close_all()
        restarted = runtime(handoff, url, transport)
        try:
            assert restarted.poll_once() == {"ok": True, "commands": 0, "receipts": 1}
            assert restarted.artifact_receiver.pending_receipts() == ()
            assert transport.downloads == 1
        finally:
            restarted.close_all()


def lease(handoff: Any) -> tuple[dict[str, Any], dict[str, str]]:
    handoff.tick()
    client = handoff.app.test_client()
    credential = {"X-Platform-Node-Credential": handoff.credentials["node-a"]}
    polled = client.post("/api/platform/v1/nodes/poll", json={"worker_id": "receiver"}, headers=credential)
    assert polled.status_code == 200
    command = polled.get_json()["commands"][0]
    return command, credential | {"X-Platform-Command-ID": command["command_id"], "X-Platform-Worker-ID": "receiver"}


def test_input_download_machine_command_scope_and_no_arbitrary_path(handoff: Any) -> None:
    command, headers = lease(handoff)
    path = "/api/platform/v1/node/artifact-inputs/" + command["grant_id"] + "/content"
    client = handoff.app.test_client()
    assert client.get(path).status_code == 403
    assert client.get(path, headers=headers | {"X-Platform-Node-Credential": handoff.credentials["node-b"]}).status_code == 404
    assert client.get(path, headers=headers | {"X-Platform-Command-ID": "wrong"}).status_code == 404
    assert client.get(path, headers=headers | {"X-Platform-Worker-ID": "wrong"}).status_code == 409
    assert client.get(path + "?path=/etc/passwd", headers=headers).status_code == 400
    assert client.get(path, headers=headers | {"Range": "bytes=0-1"}).status_code == 400
    response = client.get(path, headers=headers, buffered=False)
    assert b"".join(response.response) == handoff.data
    assert "filename=" in response.headers["Content-Disposition"]
    response.close()


@pytest.mark.parametrize("change", ["hash", "alias", "missing", "bool_size", "extra"])
def test_forged_success_receipt_never_unblocks_input(handoff: Any, change: str) -> None:
    command, headers = lease(handoff)
    args = command["arguments"]
    result = {key: args[key] for key in ("artifact_id", "sha256", "size", "input_alias")}
    result["grant_id"] = command["grant_id"]
    if change == "missing":
        result.pop("size")
    elif change == "bool_size":
        result["size"] = True
    elif change == "extra":
        result["path"] = "/tmp/source"
    else:
        result["sha256" if change == "hash" else "input_alias"] = "different"
    response = handoff.app.test_client().post("/api/platform/v1/nodes/receipts", headers=headers,
        json={"command_id": command["command_id"], "worker_id": "receiver", "status": "succeeded", "result": result})
    assert response.status_code == 409, response.get_json()
    handoff.tick()
    assert handoff.setup.repository.current_steps(OWNER, "workflow-a")[0].state == "waiting_node"
    assert handoff.setup.executor.created == 0


@pytest.mark.parametrize("revoke", ["cancel", "frozen", "workspace", "node", "generation", "expiry", "lease"])
def test_revoked_or_stale_download_is_denied(handoff: Any, revoke: str) -> None:
    command, headers = lease(handoff)
    with handoff.setup.repository.transaction() as connection:
        if revoke == "cancel":
            connection.execute("UPDATE workflow_runs SET cancel_requested=1")
        elif revoke == "frozen":
            connection.execute("UPDATE workflow_schema_meta SET admission_mode='frozen'")
        elif revoke in {"workspace", "node"}:
            connection.execute("UPDATE " + ("workspaces" if revoke == "workspace" else "nodes") + " SET enabled=0")
        elif revoke == "generation":
            connection.execute("UPDATE workflow_steps SET is_current=0")
        else:
            connection.execute("UPDATE platform_commands SET " + ("expires_at" if revoke == "expiry" else "lease_until") + "=100")
    path = "/api/platform/v1/node/artifact-inputs/" + command["grant_id"] + "/content"
    assert handoff.app.test_client().get(path, headers=headers).status_code in {403, 409}


def test_direct_admission_cannot_treat_hub_artifact_ref_as_node_receipt(handoff: Any) -> None:
    handoff.tick()
    repository = handoff.app.extensions["fleet"]["repositories"]["workflows"]
    claim = repository.claim_due(OWNER, worker_id="direct", now=handoff.setup.clock(), lease_seconds=30)[0]
    with pytest.raises(WorkflowRepositoryError, match="input_receipt_required"):
        repository.admit_step(claim, step_key="develop", generation=1,
            expected_revision=repository.get(OWNER, "workflow-a").revision,
            target_key="node-a", capacity=1, reserve_tokens=1, reserve_requests=1, now=handoff.setup.clock())


@pytest.mark.parametrize("mode", ["pause", "drain"])
def test_pause_and_drain_allow_original_input_receive_to_finish(handoff: Any, mode: str) -> None:
    command, headers = lease(handoff)
    with handoff.setup.repository.transaction() as connection:
        if mode == "pause":
            connection.execute("UPDATE workflow_runs SET pause_requested=1")
        else:
            connection.execute("UPDATE workflow_schema_meta SET admission_mode='drain'")
    path = "/api/platform/v1/node/artifact-inputs/" + command["grant_id"] + "/content"
    response = handoff.app.test_client().get(path, headers=headers)
    assert response.status_code == 200
    assert response.data == handoff.data
    result = {key: command["arguments"][key] for key in ("artifact_id", "sha256", "size", "input_alias")}
    result["grant_id"] = command["grant_id"]
    receipt = handoff.app.test_client().post("/api/platform/v1/nodes/receipts", headers=headers,
        json={"command_id": command["command_id"], "worker_id": "receiver", "status": "succeeded", "result": result})
    assert receipt.status_code == 200
    handoff.tick()
    assert handoff.setup.executor.created == 0


def test_missing_registered_alias_keeps_step_waiting_without_dispatch(handoff: Any) -> None:
    handoff.services["artifact_inputs"].targets.clear()
    handoff.tick()
    step = handoff.setup.repository.current_steps(OWNER, "workflow-a")[0]
    assert (step.state, step.reason_code) == ("waiting_node", "input_alias_unregistered")
    assert step.resolved_snapshot is None
    handoff.tick()
    assert handoff.setup.executor.created == 0


def test_closed_gate_preserves_legacy_input_flow_and_hides_receive_route(handoff: Any) -> None:
    coordinator = handoff.services["workflow_coordinator"]
    coordinator.artifact_input_service = None
    coordinator.repository.artifact_input_public_key = None
    handoff.app.config["PLATFORM_ARTIFACT_TRANSFER_ENABLED"] = False
    handoff.tick()
    assert handoff.setup.repository.current_steps(OWNER, "workflow-a")[0].state == "ready"
    handoff.tick()
    assert handoff.setup.repository.current_steps(OWNER, "workflow-a")[0].state == "dispatching"
    assert handoff.app.test_client().get("/api/platform/v1/node/artifact-inputs/any/content",
        headers={"X-Platform-Node-Credential": handoff.credentials["node-a"]}).status_code == 404


@pytest.mark.parametrize("handoff", ["source_bundle"], indirect=True)
def test_received_git_bundle_is_rehashed_extracted_and_explicitly_validated(handoff: Any) -> None:
    from tools.platform.workflow_source import extract_source_bundle
    handoff.tick()
    command = handoff.command()
    with server(handoff.app) as url:
        node = runtime(handoff, url)
        try:
            assert node.poll_once()["receipts"] == 1
        finally:
            node.close_all()
    received = handoff.inputs / (command["grant_id"] + ".tar")
    class ReceivedInput:
        def read(self, owner: str, workspace: str, artifact_id: str) -> bytes:
            assert (owner, workspace, artifact_id) == (OWNER, WORKSPACE, handoff.artifact["artifact_id"])
            return received.read_bytes()
    checkout = handoff.tmp / "validation-checkout"
    members = extract_source_bundle(ReceivedInput(), OWNER, WORKSPACE, handoff.artifact["artifact_id"],
        checkout, expected_revision=handoff.revision)
    assert set(members) == {"src.txt", "test_payload.py"}
    assert not (checkout / ".env").exists()
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "--junitxml=" + str(handoff.tmp / "validation.xml")],
        cwd=checkout, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert received.read_bytes() == handoff.data


@pytest.mark.parametrize("confirmed", [False, True])
def test_backup_keeps_input_grant_and_receipt_without_fabricating_node_ready(handoff: Any, confirmed: bool) -> None:
    from contextlib import closing
    import sqlite3
    from hub.infrastructure.platform_backup import PlatformBackupService
    if confirmed:
        handoff.tick()
        with server(handoff.app) as url:
            node = runtime(handoff, url)
            try:
                assert node.poll_once()["receipts"] == 1
            finally:
                node.close_all()
    else:
        handoff.tick()
    command = handoff.command()
    backup = PlatformBackupService()
    root, restored = handoff.tmp / "backup", handoff.tmp / "restore"
    backup.create_backup(handoff.setup.repository.db_path, handoff.tmp / "artifacts", root)
    backup.restore_backup(root, restored)
    with closing(sqlite3.connect(restored / "platform.db")) as connection:
        assert connection.execute("SELECT admission_mode FROM workflow_schema_meta").fetchone()[0] == "frozen"
        row = connection.execute("SELECT command_id,grant_id,status,result FROM platform_commands WHERE command_id=?", (command["command_id"],)).fetchone()
        assert row[0:3] == (command["command_id"], command["grant_id"], "succeeded" if confirmed else "queued")
        assert (row[3] is not None) == confirmed
        assert connection.execute("SELECT state FROM workflow_steps").fetchone()[0] == "waiting_node"


def test_hub_download_slots_bound_global_and_node_streams_and_release(handoff: Any) -> None:
    from contextlib import ExitStack
    from hub.domain.artifact_receive import ArtifactReceiveError
    repository = handoff.services["artifact_inputs"].repository
    with ExitStack() as stack:
        stack.enter_context(repository.download_slot(OWNER, "node-a"))
        stack.enter_context(repository.download_slot(OWNER, "node-a"))
        with pytest.raises(ArtifactReceiveError, match="transfer_concurrency_limit"):
            with repository.download_slot(OWNER, "node-a"):
                pytest.fail("third node stream admitted")
        stack.enter_context(repository.download_slot(OWNER, "node-b"))
        stack.enter_context(repository.download_slot(OWNER, "node-b"))
        with pytest.raises(ArtifactReceiveError, match="transfer_concurrency_limit"):
            with repository.download_slot(OWNER, "node-c"):
                pytest.fail("fifth global stream admitted")
    with repository.download_slot(OWNER, "node-a"):
        assert True


def test_closing_http_stream_releases_download_slot(handoff: Any) -> None:
    command, headers = lease(handoff)
    client = handoff.app.test_client()
    path = "/api/platform/v1/node/artifact-inputs/" + command["grant_id"] + "/content"
    first = client.get(path, headers=headers, buffered=False)
    second = client.get(path, headers=headers, buffered=False)
    try:
        denied = client.get(path, headers=headers, buffered=False)
        assert denied.status_code == 429
        first.close()
        resumed = client.get(path, headers=headers, buffered=False)
        assert resumed.status_code == 200
        resumed.close()
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("handoff", ["large"], indirect=True)
@pytest.mark.parametrize("revoke", ["disable", "rotate"])
def test_stream_rechecks_original_machine_credential_between_chunks(handoff: Any, revoke: str) -> None:
    from hub.domain.artifact_receive import ArtifactReceiveError
    command, headers = lease(handoff)
    path = "/api/platform/v1/node/artifact-inputs/" + command["grant_id"] + "/content"
    response = handoff.app.test_client().get(path, headers=headers, buffered=False)
    assert len(next(response.response)) == 65536
    platform = handoff.app.extensions["fleet"]["platform_repository"]
    if revoke == "rotate":
        platform.provision_node_credential(OWNER, "node-a", secret="r" * 40)
    else:
        with handoff.setup.repository.transaction() as connection:
            connection.execute("UPDATE node_credentials SET enabled=0 WHERE node_id='node-a'")
    try:
        with pytest.raises(ArtifactReceiveError, match="receive_credential_revoked"):
            next(response.response)
    finally:
        response.close()


@pytest.mark.parametrize("revoke", ["disable", "rotate"])
def test_receive_receipt_rechecks_credential_in_write_transaction(handoff: Any, monkeypatch: Any, revoke: str) -> None:
    command, headers = lease(handoff)
    delivery = handoff.services["platform_delivery"]
    original = delivery.receipt

    def revoke_after_auth(*args: Any, **kwargs: Any) -> Any:
        if revoke == "rotate":
            platform = handoff.app.extensions["fleet"]["platform_repository"]
            platform.provision_node_credential(OWNER, "node-a", secret="r" * 40)
        else:
            with handoff.setup.repository.transaction() as connection:
                connection.execute("UPDATE node_credentials SET enabled=0 WHERE node_id='node-a'")
        return original(*args, **kwargs)

    monkeypatch.setattr(delivery, "receipt", revoke_after_auth)
    result = {key: command["arguments"][key] for key in ("artifact_id", "sha256", "size", "input_alias")}
    result["grant_id"] = command["grant_id"]
    response = handoff.app.test_client().post("/api/platform/v1/nodes/receipts", headers=headers,
        json={"command_id": command["command_id"], "worker_id": "receiver", "status": "succeeded", "result": result})
    assert response.status_code == 409, response.get_json()
    assert response.get_json()["error"] == "receive_credential_revoked"
    assert handoff.command()["status"] == "leased"
