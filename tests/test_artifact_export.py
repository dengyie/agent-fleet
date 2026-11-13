"""Node export uses real Hub routes, files, signatures and durable recovery."""
from __future__ import annotations

from contextlib import ExitStack
import asyncio
from dataclasses import replace
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tracemalloc
from typing import Any
from urllib.parse import urlsplit

import pytest

from hub.domain.artifact_export import ArtifactExportError
from fleet_core.platform_command import PlatformCommand
from hub.infrastructure.command_repository import CommandRepository
from tools.platform.artifact_export import ArtifactExporter, OutputFile
from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig
from tools.transport import Transport
from test_artifact_transfer import TransferFixture, transfer, transfer_http
from test_workflow_dispatch import setup
from test_workflow_repository import OWNER, WORKSPACE
from support.artifact_http import server


class HubTransport:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.requests: list[tuple[str, str, int]] = []
        self.drop: str | None = None

    def request_bounded_json(self, method: str, url: str, body: bytes | None,
                             headers: dict[str, str], **kwargs: Any) -> tuple[int, dict[str, Any]]:
        assert kwargs["deadline"] > 0 and not kwargs["should_cancel"]()
        parsed = urlsplit(url)
        assert parsed.netloc == "hub.test" and not parsed.query
        self.requests.append((method, parsed.path, len(body or b"")))
        response = self.client.open(parsed.path, method=method, data=body, headers=headers)
        stage = "commit" if parsed.path.endswith("/commit") else "part" if "/parts/" in parsed.path else "reserve" if method == "POST" else "status"
        if self.drop == stage and response.status_code in {200, 201}:
            self.drop = None
            raise OSError("fixture lost response after durable Hub write")
        return response.status_code, response.get_json()


@pytest.fixture
def export_node(transfer: TransferFixture, transfer_http: Any, tmp_path: Path) -> Any:
    app, client, headers = transfer_http
    delivery = app.extensions["fleet"]["services"]["platform_delivery"]
    delivery.clock = delivery.repository.clock = transfer.workflow.clock
    root = tmp_path / "outputs"
    root.mkdir(mode=0o700)
    source = root / "report.bin"
    source.write_bytes(b"payload")
    transport = HubTransport(client)
    command = CommandRepository(transfer.repository.db_path).get("export-a")
    def exporter(**changes: Any) -> ArtifactExporter:
        values = dict(node_id="node-a", credential=headers["X-Platform-Node-Credential"], worker_id="node-worker",
            hub_url="https://hub.test", public_key=transfer.public_key,
            outputs={(WORKSPACE, "report"): OutputFile(root, "report.bin")},
            journal_path=tmp_path / "exports.db", transport=transport, clock=transfer.workflow.clock)
        return ArtifactExporter(**(values | changes))
    return type("ExportFixture", (), dict(app=app, client=client, headers=headers, root=root,
        source=source, command=command, transport=transport, tmp=tmp_path, exporter=staticmethod(exporter)))()


def test_export_publishes_fixed_ref_and_replays_only_original_receipt(export_node: Any, transfer: TransferFixture) -> None:
    node = export_node.exporter()
    step_before = transfer.workflow.repository.current_steps(OWNER, "workflow-a")[0].state
    receipt = node.export(export_node.command)
    artifact = receipt["artifact"]
    assert receipt["output_key"] == "report"
    assert artifact["size"] == 7 and artifact["sha256"] == hashlib.sha256(b"payload").hexdigest()
    assert transfer.service.artifacts.read(OWNER, WORKSPACE, artifact["artifact_id"]) == b"payload"
    requests = list(export_node.transport.requests)
    assert export_node.exporter().export(export_node.command) == receipt
    assert export_node.transport.requests == requests
    assert node.pending_receipts() == (("export-a", receipt),)
    assert str(export_node.root) not in json.dumps(receipt)
    assert export_node.headers["X-Platform-Node-Credential"] not in node.journal.path.read_bytes().decode("utf-8", errors="replace")
    response = export_node.client.post("/api/platform/v1/nodes/receipts", headers=export_node.headers,
        json={"command_id": "export-a", "worker_id": "node-worker", "status": "succeeded", "result": receipt})
    assert response.status_code == 200, response.get_json()
    node.acknowledge("export-a", receipt)
    assert node.pending_receipts() == ()
    assert transfer.workflow.repository.current_steps(OWNER, "workflow-a")[0].state == step_before


