"""Default assembly, downstream contracts and complete bounded output reads."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

from hub.application.workflow_dispatch import WorkflowDispatchError
from hub.application.workflow_external_outputs import WorkflowExternalOutputs
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.external_agent import ExternalAgentStatus
from hub.domain.workflow_contract_types import ArtifactReference, CatalogRef, NamedReference
from hub.domain.workflow_identity import canonical_json
from hub.infrastructure.workflow_input_resolver import WorkflowArtifactInputResolver
from test_external_agent_outputs import original_set, output_setup, source_setup
from test_external_agent_persistence import registration
from test_external_agent_workflow import held
from test_workflow_repository import OWNER, WORKSPACE, registered

ORIGIN = {"Origin": "http://localhost"}


def output_app(fixture: dict[str, Any], *, enabled: bool = True) -> Any:
    setup = fixture["setup"]
    config = FleetConfig.from_root(fixture["artifacts"].root.parent, platform_enabled=True,
        platform_db=setup.repository.db_path, platform_artifact_root=fixture["artifacts"].root,
        platform_workflows_enabled=True, platform_workflow_dispatch_enabled=enabled,
        platform_external_agents_enabled=enabled, dev_operator=OWNER, serve_frontend=False)
    return create_app(config, repositories={"workflow_clock": setup.clock,
        "workflow_steps": (registration(setup),), "workflow_external_agents": ((setup.target, setup.adapter),)})


@pytest.mark.parametrize("kind", ["artifact", "structured", "code_revision"])
def test_default_coordinator_exports_existing_refs_and_public_delivery_remains_unaccepted(tmp_path: Path, kind: str) -> None:
    fixture = source_setup(tmp_path, admit=False) if kind == "code_revision" else output_setup(tmp_path, kind=kind, admit=False)
    setup = fixture["setup"]
    before = {thread.ident for thread in threading.enumerate()}
    app = output_app(fixture)
    assert before == {thread.ident for thread in threading.enumerate()}
    assert not setup.transport.sends and not fixture["exports"]
    setup.repository.release_claim(setup.claim, now=setup.clock(), next_poll_at=setup.clock())
    coordinator = app.extensions["fleet"]["services"]["workflow_coordinator"]
    for _ in range(2):
        result = asyncio.run(coordinator.tick(setup.clock()))
        assert not result.failures
        setup.clock.now += 1
    client = app.test_client()
    response = client.get("/api/platform/v1/workflows/workflow-a")
    assert response.status_code == 200
    detail = response.get_json()["workflow"]
    assert detail["execution_state"] == "succeeded" and detail["validation_state"] == "not_requested"
    assert detail["acceptance_state"] == "pending" and detail["delivery_state"] == "awaiting_acceptance"
    assert detail["steps"][0]["outputs"] == [{"output_key": "report", **{
        key: fixture["manifest"][key] for key in ("artifact_id", "sha256", "size")}}]
    assert detail["steps"][0]["evidence"] == []
    events = client.get("/api/platform/v1/workflows/workflow-a/events").get_json()["events"]
    assert any(item["payload"].get("evidence_source") == "agent_reported" for item in events)
    serialized = canonical_json({"detail": detail, "events": events})
    for forbidden in (b"external_outputs", b"export", b"request_snapshot", b"dispatch_token", b"session_id"):
        assert forbidden not in serialized
    artifact = fixture["manifest"]
    content_url = f'/api/platform/v1/artifacts/{artifact["artifact_id"]}/content?workspace_id={WORKSPACE}'
    downloaded = client.get(content_url)
    assert downloaded.status_code == 200 and downloaded.headers["X-Artifact-Sha256"] == artifact["sha256"]
    assert hashlib.sha256(downloaded.data).hexdigest() == artifact["sha256"] and len(downloaded.data) == artifact["size"]
    downloaded.close()
    assert client.get(content_url, headers={"Cf-Access-Authenticated-User-Email": "other@example.test"}).status_code == 404
    decision = {"decision": "accepted", "delivery_digest": detail["delivery_digest"],
        "expected_revision": detail["revision"], "client_token": "accept-output"}
    receipt = client.post("/api/platform/v1/workflows/workflow-a/acceptance", json=decision, headers=ORIGIN)
    assert receipt.status_code == 202
    assert client.post("/api/platform/v1/workflows/workflow-a/acceptance", json=decision, headers=ORIGIN).get_json() == receipt.get_json()
    assert client.get("/api/platform/v1/workflows/workflow-a").get_json()["workflow"]["delivery_state"] == "delivered"
    assert len(setup.transport.sends) == len(fixture["exports"]) == 1 and held(setup) == "released"


def test_export_schema_success_cannot_create_validator_evidence_or_allow_acceptance(tmp_path: Path) -> None:
    fixture = output_setup(tmp_path, kind="structured", contract_changes={"validator_refs": [{"id": "review", "revision": 1}]})
    setup = fixture["setup"]
    setup.apply(setup.dispatch(fixture["executor"]()))
    client = output_app(fixture).test_client()
    detail = client.get("/api/platform/v1/workflows/workflow-a").get_json()["workflow"]
    assert detail["execution_state"] == "succeeded" and detail["validation_state"] == "pending"
    assert detail["delivery_state"] == "awaiting_validation" and detail["steps"][0]["evidence"] == []
    decision = {"decision": "accepted", "delivery_digest": detail["delivery_digest"],
        "expected_revision": detail["revision"], "client_token": "premature-accept"}
    assert client.post("/api/platform/v1/workflows/workflow-a/acceptance", json=decision, headers=ORIGIN).status_code == 409
    assert setup.repository.get(OWNER, "workflow-a").acceptance_state == "pending"


@pytest.mark.parametrize("kind", ["artifact", "structured", "code_revision"])
def test_exported_outputs_resolve_into_the_original_downstream_contract(tmp_path: Path, kind: str) -> None:
    downstream = json.loads(registered().snapshot)["steps"][0]["contract"]
    downstream.update(input_refs=[{"ref_key": "previous", "kind": "prior_step_output", "step_key": "develop", "output_key": "report"}],
        output_specs=[])
    following = ({"step_key": "review", "kind": "coding_task", "requires": ["develop"], "contract": downstream},)
    fixture = source_setup(tmp_path, following_steps=following) if kind == "code_revision" else output_setup(
        tmp_path, kind=kind, following_steps=following)
    setup = fixture["setup"]
    setup.apply(setup.dispatch(fixture["executor"]()))
    snapshot = setup.repository.snapshots((setup.claim,), now=setup.clock())[0]
    resolved = asyncio.run(WorkflowArtifactInputResolver(setup.repository, fixture["artifacts"]).resolve(snapshot, "review", CatalogRef("reviewer", 1)))
    expected = fixture["source"] if kind == "code_revision" else ArtifactReference(*[fixture["manifest"][key] for key in ("artifact_id", "sha256", "size")])
    assert resolved.contract.input_refs == (NamedReference("previous", expected),)
    setup.repository.freeze_inputs(setup.claim, step_key="review", generation=1, resolved=resolved,
        expected_revision=snapshot.workflow.revision, now=setup.clock())
    assert setup.repository.current_steps(OWNER, "workflow-a")[1].resolved_snapshot == resolved.snapshot
    assert len(setup.transport.sends) == len(fixture["exports"]) == 1


@pytest.mark.parametrize("mode", ["paused", "drain", "gates_off"])
def test_existing_output_task_can_be_read_after_pause_drain_or_new_dispatch_gates_close(tmp_path: Path, mode: str) -> None:
    fixture = output_setup(tmp_path)
    setup = fixture["setup"]
    setup.adapter.inspect = lambda task: ExternalAgentStatus("running", "accepted", task.external_id)
    initial = setup.dispatch(fixture["executor"]())
    setup.apply(initial)
    with setup.repository.transaction() as connection:
        if mode == "paused":
            connection.execute("UPDATE workflow_runs SET pause_requested=1")
        elif mode == "drain":
            connection.execute("UPDATE workflow_schema_meta SET admission_mode='drain'")
    setup.adapter.inspect = lambda task: ExternalAgentStatus("succeeded", "confirmed", task.external_id)
    app = output_app(fixture, enabled=mode != "gates_off")
    setup.repository.release_claim(setup.claim, now=setup.clock(), next_poll_at=setup.clock())
    result = asyncio.run(app.extensions["fleet"]["services"]["workflow_coordinator"].tick(setup.clock()))
    assert not result.failures
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "succeeded"
    assert len(setup.transport.sends) == len(fixture["exports"]) == 1


@pytest.mark.parametrize("raw,valid", [
    (b'{"result":0}', True), (b"[]", False), (b"null", False), (b'{"result":"\xff"}', False),
    (b'{"result":"\\ud800"}', False), (b'{"result":Infinity}', False), (b'{"result":1e400}', False),
    (b'{"result":', False), (b"\xef\xbb\xbf" + b'{"result":0}', False),
    (b'{"result":"' + b"x" * (65536 - 13) + b'"}', True),
    (b'{"result":"' + b"x" * (65537 - 13) + b'"}', False),
    (b'{"result":' * 32 + b"0" + b"}" * 32, True),
    (b'{"result":' * 33 + b"0" + b"}" * 33, False),
], ids=['object', 'array', 'null', 'invalid-utf8', 'lone-surrogate', 'infinity',
        'numeric-overflow', 'incomplete', 'bom', 'exact-byte-limit', 'over-byte-limit',
        'exact-depth-limit', 'over-depth-limit'])
def test_structured_export_checks_utf8_depth_and_the_complete_byte_boundary(tmp_path: Path, raw: bytes, valid: bool) -> None:
    fixture = output_setup(tmp_path, kind="structured", schema={"type": "object"})
    path = tmp_path / "structured-boundary.json"
    path.write_bytes(raw)
    manifest = fixture["artifacts"].put_file(OWNER, WORKSPACE, path, content_type="application/json")
    fixture["manifest"].clear()
    fixture["manifest"].update(manifest)
    if valid:
        fixture["setup"].apply(fixture["setup"].dispatch(fixture["executor"]()))
        return
    with pytest.raises(WorkflowDispatchError):
        fixture["setup"].dispatch(fixture["executor"]())
    assert held(fixture["setup"]) == "held"


@pytest.mark.parametrize("schema,valid", [
    ({"$defs": {"result": {"type": "string"}}, "properties": {"result": {"$ref": "#/$defs/result"}}}, True),
    ({"properties": {"result": {"$ref": "#/missing"}}}, False),
    ({"type": "nonexistent"}, False),
    ({"$id": "https://unavailable.example/schema", "$defs": {"result": {"type": "string"}},
      "properties": {"result": {"$ref": "#/$defs/result"}}}, True),
])
def test_frozen_result_schemas_validate_locally_without_any_network_lookup(tmp_path: Path, monkeypatch: Any, schema: dict[str, Any], valid: bool) -> None:
    import socket
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("schema attempted network access")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    fixture = output_setup(tmp_path, kind="structured", schema=schema)
    if valid:
        fixture["setup"].apply(fixture["setup"].dispatch(fixture["executor"]()))
        return
    with pytest.raises(WorkflowDispatchError) as error:
        fixture["setup"].dispatch(fixture["executor"]())
    assert error.value.__cause__ is not None and held(fixture["setup"]) == "held"


class BoundedDescriptor:
    def __init__(self, handle: Any, requests: list[int]) -> None:
        self.handle, self.requests = handle, requests

    def __enter__(self) -> BoundedDescriptor:
        return self

    def __exit__(self, *args: Any) -> None:
        self.handle.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.handle, name)

    def read(self, size: int) -> bytes:
        assert 0 < size <= 65536
        self.requests.append(size)
        return self.handle.read(size)


def test_large_existing_hub_output_streams_bounded_blocks_with_small_heap(tmp_path: Path, monkeypatch: Any) -> None:
    size = 12 * 1024 * 1024
    fixture = output_setup(tmp_path, specs=[{"output_key": "report", "kind": "artifact", "required": True, "max_bytes": size}])
    store = fixture["artifacts"]
    block = b"x" * 65536
    digest = hashlib.sha256()
    # Seed an existing published artifact, independently of the transfer writer's
    # POSIX directory-fsync capability. This probe measures output verification
    # on every platform; transfer publication has its own durability tests.
    artifact_root = store.root / fixture["manifest"]["artifact_id"]
    with (artifact_root / "content").open("wb") as stream:
        for _ in range(size // len(block)):
            stream.write(block)
            digest.update(block)
    manifest_path = artifact_root / "manifest.json"
    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    stored.update(size=size, sha256=digest.hexdigest(), name="large.bin", content_type="application/octet-stream")
    manifest_path.write_text(json.dumps(stored), encoding="utf-8")
    manifest = store.get(OWNER, WORKSPACE, stored["artifact_id"])
    assert manifest["size"] == size and manifest["sha256"] == digest.hexdigest()
    fixture["manifest"].clear()
    fixture["manifest"].update(manifest)
    exported = original_set(fixture)
    requests: list[int] = []
    original_fdopen = os.fdopen
    monkeypatch.setattr(os, "fdopen", lambda *args, **kwargs: BoundedDescriptor(original_fdopen(*args, **kwargs), requests))
    tracemalloc.start()
    try:
        proof = WorkflowExternalOutputs(store).verify(fixture["setup"].resolved, exported.task, exported)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert proof.export == exported and len(requests) >= size // 65536 * 2
    assert peak < 2 * 1024 * 1024, peak


def test_configuration_changed_during_complete_output_read_cannot_commit_new_outputs(tmp_path: Path, monkeypatch: Any) -> None:
    fixture = output_setup(tmp_path, kind="structured")
    original_get = fixture["artifacts"].get
    def changed(*args: str) -> dict[str, Any] | None:
        fixture["setup"].adapter.account_ref = "changed-during-read"
        return original_get(*args)
    monkeypatch.setattr(fixture["artifacts"], "get", changed)
    with pytest.raises(WorkflowDispatchError):
        fixture["setup"].dispatch(fixture["executor"]())
    assert held(fixture["setup"]) == "held"
    with fixture["setup"].repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_artifact_refs").fetchone()[0] == 0
