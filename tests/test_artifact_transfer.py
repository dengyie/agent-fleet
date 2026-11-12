"""Command-bound transfers exercised against real workflow SQLite and files."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from hub.application.artifact_transfer_service import ArtifactTransferService
from hub.domain.artifact_transfer import ArtifactTransferError, TransferLimits, validate_part, validate_reservation
from fleet_core.control import generate_ed25519_keypair
from fleet_core.platform_command import PlatformCommand
from hub.domain.workflow_contract import parse_definition, resolve_contract
from hub.domain.workflow_contract_types import AuthorizedInputs, CatalogRef
from hub.infrastructure.artifact_transfer_repository import ArtifactTransferRepository
from hub.infrastructure.command_repository import CommandRepository
from hub.platform.artifacts import ArtifactStore
from test_workflow_dispatch import Setup, setup
from test_workflow_repository import OWNER, WORKSPACE, registered


@dataclass
class TransferFixture:
    workflow: Setup
    repository: ArtifactTransferRepository
    service: ArtifactTransferService
    private_key: bytes
    public_key: bytes
    command_id: str = "export-a"

    def reserve(self, data: bytes = b"payload", **changes: Any) -> Any:
        request = dict(command_id=self.command_id, owner_id=OWNER, node_id="node-a", worker_id="node-worker",
                       output_key="report", name="report.bin", size=len(data), sha256=hashlib.sha256(data).hexdigest())
        return self.service.reserve(**(request | changes))

    def part(self, transfer_id: str, data: bytes = b"payload", offset: int = 0, **changes: Any) -> Any:
        request = dict(owner_id=OWNER, node_id="node-a", worker_id="node-worker", offset=offset,
                       payload=data, declared_hash=hashlib.sha256(data).hexdigest(), content_length=len(data))
        return self.service.put_part(transfer_id, **(request | changes))

    def commit(self, transfer_id: str, data: bytes = b"payload", **changes: Any) -> Any:
        request = dict(owner_id=OWNER, node_id="node-a", worker_id="node-worker", command_id=self.command_id,
                       size=len(data), sha256=hashlib.sha256(data).hexdigest())
        return self.service.commit(transfer_id, **(request | changes))


@pytest.fixture
def transfer(tmp_path: Path, setup: Setup) -> TransferFixture:
    raw = json.loads(registered().snapshot)
    raw["steps"][0]["contract"]["output_specs"] = [
        {"output_key": key, "kind": "artifact", "required": True, "max_bytes": 256 * 1024 * 1024}
        for key in ("report", "second", "third")]
    raw["steps"][0]["contract"]["budget"].update(max_active_seconds=7200, max_wait_seconds=7200)
    definition = parse_definition(json.dumps(raw).encode())
    setup.resolved = resolve_contract(definition, AuthorizedInputs(OWNER, WORKSPACE, "develop", CatalogRef("cli", 1), ()))
    with setup.repository.transaction() as connection:
        connection.execute("UPDATE workflow_runs SET definition_snapshot=?,definition_digest=?", (definition.snapshot, hashlib.sha256(definition.snapshot).hexdigest()))
        connection.execute("UPDATE workflow_steps SET contract_snapshot=?,resolved_snapshot=?,resolved_digest=?,active_deadline=7300,wait_deadline=7300", (setup.resolved.contract.snapshot, setup.resolved.snapshot, setup.resolved.digest))
        connection.execute("UPDATE workflow_dispatches SET payload_hash=?", (setup.resolved.digest,))
    binding = setup.dispatch().binding
    private, public = generate_ed25519_keypair()
    commands = CommandRepository(setup.repository.db_path, clock=setup.clock)
    commands.init()
    command = PlatformCommand.create(command_id="export-a", target_node="node-a", action="artifact.export",
        resource_id=WORKSPACE, owner_id=OWNER, arguments={"output_key": "report"},
        expires_at=4000, run_id=binding.run_id).signed(private)
    commands.enqueue(command)
    with setup.repository.transaction() as connection:
        connection.execute("UPDATE platform_commands SET status='running',lease_owner='node-worker',lease_until=4000")
    repository = ArtifactTransferRepository(setup.repository.db_path, command_public_key=public, clock=setup.clock)
    repository.init()
    store = ArtifactStore(tmp_path / "artifacts", clock=setup.clock)
    service = ArtifactTransferService(repository, store, clock=setup.clock)
    return TransferFixture(setup, repository, service, private, public)


def test_legitimate_signed_command_can_reserve_and_commit_exactly_once(transfer: TransferFixture) -> None:
    manifest = transfer.reserve()
    assert manifest == transfer.reserve()
    assert transfer.part(manifest.transfer_id) == transfer.part(manifest.transfer_id)
    result = transfer.commit(manifest.transfer_id)
    assert result == transfer.commit(manifest.transfer_id)
    assert result["artifact_id"] == manifest.artifact_id
    assert transfer.service.artifacts.read(OWNER, WORKSPACE, manifest.artifact_id) == b"payload"
    with transfer.workflow.repository.transaction(write=False) as connection:
        refs = connection.execute("SELECT artifact_id FROM workflow_artifact_refs").fetchall()
        assert [row[0] for row in refs] == [manifest.artifact_id]


def test_default_limit_and_zero_byte_part_are_rejected() -> None:
    with pytest.raises(ArtifactTransferError, match="artifact_too_large"):
        validate_reservation(size=64 * 1024 * 1024 + 1, sha256="a" * 64, name="large.bin", limits=TransferLimits())
    with pytest.raises(ArtifactTransferError, match="invalid_part_size"):
        validate_part(offset=0, length=0, payload=b"", declared_hash=hashlib.sha256(b"").hexdigest(),
                      total_size=0, chunk_size=1024 * 1024, max_parts=256, existing_parts=0)


def test_full_part_receipt_list_allows_retransmission_of_last_part() -> None:
    data = b"x" * (1024 * 1024)
    assert validate_part(offset=255 * len(data), length=len(data), payload=data,
        declared_hash=hashlib.sha256(data).hexdigest(), total_size=256 * len(data),
        chunk_size=len(data), max_parts=256, existing_parts=256) == hashlib.sha256(data).hexdigest()


def test_tampered_signature_is_not_an_export_authorization(transfer: TransferFixture) -> None:
    with transfer.workflow.repository.transaction() as connection:
        connection.execute("UPDATE platform_commands SET signature='forged'")
    with pytest.raises(ArtifactTransferError, match="artifact_command_unavailable"):
        transfer.reserve()


def test_owner_node_worker_and_output_boundaries(transfer: TransferFixture) -> None:
    for changes in ({"owner_id": "other@example.test"}, {"node_id": "node-b"},
                    {"worker_id": "other"}, {"output_key": "other"}):
        with pytest.raises(ArtifactTransferError):
            transfer.reserve(**changes)


def test_zero_file_needs_no_parts(transfer: TransferFixture) -> None:
    manifest = transfer.reserve(b"")
    result = transfer.commit(manifest.transfer_id, b"")
    assert result["size"] == 0


def test_reservation_conflict_and_missing_chunk(transfer: TransferFixture) -> None:
    manifest = transfer.reserve()
    with pytest.raises(ArtifactTransferError, match="transfer_conflict"):
        transfer.reserve(b"different")
    with pytest.raises(ArtifactTransferError, match="missing_or_overlapping_part"):
        transfer.commit(manifest.transfer_id)
    transfer.part(manifest.transfer_id)
    assert transfer.commit(manifest.transfer_id)["artifact_id"] == manifest.artifact_id


@pytest.fixture
def transfer_http(transfer: TransferFixture, tmp_path: Path) -> tuple[Any, Any, dict[str, str]]:
    from hub.bootstrap import create_app
    from hub.config import FleetConfig

    app = create_app(FleetConfig.from_root(tmp_path, platform_enabled=True,
        platform_db=transfer.repository.db_path, platform_artifact_root=transfer.service.artifacts.root,
        platform_workflows_enabled=True, platform_workflow_dispatch_enabled=True,
        platform_artifact_transfer_enabled=True, platform_command_signing_raw=transfer.private_key,
        dev_operator=OWNER, serve_frontend=False), repositories={"workflow_clock": transfer.workflow.clock})
    platform = app.extensions["fleet"]["platform_repository"]
    platform.upsert_node(OWNER, {"node_id": "node-a", "label": "Transfer node"})
    credential = platform.provision_node_credential(OWNER, "node-a", secret="n" * 40)["credential"]
    headers = {"X-Platform-Node-Credential": credential, "X-Platform-Command-ID": "export-a", "X-Platform-Worker-ID": "node-worker"}
    return app, app.test_client(), headers


@pytest.mark.parametrize("suffix", ["", "/transfer-a/commit"])
def test_transfer_deep_json_uses_bounded_request_decoder(transfer_http: Any, suffix: str) -> None:
    _, client, headers = transfer_http
    response = client.post("/api/platform/v1/node/artifact-transfers" + suffix,
                           data="[" * 10000 + "]" * 10000,
                           content_type="application/json", headers=headers)
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_json"


def test_http_node_auth_chunk_limit_scope_and_ready_download(transfer: TransferFixture, transfer_http: Any) -> None:
    app, client, headers = transfer_http
    data = b"x" * (1024 * 1024)
    body = dict(command_id="export-a", output_key="report", name="report.bin", size=len(data), sha256=hashlib.sha256(data).hexdigest())
    path = "/api/platform/v1/node/artifact-transfers"
    assert client.post(path, json=body).status_code in {401, 403}
    first = client.post(path, json=body, headers=headers)
    assert first.status_code == 201, first.get_json()
    manifest = first.get_json()["transfer"]
    assert manifest["expires_at"].endswith("+00:00")
    target = path + "/" + manifest["transfer_id"]
    part_headers = headers | {"X-Content-SHA256": body["sha256"]}
    assert client.put(target + "/parts/0", data=data, headers=part_headers).status_code == 200
    assert app.config["MAX_CONTENT_LENGTH"] == 256 * 1024
    assert client.put(target + "/parts/0", data=data + b"x", headers=part_headers).status_code == 413
    assert client.get(target, headers=headers | {"X-Platform-Command-ID": "wrong"}).status_code == 404
    assert client.get(target, headers=headers | {"X-Platform-Worker-ID": "wrong"}).status_code == 404
    assert str(transfer.service.staging_root) not in client.get(target, headers=headers).get_data(as_text=True)
    committed = client.post(target + "/commit", json={key: body[key] for key in ("command_id", "size", "sha256")}, headers=headers)
    assert committed.status_code == 200, committed.get_json()
    download = client.get(f"/api/platform/v1/artifacts/{manifest['artifact_id']}/content?workspace_id={WORKSPACE}", buffered=False)
    chunks = list(download.response)
    assert max(map(len, chunks)) <= 1024 * 1024
    assert b"".join(chunks) == data
    assert download.headers["Content-Disposition"].startswith("attachment;")
    download.close()


def test_download_and_preview_avoid_full_file_read(transfer: TransferFixture, transfer_http: Any, monkeypatch: Any) -> None:
    app, client, _headers = transfer_http
    manifest = transfer.reserve(content_type="text/plain")
    transfer.part(manifest.transfer_id)
    transfer.commit(manifest.transfer_id)
    store = app.extensions["fleet"]["services"]["platform_artifacts"]
    monkeypatch.setattr(store, "read", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("full-file allocation")))
    path = f"/api/platform/v1/artifacts/{manifest.artifact_id}"
    response = client.get(path + f"/content?workspace_id={WORKSPACE}", buffered=False)
    assert response.status_code == 200
    assert b"".join(response.response) == b"payload"
    response.close()
    response = client.get(path + f"/preview?workspace_id={WORKSPACE}")
    assert response.status_code == 200
    assert response.get_json()["preview"]["text"] == "payload"


def add_export(fixture: TransferFixture, command_id: str, output_key: str) -> None:
    with fixture.workflow.repository.transaction(write=False) as connection:
        run_id = connection.execute("SELECT run_id FROM platform_commands WHERE command_id=?", (fixture.command_id,)).fetchone()[0]
    command = PlatformCommand.create(command_id=command_id, target_node="node-a", action="artifact.export",
        resource_id=WORKSPACE, owner_id=OWNER, arguments={"output_key": output_key}, expires_at=4000,
        run_id=run_id).signed(fixture.private_key)
    CommandRepository(fixture.repository.db_path, clock=fixture.workflow.clock).enqueue(command)
    with fixture.workflow.repository.transaction() as connection:
        connection.execute("UPDATE platform_commands SET status='running',lease_owner='node-worker',lease_until=4000 WHERE command_id=?", (command_id,))


def test_large_file_is_streamed_and_legacy_limit_is_unchanged(transfer: TransferFixture, tmp_path: Path) -> None:
    from hub.platform.artifacts import ArtifactError

    data = b"q" * (11 * 1024 * 1024 + 5)
    manifest = transfer.reserve(data)
    for offset in range(0, len(data), manifest.chunk_size):
        transfer.part(manifest.transfer_id, data[offset:offset + manifest.chunk_size], offset)
    result = transfer.commit(manifest.transfer_id, data)
    assert result["size"] == len(data)
    with transfer.service.artifacts.open_verified(OWNER, WORKSPACE, manifest.artifact_id) as handle:
        assert hashlib.sha256(handle.read()).hexdigest() == hashlib.sha256(data).hexdigest()
    source = tmp_path / "legacy-too-large.bin"
    source.write_bytes(data)
    with pytest.raises(ArtifactError, match="artifact_too_large"):
        transfer.service.artifacts.put_file(OWNER, WORKSPACE, source)
    with pytest.raises(ArtifactError, match="artifact_too_large"):
        transfer.service.artifacts.put_bytes(OWNER, WORKSPACE, b"\x89PNG\r\n\x1a\n" + data)


@pytest.mark.parametrize("changes,code", [
    ({"offset": 1}, "invalid_offset"), ({"content_length": 8}, "invalid_content_length"),
    ({"declared_hash": "a" * 64}, "part_hash_mismatch"), ({"offset": 1024 * 1024}, "part_out_of_bounds"),
])
def test_bad_part_is_rejected_before_disk_write(transfer: TransferFixture, changes: dict[str, Any], code: str) -> None:
    manifest = transfer.reserve()
    with pytest.raises(ArtifactTransferError, match=code):
        transfer.part(manifest.transfer_id, **changes)
    assert not list((transfer.service.staging_root / manifest.transfer_id).iterdir())


def test_conflicting_part_does_not_overwrite_original_bytes(transfer: TransferFixture) -> None:
    manifest = transfer.reserve()
    transfer.part(manifest.transfer_id)
    with pytest.raises(ArtifactTransferError, match="part_conflict"):
        transfer.part(manifest.transfer_id, b"changed")
    assert transfer.commit(manifest.transfer_id)["sha256"] == manifest.sha256


def test_workflow_and_workspace_quotas_include_committed_files_and_failed_scratch(transfer: TransferFixture) -> None:
    from dataclasses import replace

    transfer.service.limits = replace(transfer.service.limits, workspace_quota=20, workflow_budget=14)
    first = transfer.reserve()
    transfer.part(first.transfer_id)
    transfer.commit(first.transfer_id)
    add_export(transfer, "export-b", "second")
    with pytest.raises(ArtifactTransferError, match="workspace_quota_exceeded"):
        transfer.reserve(command_id="export-b", output_key="second")
    transfer.service.limits = replace(transfer.service.limits, workspace_quota=100)
    second = transfer.reserve(command_id="export-b", output_key="second")
    assert second.transfer_id != first.transfer_id
    add_export(transfer, "export-c", "third")
    with pytest.raises(ArtifactTransferError, match="workflow_artifact_budget_exceeded"):
        transfer.reserve(command_id="export-c", output_key="third")


def test_legacy_ready_files_are_charged_to_workspace(transfer: TransferFixture, tmp_path: Path) -> None:
    from dataclasses import replace

    source = tmp_path / "prior.txt"
    source.write_bytes(b"prior")
    transfer.service.artifacts.put_file(OWNER, WORKSPACE, source)
    transfer.service.limits = replace(transfer.service.limits, workspace_quota=11)
    with pytest.raises(ArtifactTransferError, match="workspace_quota_exceeded"):
        transfer.reserve()


def test_mismatched_generation_or_lease_cannot_publish(transfer: TransferFixture) -> None:
    manifest = transfer.reserve()
    transfer.part(manifest.transfer_id)
    with transfer.workflow.repository.transaction() as connection:
        connection.execute("UPDATE workflow_steps SET is_current=0")
    with pytest.raises(ArtifactTransferError, match="artifact_command_unavailable"):
        transfer.commit(manifest.transfer_id)
    assert transfer.service.artifacts.get(OWNER, WORKSPACE, manifest.artifact_id) is None


def test_no_public_key_means_fail_closed(transfer: TransferFixture) -> None:
    transfer.repository.command_public_key = None
    with pytest.raises(ArtifactTransferError, match="artifact_command_unavailable"):
        transfer.reserve()


def test_urlsafe_identity_can_start_with_dash_or_underscore(transfer: TransferFixture) -> None:
    identities = iter(("_reservation", "-artifact"))
    transfer.service.identity = lambda: next(identities)
    manifest = transfer.reserve()
    assert manifest.transfer_id == "_reservation"
    transfer.part(manifest.transfer_id)
    assert transfer.commit(manifest.transfer_id)["artifact_id"] == "transfer--artifact"


def _reserve_in_process(db_path: str, artifact_root: str, public_key: bytes, command_id: str,
                        output_key: str, limits: TransferLimits, barrier: Any, queue: Any) -> None:
    import time

    repository = ArtifactTransferRepository(Path(db_path), command_public_key=public_key, clock=lambda: 104)
    service = ArtifactTransferService(repository, ArtifactStore(Path(artifact_root)), limits=limits, clock=lambda: 104)
    barrier.wait(timeout=15)
    end = time.monotonic() + 5
    while True:
        try:
            result = service.reserve(command_id=command_id, owner_id=OWNER, node_id="node-a", worker_id="node-worker",
                output_key=output_key, name="report.bin", size=7, sha256=hashlib.sha256(b"payload").hexdigest())
            queue.put(("reserved", result.transfer_id))
            return
        except ArtifactTransferError as exc:
            if exc.code == "transfer_busy" and time.monotonic() < end:
                time.sleep(0.01)
                continue
            queue.put((exc.code, None))
            return


@pytest.mark.parametrize("limit_name,limit_value,error", [
    ("per_node_concurrency", 1, "transfer_concurrency_limit"),
    ("global_concurrency", 1, "transfer_concurrency_limit"),
    ("workspace_quota", 7, "workspace_quota_exceeded"),
    ("workflow_budget", 7, "workflow_artifact_budget_exceeded"),
])
def test_two_os_processes_cannot_overbook_last_capacity(transfer: TransferFixture, limit_name: str, limit_value: int, error: str) -> None:
    import multiprocessing
    from dataclasses import replace

    add_export(transfer, "export-b", "second")
    limits = replace(transfer.service.limits, **{limit_name: limit_value})
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_reserve_in_process, args=(str(transfer.repository.db_path),
        str(transfer.service.artifacts.root), transfer.public_key, command, output, limits, barrier, queue))
        for command, output in (("export-a", "report"), ("export-b", "second"))]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    results = [queue.get(timeout=5)[0] for _ in processes]
    queue.close()
    queue.join_thread()
    assert sorted(results) == sorted(["reserved", error])
    assert len(transfer.repository.manifests()) == 1


def test_active_chunk_renewal_has_a_two_hour_absolute_deadline(transfer: TransferFixture) -> None:
    with transfer.workflow.repository.transaction(write=False) as connection:
        run_id = connection.execute("SELECT run_id FROM platform_commands WHERE command_id='export-a'").fetchone()[0]
    command = PlatformCommand.create(command_id="export-a", target_node="node-a", action="artifact.export", resource_id=WORKSPACE,
        owner_id=OWNER, arguments={"output_key": "report"}, expires_at=20000, run_id=run_id).signed(transfer.private_key)
    with transfer.workflow.repository.transaction() as connection:
        connection.execute("UPDATE platform_commands SET expires_at=20000,lease_until=20000,signature=? WHERE command_id='export-a'", (command.signature,))
    manifest = transfer.reserve()
    transfer.part(manifest.transfer_id)
    for now in (1800, 3500, 5200, 6900):
        transfer.workflow.clock.now = now
        transfer.part(manifest.transfer_id)
    row = transfer.repository.reservation(manifest.transfer_id)
    assert row["expires_at"] == row["absolute_expires_at"] == 7304
    transfer.workflow.clock.now = 7305
    with pytest.raises(ArtifactTransferError, match="reservation_expired"):
        transfer.part(manifest.transfer_id)
    assert transfer.repository.reservation(manifest.transfer_id)["staging_bytes"] == 7


@pytest.mark.parametrize("changes", [{"default_size": 256 * 1024 * 1024 + 1}, {"max_size": 256 * 1024 * 1024 + 1}, {"absolute_ttl": 7201}])
def test_protocol_hard_limits_cannot_be_configured_away(changes: dict[str, int]) -> None:
    with pytest.raises(ArtifactTransferError, match="invalid_transfer_limits"):
        TransferLimits(**changes)
    assert TransferLimits(default_size=256 * 1024 * 1024).max_size == 256 * 1024 * 1024


def test_transfer_gate_is_default_off_and_requires_signing(tmp_path: Path) -> None:
    from dataclasses import replace
    from hub.bootstrap import create_app
    from hub.config import FleetConfig

    config = FleetConfig.from_root(tmp_path, platform_enabled=True, dev_operator=OWNER, serve_frontend=False)
    app = create_app(config)
    assert app.test_client().post("/api/platform/v1/node/artifact-transfers", json={}).status_code == 404
    import sqlite3
    with sqlite3.connect(config.platform_db) as connection:
        assert connection.execute("SELECT 1 FROM sqlite_master WHERE name='artifact_transfers'").fetchone() is None
    assert not FleetConfig.from_root(tmp_path, platform_enabled=True,
        platform_artifact_transfer_enabled=True).platform_artifact_transfer_enabled
    with pytest.raises(ValueError, match="artifact transfer gate dependencies"):
        create_app(replace(config, platform_artifact_transfer_enabled=True))
    with pytest.raises(ValueError, match="artifact transfer requires command signing"):
        create_app(FleetConfig.from_root(tmp_path / "unsigned", platform_enabled=True, platform_workflows_enabled=True,
            platform_workflow_dispatch_enabled=True, platform_artifact_transfer_enabled=True, dev_operator=OWNER, serve_frontend=False))


def test_stale_command_is_rejected_before_consuming_request_body(transfer: TransferFixture, transfer_http: Any) -> None:
    import io

    _app, client, headers = transfer_http
    manifest = transfer.reserve()
    with transfer.workflow.repository.transaction() as connection:
        connection.execute("UPDATE platform_commands SET lease_until=103")
    class UnreadableBody(io.BytesIO):
        def read(self, *args: Any, **kwargs: Any) -> bytes:
            raise AssertionError("unauthorized body read")
        def readinto(self, buffer: Any) -> int:
            raise AssertionError("unauthorized body read")
    response = client.open(f"/api/platform/v1/node/artifact-transfers/{manifest.transfer_id}/parts/0", method="PUT",
        headers=headers | {"X-Content-SHA256": manifest.sha256},
        environ_overrides={"wsgi.input": UnreadableBody(b"payload"), "CONTENT_LENGTH": "7"})
    assert response.status_code == 409
    assert response.get_json()["error"] == "artifact_command_unavailable"


def test_slow_body_cannot_outlive_reservation(transfer: TransferFixture, transfer_http: Any) -> None:
    import io

    _app, client, headers = transfer_http
    manifest = transfer.reserve()
    class SlowBody(io.BytesIO):
        def readinto(self, buffer: Any) -> int:
            transfer.workflow.clock.now = manifest.expires_at + 1
            return super().readinto(buffer)
    response = client.open(f"/api/platform/v1/node/artifact-transfers/{manifest.transfer_id}/parts/0", method="PUT",
        headers=headers | {"X-Content-SHA256": manifest.sha256},
        environ_overrides={"wsgi.input": SlowBody(b"payload"), "CONTENT_LENGTH": "7"})
    assert response.status_code == 409
    assert response.get_json()["error"] == "reservation_expired"
    assert not list((transfer.service.staging_root / manifest.transfer_id).iterdir())


def test_disconnected_body_has_stable_error_and_writes_nothing(transfer: TransferFixture, transfer_http: Any) -> None:
    import io

    _app, client, headers = transfer_http
    manifest = transfer.reserve()
    response = client.open(f"/api/platform/v1/node/artifact-transfers/{manifest.transfer_id}/parts/0", method="PUT",
        headers=headers | {"X-Content-SHA256": manifest.sha256},
        environ_overrides={"wsgi.input": io.BytesIO(b"pay"), "CONTENT_LENGTH": "7"})
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_content_length"
    assert not list((transfer.service.staging_root / manifest.transfer_id).iterdir())


def test_maximum_256_parts_are_out_of_order_resumable_and_memory_bounded(transfer: TransferFixture) -> None:
    import tracemalloc
    from dataclasses import replace

    block = b"m" * (1024 * 1024)
    total = len(block) * 256
    digest = hashlib.sha256()
    for _ in range(256):
        digest.update(block)
    checksum = digest.hexdigest()
    transfer.service.limits = replace(transfer.service.limits, default_size=total)
    manifest = transfer.reserve(size=total, sha256=checksum)
    tracemalloc.start()
    try:
        for index in reversed(range(256)):
            transfer.part(manifest.transfer_id, block, index * len(block))
        last = transfer.part(manifest.transfer_id, block, 255 * len(block))
        assert last["offset"] == 255 * len(block)
        result = transfer.commit(manifest.transfer_id, size=total, sha256=checksum)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result["size"] == total and result["sha256"] == checksum
    assert peak < 16 * 1024 * 1024
    assert not (transfer.service.staging_root / manifest.transfer_id / ".publication").exists()


def test_stale_expiry_update_cannot_rewrite_ready(transfer: TransferFixture) -> None:
    manifest = transfer.reserve()
    transfer.part(manifest.transfer_id)
    transfer.commit(manifest.transfer_id)
    transfer.repository.state(manifest.transfer_id, "failed", code="reservation_expired", now=4001)
    assert transfer.repository.reservation(manifest.transfer_id)["state"] == "ready"
    assert transfer.service.artifacts.get(OWNER, WORKSPACE, manifest.artifact_id) is not None


@pytest.mark.parametrize("action", ["pause", "drain"])
@pytest.mark.parametrize("phase", ["reservation", "part", "commit"])
def test_admission_stop_preserves_current_export(transfer: TransferFixture, transfer_http: Any, action: str, phase: str) -> None:
    from hub.domain.workflow_control import WorkflowControlRequest
    from hub.infrastructure.workflow_control_repository import WorkflowControlRepository

    _app, client, headers = transfer_http
    manifest = transfer.reserve() if phase != "reservation" else None
    if phase == "commit":
        transfer.part(manifest.transfer_id)
    if action == "pause":
        current = transfer.workflow.repository.get(OWNER, "workflow-a")
        WorkflowControlRepository(transfer.workflow.repository).control(
            WorkflowControlRequest(OWNER, "workflow-a", "pause", current.revision, "pause-current-export"),
            now=transfer.workflow.clock())
    else:
        with transfer.workflow.repository.transaction() as connection:
            connection.execute("UPDATE workflow_schema_meta SET admission_mode='drain'")
    base = "/api/platform/v1/node/artifact-transfers"
    if manifest is None:
        response = client.post(base, headers=headers, json={"command_id": "export-a", "output_key": "report",
            "name": "report.bin", "size": 7, "sha256": hashlib.sha256(b"payload").hexdigest()})
        assert response.status_code == 201, response.get_json()
        transfer_id = response.get_json()["transfer"]["transfer_id"]
    else:
        transfer_id = manifest.transfer_id
    if phase != "commit":
        response = client.put(base + f"/{transfer_id}/parts/0", data=b"payload",
            headers=headers | {"X-Content-SHA256": hashlib.sha256(b"payload").hexdigest()})
        assert response.status_code == 200, response.get_json()
    response = client.post(base + f"/{transfer_id}/commit", headers=headers,
        json={"command_id": "export-a", "size": 7, "sha256": hashlib.sha256(b"payload").hexdigest()})
    assert response.status_code == 200, response.get_json()
    result = response.get_json()["artifact"]
    assert transfer.service.artifacts.read(OWNER, WORKSPACE, result["artifact_id"]) == b"payload"
    assert transfer.workflow.repository.current_steps(OWNER, "workflow-a")[0].state in {"running", "waiting_external"}


@pytest.mark.parametrize("construction", ["factory", "direct"])
def test_dispatch_gate_off_keeps_bound_transfer_http_available(transfer: TransferFixture, tmp_path: Path, construction: str) -> None:
    from dataclasses import replace
    from hub.bootstrap import create_app
    from hub.config import FleetConfig

    manifest = transfer.reserve()
    config = FleetConfig.from_root(tmp_path, platform_enabled=True,
        platform_db=transfer.repository.db_path, platform_artifact_root=transfer.service.artifacts.root,
        platform_workflows_enabled=True, platform_workflow_dispatch_enabled=False,
        platform_artifact_transfer_enabled=True, platform_command_signing_raw=transfer.private_key,
        dev_operator=OWNER, serve_frontend=False)
    if construction == "direct":
        config = replace(config, platform_artifact_transfer_enabled=True)
    assert config.platform_artifact_transfer_enabled
    app = create_app(config, repositories={"workflow_clock": transfer.workflow.clock})
    assert not app.extensions["fleet"]["services"]["workflow_coordinator"].dispatch_enabled
    platform = app.extensions["fleet"]["platform_repository"]
    platform.upsert_node(OWNER, {"node_id": "node-a", "label": "Transfer node"})
    credential = platform.provision_node_credential(OWNER, "node-a", secret="n" * 40)["credential"]
    headers = {"X-Platform-Node-Credential": credential, "X-Platform-Command-ID": "export-a", "X-Platform-Worker-ID": "node-worker"}
    client = app.test_client()
    base = f"/api/platform/v1/node/artifact-transfers/{manifest.transfer_id}"
    response = client.put(base + "/parts/0", data=b"payload", headers=headers | {"X-Content-SHA256": manifest.sha256})
    assert response.status_code == 200, response.get_json()
    response = client.post(base + "/commit", headers=headers,
        json={"command_id": "export-a", "size": 7, "sha256": manifest.sha256})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["artifact"]["artifact_id"] == manifest.artifact_id


@pytest.mark.parametrize("revocation", ["step_deadline", "run_cancel"])
@pytest.mark.parametrize("phase", ["reservation", "part", "commit"])
def test_revoked_run_or_expired_step_cannot_publish(transfer: TransferFixture, revocation: str, phase: str) -> None:
    manifest = transfer.reserve() if phase != "reservation" else None
    if phase == "commit":
        transfer.part(manifest.transfer_id)
    statement, parameters = {
        "step_deadline": ("UPDATE workflow_steps SET active_deadline=?", (transfer.workflow.clock(),)),
        "run_cancel": ("UPDATE runs SET cancel_requested=1", ()),
    }[revocation]
    with transfer.workflow.repository.transaction() as connection:
        connection.execute(statement, parameters)
    with pytest.raises(ArtifactTransferError, match="artifact_command_unavailable"):
        if phase == "reservation":
            transfer.reserve()
        elif phase == "part":
            transfer.part(manifest.transfer_id)
        else:
            transfer.commit(manifest.transfer_id)
    if manifest is not None:
        assert transfer.service.artifacts.get(OWNER, WORKSPACE, manifest.artifact_id) is None


def test_step_deadline_is_enforced_while_receiving_body(transfer: TransferFixture, transfer_http: Any) -> None:
    import io

    _app, client, headers = transfer_http
    manifest = transfer.reserve()
    deadline = transfer.workflow.clock() + 1
    with transfer.workflow.repository.transaction() as connection:
        connection.execute("UPDATE workflow_steps SET active_deadline=?", (deadline,))

    class SlowBody(io.BytesIO):
        def readinto(self, buffer: Any) -> int:
            transfer.workflow.clock.now = deadline + 1
            return super().readinto(buffer)

    response = client.open(f"/api/platform/v1/node/artifact-transfers/{manifest.transfer_id}/parts/0", method="PUT",
        headers=headers | {"X-Content-SHA256": manifest.sha256},
        environ_overrides={"wsgi.input": SlowBody(b"payload"), "CONTENT_LENGTH": "7"})
    assert response.status_code == 409, response.get_json()
    assert response.get_json()["error"] == "artifact_command_unavailable"
    assert not list((transfer.service.staging_root / manifest.transfer_id).iterdir())