@pytest.mark.parametrize("stage", ["reserve", "part", "commit"])
def test_lost_response_recovers_original_ids_without_duplicate_parts(export_node: Any, transfer: TransferFixture, stage: str) -> None:
    export_node.transport.drop = stage
    first = export_node.exporter()
    with pytest.raises(ArtifactExportError, match="export_io_failed"):
        first.export(export_node.command)
    saved = transfer.repository.find_by_command("export-a", "report")
    restarted = export_node.exporter()
    assert restarted.recover() == ()
    receipt = restarted.pending_receipts()[0][1]
    assert receipt["artifact"]["artifact_id"] == saved["artifact_id"]
    assert restarted.journal.get("export-a")["transfer_id"] == saved["transfer_id"]
    with transfer.repository.transaction() as connection:
        assert connection.execute("SELECT count(*) FROM artifact_transfers").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM artifact_transfer_parts").fetchone()[0] == 1
    assert sum(method == "PUT" for method, path, length in export_node.transport.requests) == 1


@pytest.mark.parametrize("change", [
    {"arguments": {"output_key": "report", "path": "/etc/passwd"}},
    {"arguments": {"output_key": "unknown"}}, {"resource_id": "other"}, {"target_node": "node-b"},
    {"expires_at": 104},
])
def test_signed_command_cannot_choose_unregistered_path(export_node: Any, transfer: TransferFixture, change: dict[str, Any]) -> None:
    values = {key: export_node.command[key] for key in ("command_id", "target_node", "action", "resource_id", "arguments", "run_id", "expires_at")}
    command = PlatformCommand.create(**(values | change)).signed(transfer.private_key).as_dict()
    with pytest.raises(ArtifactExportError):
        export_node.exporter().export(command)
    assert not export_node.transport.requests
    assert export_node.source.read_bytes() == b"payload"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "writable", "ancestor"])
def test_unsafe_local_outputs_never_reach_hub(export_node: Any, kind: str) -> None:
    output = OutputFile(export_node.root, "report.bin")
    if kind == "writable":
        export_node.source.chmod(0o666)
    elif kind == "directory":
        output = OutputFile(export_node.tmp, "outputs")
    elif kind == "ancestor":
        alias = export_node.tmp / "alias"
        alias.symlink_to(export_node.root, target_is_directory=True)
        output = OutputFile(alias, "report.bin")
    else:
        alias = export_node.root / "linked.bin"
        if kind == "symlink":
            alias.symlink_to(export_node.source)
        else:
            alias.hardlink_to(export_node.source)
        output = OutputFile(export_node.root, "linked.bin")
    with pytest.raises(ArtifactExportError, match="unsafe_output_path"):
        export_node.exporter(outputs={(WORKSPACE, "report"): output}).export(export_node.command)
    assert not export_node.transport.requests
    assert export_node.source.read_bytes() == b"payload"


@pytest.mark.parametrize("path", ["", ".", "..", "../file", "/etc/passwd", "a/../../file", "a\\b", "a\x00b", "a\ud800"])
def test_output_registration_rejects_unsafe_paths(tmp_path: Path, path: str) -> None:
    with pytest.raises(ArtifactExportError, match="invalid_output_file"):
        OutputFile(tmp_path, path)


def test_changed_source_cannot_resume_old_reservation(export_node: Any, transfer: TransferFixture) -> None:
    export_node.transport.drop = "part"
    with pytest.raises(ArtifactExportError):
        export_node.exporter().export(export_node.command)
    export_node.source.write_bytes(b"changed")
    restarted = export_node.exporter()
    assert restarted.recover() == (("export-a", "export_source_changed"),)
    assert restarted.journal.get("export-a")["state"] == "reconciliation"
    saved = transfer.repository.find_by_command("export-a", "report")
    assert transfer.repository.reservation(saved["transfer_id"])["state"] == "uploading"
    assert not restarted.pending_receipts()


def test_zero_bytes_needs_no_put(export_node: Any) -> None:
    export_node.source.write_bytes(b"")
    receipt = export_node.exporter().export(export_node.command)
    assert receipt["artifact"]["size"] == 0
    assert not any(method == "PUT" for method, path, length in export_node.transport.requests)


def test_file_limit_and_cancellation_precede_network(export_node: Any) -> None:
    with pytest.raises(ArtifactExportError, match="artifact_too_large"):
        export_node.exporter(max_bytes=6).export(export_node.command)
    with pytest.raises(ArtifactExportError, match="export_cancelled"):
        export_node.exporter(should_cancel=lambda: True).export(export_node.command)
    assert not export_node.transport.requests


@pytest.mark.parametrize("parent_noise_bytes", [0, 12 * 1024 * 1024], ids=["quiet", "unrelated-allocation"])
def test_export_stream_uses_bounded_python_memory(tmp_path: Path, parent_noise_bytes: int) -> None:
    # tracemalloc covers the whole process. Keep this chain's unchanged 8 MiB
    # budget independent of allocations by other HTTP handlers or test helpers.
    probe = Path(__file__).parent / "fixtures" / "artifact_export_memory_probe.py"
    root = tmp_path / "memory-probe"
    tracemalloc.start()
    try:
        unrelated = bytearray(parent_noise_bytes)
        result = subprocess.run([sys.executable, str(probe), str(root)],
                                capture_output=True, text=True, timeout=30)
        _, parent_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(unrelated) == parent_noise_bytes
    assert parent_peak >= parent_noise_bytes
    measured = json.loads((root / "memory.json").read_text(encoding="utf-8"))
    assert measured["pid"] != os.getpid()
    assert measured["size"] == 12 * 1024 * 1024
    assert measured["peak_bytes"] < 8 * 1024 * 1024


@pytest.mark.parametrize("stage", ["part", "receipt"])
def test_runtime_recovers_upload_before_sending_terminal_receipt(export_node: Any, transfer: TransferFixture, stage: str) -> None:
    class DropPart(Transport):
        dropped = False
        def request_bounded_json(self, method: str, url: str, *args: Any, **kwargs: Any) -> Any:
            result = super().request_bounded_json(method, url, *args, **kwargs)
            if ((method == "PUT" and stage == "part") or (url.endswith("/receipts") and stage == "receipt")) and not self.dropped:
                self.dropped = True
                raise OSError("response lost after Hub stored part")
            return result
    with transfer.workflow.repository.transaction() as connection:
        connection.execute("UPDATE platform_commands SET status='queued',lease_owner=NULL,lease_until=NULL WHERE command_id='export-a'")
    transport = DropPart(mode="direct", attempts=1)
    with server(export_node.app) as url:
        config = NodeRuntimeConfig(node_id="node-a", credential=export_node.headers["X-Platform-Node-Credential"],
            worker_id="node-worker", hub_url=url, public_key=transfer.public_key, workspace_root=export_node.tmp / "workspace",
            journal_path=export_node.tmp / "commands.db", capabilities=frozenset({"artifact.export"}),
            artifact_outputs=((WORKSPACE, "report", OutputFile(export_node.root, "report.bin")),))
        first = NodeRuntime(config, transport=transport, clock=transfer.workflow.clock)
        try:
            assert first.manifest()["capabilities"]["artifact.export"]
            assert first.poll_once(lease_s=300) == {"ok": True, "commands": 1, "receipts": 0}
            assert CommandRepository(transfer.repository.db_path).get("export-a")["status"] == ("leased" if stage == "part" else "succeeded")
        finally:
            first.close_all()
        restarted = NodeRuntime(config, transport=transport, clock=transfer.workflow.clock)
        try:
            assert restarted.poll_once(lease_s=300) == {"ok": True, "commands": 0, "receipts": 1}
            assert not restarted.artifact_exporter.pending_receipts()
            assert CommandRepository(transfer.repository.db_path).get("export-a")["status"] == "succeeded"
        finally:
            restarted.close_all()


def test_hub_does_not_accept_forged_export_success_before_commit(export_node: Any) -> None:
    receipt = {"output_key": "report", "artifact": {"artifact_id": "invented", "size": 7, "sha256": "a" * 64}}
    response = export_node.client.post("/api/platform/v1/nodes/receipts", headers=export_node.headers,
        json={"command_id": "export-a", "worker_id": "node-worker", "status": "succeeded", "result": receipt})
    assert response.status_code == 409
    assert response.get_json()["error"] == "invalid_export_receipt"


@pytest.mark.parametrize("revoke", ["disable", "rotate"])
def test_export_receipt_transaction_rechecks_original_machine_credential(export_node: Any, monkeypatch: Any, revoke: str) -> None:
    receipt = export_node.exporter().export(export_node.command)
    delivery = export_node.app.extensions["fleet"]["services"]["platform_delivery"]
    platform = export_node.app.extensions["fleet"]["platform_repository"]
    original = delivery.receipt
    def after_auth(*args: Any, **kwargs: Any) -> Any:
        if revoke == "disable":
            platform.upsert_node(OWNER, {"node_id": "node-a", "label": "Disabled", "enabled": False})
        else:
            platform.provision_node_credential(OWNER, "node-a", secret="r" * 40)
        return original(*args, **kwargs)
    monkeypatch.setattr(delivery, "receipt", after_auth)
    response = export_node.client.post("/api/platform/v1/nodes/receipts", headers=export_node.headers,
        json={"command_id": "export-a", "worker_id": "node-worker", "status": "succeeded", "result": receipt})
    assert response.status_code == 409
    assert response.get_json()["error"] == "export_credential_revoked"


@pytest.mark.parametrize("revoke", ["disable", "rotate"])
def test_http_commit_cannot_publish_if_credential_changes_after_auth(export_node: Any, monkeypatch: Any, revoke: str) -> None:
    export_node.transport.drop = "part"
    node = export_node.exporter()
    with pytest.raises(ArtifactExportError):
        node.export(export_node.command)
    transfer_id = node.journal.get("export-a")["transfer_id"]
    service = export_node.app.extensions["fleet"]["services"]["artifact_transfers"]
    platform = export_node.app.extensions["fleet"]["platform_repository"]
    original = service.repository.publish_ready
    def before_transaction(*args: Any, **kwargs: Any) -> None:
        if revoke == "disable":
            platform.upsert_node(OWNER, {"node_id": "node-a", "label": "Disabled", "enabled": False})
        else:
            platform.provision_node_credential(OWNER, "node-a", secret="r" * 40)
        original(*args, **kwargs)
    monkeypatch.setattr(service.repository, "publish_ready", before_transaction)
    response = export_node.client.post("/api/platform/v1/node/artifact-transfers/" + transfer_id + "/commit",
        headers=export_node.headers, json={"command_id": "export-a", "size": 7, "sha256": hashlib.sha256(b"payload").hexdigest()})
    assert response.status_code == 409
    assert service.repository.reservation(transfer_id)["state"] != "ready"
    assert service.artifacts.get(OWNER, WORKSPACE, node.journal.get("export-a")["artifact_id"]) is None


def test_source_change_after_part_never_commits(export_node: Any, transfer: TransferFixture) -> None:
    original = export_node.transport.request_bounded_json
    def changed(method: str, *args: Any, **kwargs: Any) -> Any:
        result = original(method, *args, **kwargs)
        if method == "PUT":
            export_node.source.write_bytes(b"changed")
        return result
    export_node.transport.request_bounded_json = changed
    node = export_node.exporter()
    with pytest.raises(ArtifactExportError, match="export_source_changed"):
        node.export(export_node.command)
    assert node.journal.get("export-a")["state"] == "reconciliation"
    assert not node.pending_receipts()
    assert not any(path.endswith("/commit") for method, path, length in export_node.transport.requests)


@pytest.mark.parametrize("damage", ["command", "receipt"])
def test_damaged_ready_export_receipt_is_not_replayed(export_node: Any, damage: str) -> None:
    node = export_node.exporter()
    node.export(export_node.command)
    with node.journal.transaction() as connection:
        connection.execute("UPDATE artifact_exports SET " + damage + "='[]'")
    with pytest.raises(ArtifactExportError, match="export_reconciliation_required"):
        export_node.exporter().pending_receipts()
    assert node.journal.get("export-a")["state"] == "reconciliation"


def test_unknown_export_journal_schema_is_not_reinitialized(export_node: Any) -> None:
    node = export_node.exporter()
    with node.journal.transaction() as connection:
        connection.execute("UPDATE artifact_export_meta SET schema_version=2")
    with pytest.raises(ArtifactExportError, match="export_journal_schema"):
        export_node.exporter()


def test_three_uncertain_reservation_attempts_stop_automatic_replay(export_node: Any) -> None:
    node = export_node.exporter()
    for attempt in range(3):
        export_node.transport.drop = "reserve"
        with pytest.raises(ArtifactExportError, match="export_io_failed"):
            node.export(export_node.command)
    assert node.journal.get("export-a")["state"] == "reconciliation"
    assert not node.deferred("export-a")
    assert node.recover() == ()


def test_runtime_send_and_receive_share_two_local_slots(export_node: Any, transfer: TransferFixture) -> None:
    from tools.platform.artifact_transfer import InputDirectory
    root = export_node.tmp / "inputs"
    root.mkdir(mode=0o700)
    runtime = NodeRuntime(NodeRuntimeConfig(node_id="node-a", credential=export_node.headers["X-Platform-Node-Credential"],
        hub_url="https://hub.test", public_key=transfer.public_key, workspace_root=export_node.tmp / "workspace",
        journal_path=export_node.tmp / "commands.db", capabilities=frozenset({"artifact.export", "artifact.receive"}),
        artifact_inputs=(("source", InputDirectory(root)),),
        artifact_outputs=((WORKSPACE, "report", OutputFile(export_node.root, "report.bin")),)),
        clock=transfer.workflow.clock, transport=export_node.transport)
    try:
        with ExitStack() as slots:
            slots.enter_context(runtime.artifact_receiver._slot())
            slots.enter_context(runtime.artifact_exporter._slot("held"))
            with pytest.raises(ArtifactExportError, match="export_concurrency_limit"):
                runtime.artifact_exporter.export(export_node.command)
    finally:
        runtime.close_all()


@pytest.mark.parametrize("damage", ["missing-source", "expiry", "non-object-command"])
def test_corrupt_or_missing_pending_export_enters_reconciliation(export_node: Any, damage: str) -> None:
    export_node.transport.drop = "part"
    first = export_node.exporter()
    with pytest.raises(ArtifactExportError):
        first.export(export_node.command)
    if damage == "missing-source":
        export_node.source.rename(export_node.root / "preserved-original.bin")
    else:
        with first.journal.transaction() as connection:
            if damage == "expiry":
                connection.execute("UPDATE artifact_exports SET expires_at='invalid'")
            else:
                connection.execute("UPDATE artifact_exports SET command='[]'")
    restarted = export_node.exporter()
    errors = restarted.recover()
    assert errors and errors[0][0] == "export-a"
    assert restarted.journal.get("export-a")["state"] == "reconciliation"
    assert not restarted.deferred("export-a")


def _crash_export(root: Path, journal: Path, credential: str, public_key: bytes,
                  command: dict[str, Any], url: str, stage: str) -> None:
    class CrashTransport(Transport):
        def request_bounded_json(self, method: str, target: str, *args: Any, **kwargs: Any) -> Any:
            result = super().request_bounded_json(method, target, *args, **kwargs)
            if ((stage == "reservation" and method == "POST" and target.endswith("artifact-transfers"))
                    or (stage == "part" and method == "PUT")
                    or (stage == "commit" and target.endswith("/commit"))):
                os._exit(73)
            return result
    node = ArtifactExporter(node_id="node-a", credential=credential, worker_id="node-worker", hub_url=url,
        public_key=public_key, outputs={(WORKSPACE, "report"): OutputFile(root, "report.bin")},
        journal_path=journal, transport=CrashTransport(mode="direct", attempts=1), clock=lambda: 104.)
    if stage == "ready":
        original = node.journal.ready
        def ready(*args: Any, **kwargs: Any) -> None:
            original(*args, **kwargs)
            os._exit(73)
        node.journal.ready = ready
    node.export(command)


@pytest.mark.parametrize("stage", ["reservation", "part", "commit", "ready"])
def test_real_process_exit_recovers_one_original_artifact(export_node: Any, transfer: TransferFixture, stage: str) -> None:
    with server(export_node.app) as url:
        process = multiprocessing.get_context("spawn").Process(target=_crash_export,
            args=(export_node.root, export_node.tmp / "crash-exports.db", export_node.headers["X-Platform-Node-Credential"],
                transfer.public_key, export_node.command, url, stage))
        process.start()
        try:
            process.join(25)
            assert process.exitcode == 73
            original = transfer.repository.find_by_command("export-a", "report")
            restarted = export_node.exporter(hub_url=url, journal_path=export_node.tmp / "crash-exports.db",
                transport=Transport(mode="direct", attempts=1))
            assert restarted.recover() == ()
            receipt = restarted.pending_receipts()[0][1]
            assert receipt["artifact"]["artifact_id"] == original["artifact_id"]
            assert transfer.service.artifacts.read(OWNER, WORKSPACE, original["artifact_id"]) == b"payload"
            with transfer.repository.transaction() as connection:
                assert connection.execute("SELECT count(*) FROM artifact_transfers").fetchone()[0] == 1
                assert connection.execute("SELECT count(*) FROM artifact_transfer_parts").fetchone()[0] == 1
        finally:
            if process.is_alive():
                process.terminate()
                process.join(10)


def test_two_node_http_export_receive_and_fixed_source_validation(export_node: Any, transfer: TransferFixture) -> None:
    from hub.domain.artifact_receive import RegisteredNodeInputs
    from hub.domain.workflow_contract import resolve_contract
    from hub.domain.workflow_contract_types import ArtifactReference, AuthorizedInputs, CatalogRef, InputBinding
    from tools.platform.artifact_transfer import InputDirectory
    from hub.platform.artifacts import ArtifactStore
    from tools.platform.workflow_source import SourceExporter, extract_source_bundle
    from test_workflow_repository import registered
    from test_workflow_source import repo_with_commit, git

    repo, base, head, tree = repo_with_commit(export_node.tmp)
    (repo / "test_payload.py").write_text('from pathlib import Path\ndef test_payload():\n    assert Path("src.txt").read_text() == "fixed source\\n"\n', encoding="utf-8")
    git(repo, "add", "test_payload.py")
    git(repo, "commit", "-qm", "fixed validation recipe")
    head, tree = git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "HEAD^{tree}")
    local_store = ArtifactStore(export_node.tmp / "node-a-local-artifacts")
    bundle = asyncio.run(SourceExporter(repo, local_store).export(OWNER, WORKSPACE,
        repository_id="repo-a", base_commit=base, head_commit=head, tree_id=tree))
    export_node.source.write_bytes(local_store.read(OWNER, WORKSPACE, bundle.manifest["artifact_id"]))
    platform = export_node.app.extensions["fleet"]["platform_repository"]
    platform.upsert_node(OWNER, {"node_id": "node-b", "label": "Receiver"})
    credential_b = platform.provision_node_credential(OWNER, "node-b", secret="b" * 40)["credential"]
    with transfer.workflow.repository.transaction() as connection:
        connection.execute("UPDATE platform_commands SET status='queued',lease_owner=NULL,lease_until=NULL WHERE command_id='export-a'")
    with server(export_node.app) as url:
        producer = NodeRuntime(NodeRuntimeConfig(node_id="node-a", credential=export_node.headers["X-Platform-Node-Credential"],
            worker_id="node-worker", hub_url=url, public_key=transfer.public_key, workspace_root=export_node.tmp / "producer-workspace",
            journal_path=export_node.tmp / "producer-commands.db", capabilities=frozenset({"artifact.export"}),
            artifact_outputs=((WORKSPACE, "report", OutputFile(export_node.root, "report.bin")),)),
            clock=transfer.workflow.clock, transport=Transport(mode="direct", attempts=1))
        try:
            assert producer.poll_once(lease_s=300) == {"ok": True, "commands": 1, "receipts": 1}
        finally:
            producer.close_all()
        command_repository = CommandRepository(transfer.repository.db_path)
        reference = command_repository.get("export-a")["result"]["artifact"]
        assert reference["artifact_id"] != bundle.manifest["artifact_id"]
        assert (reference["sha256"], reference["size"]) == (bundle.manifest["sha256"], bundle.manifest["size"])
        with transfer.workflow.repository.transaction(write=False) as connection:
            bound = connection.execute("SELECT artifact_id,sha256,size FROM workflow_artifact_refs WHERE output_key='output:report'").fetchone()
            assert tuple(bound) == (reference["artifact_id"], reference["sha256"], reference["size"])
        definition = registered(source=reference)
        repository = transfer.workflow.repository
        repository.create_or_get(OWNER, WORKSPACE, "workflow-receiver", client_token="receiver",
            definition=definition, now=transfer.workflow.clock())
        claim = next(claim for claim in repository.claim_due(OWNER, worker_id="input-preparer", now=transfer.workflow.clock(),
            limit=10, lease_seconds=60) if claim.workflow_id == "workflow-receiver")
        resolved = resolve_contract(definition, AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("cli", 1),
            (InputBinding("source", OWNER, WORKSPACE, ArtifactReference(**reference)),)))
        inputs = export_node.app.extensions["fleet"]["services"]["artifact_inputs"]
        target = RegisteredNodeInputs(OWNER, WORKSPACE, "node-b", (("source", "source", ".tar"),))
        inputs.targets[(OWNER, WORKSPACE, "node-b")] = target
        assert inputs.prepare(claim, expected_revision=1, resolved=resolved, generation=1, target_key="node-b") is False
        assert repository.current_steps(OWNER, "workflow-receiver")[0].state == "waiting_node"
        with repository.transaction(write=False) as connection:
            grant_id = connection.execute("SELECT grant_id FROM platform_commands WHERE target_node='node-b' AND action='artifact.receive'").fetchone()[0]
        path = "/api/platform/v1/node/artifact-inputs/" + grant_id + "/content"
        assert export_node.client.get(path, headers=export_node.headers | {"X-Platform-Command-ID": "receive-" + grant_id}).status_code == 404
        root_b = export_node.tmp / "node-b-inputs"
        root_b.mkdir(mode=0o700)
        consumer = NodeRuntime(NodeRuntimeConfig(node_id="node-b", credential=credential_b, worker_id="receiver",
            hub_url=url, public_key=transfer.public_key, workspace_root=export_node.tmp / "receiver-workspace",
            journal_path=export_node.tmp / "receiver-commands.db", capabilities=frozenset({"artifact.receive"}),
            artifact_inputs=(("source", InputDirectory(root_b, frozenset({".tar"}))),)),
            clock=transfer.workflow.clock, transport=Transport(mode="direct", attempts=1))
        try:
            assert consumer.poll_once(lease_s=300) == {"ok": True, "commands": 1, "receipts": 1}
            assert not consumer.artifact_receiver.pending_receipts()
        finally:
            consumer.close_all()
        received = root_b / (grant_id + ".tar")
        assert hashlib.sha256(received.read_bytes()).hexdigest() == reference["sha256"]
        assert repository.current_steps(OWNER, "workflow-receiver")[0].state == "waiting_node"
        assert inputs.prepare(claim, expected_revision=repository.get(OWNER, "workflow-receiver").revision,
            resolved=resolved, generation=1, target_key="node-b") is False
        assert repository.current_steps(OWNER, "workflow-receiver")[0].state == "ready"
    class ReceivedInput:
        def read(self, owner: str, workspace: str, artifact_id: str) -> bytes:
            assert (owner, workspace, artifact_id) == (OWNER, WORKSPACE, reference["artifact_id"])
            return received.read_bytes()
    checkout = export_node.tmp / "receiver-validation"
    revision = replace(bundle.revision, source_bundle=ArtifactReference(**reference))
    assert set(extract_source_bundle(ReceivedInput(), OWNER, WORKSPACE, reference["artifact_id"], checkout,
        expected_revision=revision)) == {"src.txt", "test_payload.py"}
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "--junitxml=" + str(export_node.tmp / "validation.xml")],
        cwd=checkout, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout and not (checkout / ".env").exists()
