"""Published Hub outputs remain bound to the original external execution."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import io
import tarfile
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hub.application.workflow_dispatch import WorkflowDispatchError
from hub.application.workflow_external_execution import ExternalStepExecutor
from hub.application.workflow_external_outputs import WorkflowExternalOutputs, WorkflowExternalOutputsError
from hub.domain.external_agent import ExternalAgentCapabilities, ExternalAgentStatus, ExternalTaskRef
from hub.domain.external_outputs import ExternalArtifactOutput, ExternalOutputSet, VerifiedExternalOutputs, parse_external_outputs
from hub.domain.workflow_contract import reference_values
from hub.domain.workflow_contract_types import ArtifactReference, NamedReference
from hub.domain.workflow_identity import canonical_json, dispatch_token
from hub.domain.workflow_errors import WorkflowValidationError
from hub.infrastructure.workflow_repository import WorkflowRepositoryError
from hub.platform.artifacts import ArtifactStore
from hub.platform.workflow_source import SourceExporter
from test_external_agent_workflow import make_external, held
from test_workflow_repository import OWNER, WORKSPACE


def output_setup(tmp_path: Path, *, kind: str = "artifact", optional: bool = False, schema: dict[str, Any] | None = None,
                 specs: list[dict[str, Any]] | None = None, admit: bool = True,
                 contract_changes: dict[str, Any] | None = None,
                 following_steps: tuple[dict[str, Any], ...] = ()) -> Any:
    from hub.application.workflow_external_outputs import WorkflowExternalOutputs
    caps = ExternalAgentCapabilities(True, True, True, False, False, True)
    constraint = {"max_bytes": 100} if kind == "artifact" else {"result_schema": schema or {"type": "object", "required": ["result"]}}
    setup = make_external(tmp_path, admit=admit, capabilities=caps, following_steps=following_steps,
        contract_changes={**(contract_changes or {}), "output_specs": specs if specs is not None else [
        {"output_key": "report", "kind": kind, "required": not optional, **constraint},
    ]})
    artifacts = ArtifactStore(tmp_path / "artifacts", clock=setup.clock)
    source = tmp_path / "report.json"
    source.write_bytes(b'{"result":"complete"}')
    manifest = artifacts.put_file(OWNER, WORKSPACE, source, name="report.json", content_type="application/json")
    result: dict[str, Any] = {"setup": setup, "artifacts": artifacts, "manifest": manifest, "exports": []}

    def export(task: Any, specs: Any) -> Any:
        from hub.domain.external_outputs import ExternalArtifactOutput, ExternalOutputSet
        from hub.domain.workflow_contract_types import ArtifactReference
        result["exports"].append((task, specs))
        return ExternalOutputSet("workflow-a", "develop", 1, task, (
            ExternalArtifactOutput("report", ArtifactReference(manifest["artifact_id"], manifest["sha256"], manifest["size"])),
        ))

    setup.adapter.export_outputs = export
    setup.adapter.inspect = lambda task: ExternalAgentStatus("succeeded", "agent_reported", task.external_id, session_id=task.session_id)
    result["executor"] = lambda: ExternalStepExecutor(
        setup.store, setup.target, setup.adapter, clock=setup.clock, enabled=True, outputs=WorkflowExternalOutputs(artifacts))
    return result


def test_external_published_output_is_atomic_private_and_restart_stable(tmp_path: Path) -> None:
    fixture = output_setup(tmp_path)
    setup = fixture["setup"]
    observed = setup.dispatch(fixture["executor"]())
    assert observed.state == "succeeded" and observed.evidence_source == "agent_reported"
    assert held(setup) == "held"
    setup.apply(observed)
    before = setup.repository.get(OWNER, "workflow-a")
    with setup.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == 8
        row = connection.execute("SELECT output_key,artifact_id,sha256,size FROM workflow_artifact_refs").fetchone()
        assert tuple(row) == ("output:report", fixture["manifest"]["artifact_id"], fixture["manifest"]["sha256"], fixture["manifest"]["size"])
        assert connection.execute("SELECT count(*) FROM workflow_evidence").fetchone()[0] == 0
    assert b"external_outputs" not in b"".join(event.payload for event in setup.repository.events(OWNER, "workflow-a").events)
    assert held(setup) == "released" and len(fixture["exports"]) == 1
    recovered = asyncio.run(fixture["executor"]().inspect(observed.binding))
    assert recovered == observed and len(fixture["exports"]) == 1
    setup.apply(recovered)
    assert setup.repository.get(OWNER, "workflow-a") == before
    assert len(setup.transport.sends) == 1


@pytest.mark.parametrize("change", ["owner", "workspace", "hash", "size", "bytes", "missing", "hidden"])
def test_external_output_rejects_scope_or_complete_bytes_before_commit(tmp_path: Path, change: str) -> None:
    fixture = output_setup(tmp_path)
    setup, manifest = fixture["setup"], fixture["manifest"]
    path = fixture["artifacts"].root / manifest["artifact_id"] / "manifest.json"
    raw = json.loads(path.read_text())
    if change in {"owner", "workspace"}:
        raw[change + "_id"] = "foreign"
    elif change == "hash":
        raw["sha256"] = "f" * 64
    elif change == "size":
        raw["size"] += 1
    elif change == "hidden":
        raw["ready"] = False
    elif change in {"bytes", "missing"}:
        content = path.with_name("content")
        content.write_bytes(b"tampered" if change == "bytes" else b"")
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(WorkflowDispatchError) as error:
        setup.dispatch(fixture["executor"]())
    assert error.value.__cause__ is not None
    with setup.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_artifact_refs").fetchone()[0] == 0
    assert held(setup) == "held" and setup.repository.get(OWNER, "workflow-a").execution_state == "unknown"


@pytest.mark.parametrize("change", ["workflow", "step", "generation", "task", "missing", "duplicate", "extra", "required"])
def test_external_output_set_rejects_inexact_original_contract(tmp_path: Path, change: str) -> None:
    fixture = output_setup(tmp_path)
    setup = fixture["setup"]
    original = setup.adapter.export_outputs

    def export(task: Any, specs: Any) -> Any:
        result = original(task, specs)
        if change == "workflow":
            return replace(result, workflow_id="foreign")
        if change == "step":
            return replace(result, step_key="foreign")
        if change == "generation":
            return replace(result, generation=2)
        if change == "task":
            return replace(result, task=replace(task, external_id="foreign"))
        if change == "missing":
            return {"text": "success"}
        if change == "duplicate":
            return replace(result, outputs=result.outputs * 2)
        if change == "extra":
            return replace(result, outputs=(replace(result.outputs[0], output_key="extra"),))
        return replace(result, outputs=())

    setup.adapter.export_outputs = export
    with pytest.raises(WorkflowDispatchError):
        setup.dispatch(fixture["executor"]())
    assert held(setup) == "held" and len(setup.transport.sends) == 1


@pytest.mark.parametrize("change", ["cancel", "frozen", "workspace", "generation", "deadline"])
def test_external_output_commit_rechecks_current_authority(tmp_path: Path, change: str) -> None:
    fixture = output_setup(tmp_path)
    setup = fixture["setup"]
    observed = setup.dispatch(fixture["executor"]())
    with setup.repository.transaction() as connection:
        if change == "cancel":
            connection.execute("UPDATE workflow_runs SET cancel_requested=1")
        elif change == "frozen":
            connection.execute("UPDATE workflow_schema_meta SET admission_mode='frozen'")
        elif change == "workspace":
            connection.execute("UPDATE workspaces SET enabled=0")
        elif change == "generation":
            connection.execute("UPDATE workflow_steps SET is_current=0")
        else:
            connection.execute("UPDATE workflow_steps SET active_deadline=100")
    with pytest.raises(WorkflowRepositoryError):
        setup.apply(observed)
    with setup.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_artifact_refs").fetchone()[0] == 0
    assert held(setup) == "held"


def test_external_output_rollback_preserves_original_task_and_all_reservations(tmp_path: Path) -> None:
    fixture = output_setup(tmp_path)
    setup = fixture["setup"]
    observed = setup.dispatch(fixture["executor"]())
    with setup.repository.transaction() as connection:
        connection.execute("CREATE TRIGGER fail_output BEFORE INSERT ON workflow_artifact_refs BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(WorkflowRepositoryError) as error:
        setup.apply(observed)
    assert isinstance(error.value.__cause__, sqlite3.Error)
    assert held(setup) == "held"
    with setup.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT subject_digest FROM workflow_steps").fetchone()[0] is None
        assert connection.execute("SELECT observation_snapshot FROM workflow_dispatches").fetchone()[0] is None
    assert len(setup.transport.sends) == 1


@pytest.mark.parametrize("data,valid", [(b'{"result":42}', True), (b'{"other":42}', False), (b'{"result":1,"result":2}', False), (b'{"result":NaN}', False)])
def test_external_structured_output_checks_actual_complete_schema(tmp_path: Path, data: bytes, valid: bool) -> None:
    fixture = output_setup(tmp_path, kind="structured")
    setup, manifest = fixture["setup"], fixture["manifest"]
    path = fixture["artifacts"].root / manifest["artifact_id"]
    (path / "content").write_bytes(data)
    raw = json.loads((path / "manifest.json").read_text())
    raw.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    (path / "manifest.json").write_text(json.dumps(raw), encoding="utf-8")
    manifest.update(size=len(data), sha256=raw["sha256"])
    if not valid:
        with pytest.raises(WorkflowDispatchError):
            setup.dispatch(fixture["executor"]())
        return
    observed = setup.dispatch(fixture["executor"]())
    setup.apply(observed)
    assert setup.repository.get(OWNER, "workflow-a").execution_state == "succeeded"


def test_external_output_free_contract_keeps_original_observation_bytes(tmp_path: Path) -> None:
    setup = make_external(tmp_path)
    observed = setup.dispatch()
    setup.apply(observed)
    with setup.repository.transaction(write=False) as connection:
        raw = bytes(connection.execute("SELECT observation_snapshot FROM workflow_dispatches").fetchone()[0])
    assert b"external_outputs" not in raw
    assert b"subject_digest" not in raw
    assert held(setup) == "held"


def original_set(fixture: dict[str, Any]) -> ExternalOutputSet:
    token = dispatch_token(OWNER, "workflow-a", "develop", 1)
    task = ExternalTaskRef(fixture["setup"].target.agent_id, token, external_id=token)
    return fixture["setup"].adapter.export_outputs(task, ())


@pytest.mark.parametrize("field,value", [
    ("artifact_id", "../escape"), ("artifact_id", ""), ("artifact_id", True),
    ("sha256", "a" * 63), ("sha256", "A" * 64), ("sha256", None),
    ("size", True), ("size", -1), ("size", 1.0), ("size", "1"), ("size", 256 * 1024 * 1024 + 1),
])
def test_external_output_reference_rejects_inexact_types(tmp_path: Path, field: str, value: Any) -> None:
    fixture = output_setup(tmp_path)
    reference = original_set(fixture).outputs[0].artifact
    with pytest.raises(WorkflowValidationError):
        ExternalArtifactOutput("report", replace(reference, **{field: value}))


@pytest.mark.parametrize("generation", [True, False, 0, -1, 1.0, "1", 9007199254740992])
def test_external_output_generation_is_exact_and_json_safe(tmp_path: Path, generation: Any) -> None:
    original = original_set(output_setup(tmp_path))
    with pytest.raises(WorkflowValidationError):
        replace(original, generation=generation)


@pytest.mark.parametrize("change", ["extra", "version", "task", "output", "artifact", "array", "duplicate", "noncanonical", "bom"])
def test_external_output_parser_accepts_only_the_complete_canonical_dto(tmp_path: Path, change: str) -> None:
    fixture = output_setup(tmp_path)
    original = original_set(fixture)
    value = original.payload()
    if change == "extra":
        value["text"] = "pretend output"
    elif change == "version":
        value["schema_version"] = True
    elif change == "task":
        value["task"].pop("session_id")
    elif change in {"output", "artifact"}:
        selected = value["outputs"][0] if change == "output" else value["outputs"][0]["artifact"]
        selected["extra"] = "forbidden"
    raw = canonical_json(value)
    if change == "array":
        raw = b"[]"
    elif change == "duplicate":
        raw = b'{"schema_version":1,' + raw[1:]
    elif change == "noncanonical":
        raw = json.dumps(value).encode()
    elif change == "bom":
        raw = b"\xef\xbb\xbf" + raw
    assert parse_external_outputs(original.snapshot, fixture["setup"].resolved, original.task) == original
    with pytest.raises(WorkflowValidationError):
        parse_external_outputs(raw, fixture["setup"].resolved, original.task)


def test_external_output_keys_and_distinct_byte_budget_are_bounded(tmp_path: Path) -> None:
    original = original_set(output_setup(tmp_path))
    item = original.outputs[0]
    outputs = tuple(replace(item, output_key=f"key-{i:02d}") for i in range(16))
    assert len(replace(original, outputs=outputs).outputs) == 16
    with pytest.raises(WorkflowValidationError):
        replace(original, outputs=outputs + (replace(item, output_key="extra"),))
    large = replace(item.artifact, size=256 * 1024 * 1024)
    repeated = tuple(replace(item, output_key=f"key-{i:02d}", artifact=large) for i in range(16))
    assert len(replace(original, outputs=repeated).outputs) == 16
    with pytest.raises(WorkflowValidationError, match="external_output_too_large"):
        replace(original, outputs=(replace(item, artifact=large),
            replace(item, output_key="second", artifact=replace(item.artifact, artifact_id="second"))))
    with pytest.raises(WorkflowValidationError, match="external_output_conflict"):
        replace(original, outputs=(item, replace(item, output_key="second", artifact=replace(item.artifact, sha256="f" * 64))))


def test_complete_external_proof_keeps_original_16k_limit_and_counts_source_bytes(tmp_path: Path) -> None:
    fixture = source_setup(tmp_path)
    original = original_set(fixture)
    item = original.outputs[0]
    source = replace(fixture["source"], repository_id="r" * 128, base_commit="a" * 64, head_commit="b" * 64,
        tree_id="c" * 64, source_bundle=replace(fixture["source"].source_bundle, artifact_id="s" * 128))
    exported = replace(original, outputs=tuple(replace(item, output_key=f"key{i:02d}" + "x" * 100,
        artifact=replace(item.artifact, artifact_id="a" * 128)) for i in range(16)))
    assert len(exported.snapshot) < 16384
    proof = VerifiedExternalOutputs(exported, tuple(NamedReference(output.output_key, source) for output in exported.outputs))
    assert len(canonical_json(proof.payload())) > 16384
    with pytest.raises(WorkflowValidationError, match="external_output_too_large"):
        proof.snapshot
    large = replace(item, artifact=replace(item.artifact, size=256 * 1024 * 1024))
    with pytest.raises(WorkflowValidationError, match="external_output_too_large"):
        VerifiedExternalOutputs(replace(original, outputs=(large,)), (NamedReference(item.output_key, fixture["source"]),))


@pytest.mark.parametrize("change", ["optional_missing", "required_missing", "too_large"])
def test_partial_external_output_set_matches_required_and_optional_specs(tmp_path: Path, change: str) -> None:
    fixture = output_setup(tmp_path, specs=[
        {"output_key": "report", "kind": "artifact", "required": True, "max_bytes": 100},
        {"output_key": "optional", "kind": "artifact", "required": False, "max_bytes": 100},
    ])
    setup = fixture["setup"]
    original_export = setup.adapter.export_outputs
    if change == "required_missing":
        setup.adapter.export_outputs = lambda task, specs: replace(original_export(task, specs), outputs=())
    elif change == "too_large":
        fixture["manifest"]["size"] = 101
    if change == "optional_missing":
        setup.apply(setup.dispatch(fixture["executor"]()))
        assert setup.repository.get(OWNER, "workflow-a").execution_state == "succeeded"
        return
    with pytest.raises(WorkflowDispatchError):
        setup.dispatch(fixture["executor"]())
    assert held(setup) == "held"


def test_external_optional_empty_set_commits_without_fabricating_an_artifact(tmp_path: Path) -> None:
    fixture = output_setup(tmp_path, optional=True)
    setup = fixture["setup"]
    original = setup.adapter.export_outputs
    setup.adapter.export_outputs = lambda task, specs: replace(original(task, specs), outputs=())
    observation = setup.dispatch(fixture["executor"]())
    assert observation.external_outputs.export.outputs == ()
    setup.apply(observation)
    with setup.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_artifact_refs").fetchone()[0] == 0
        assert connection.execute("SELECT subject_digest FROM workflow_steps").fetchone()[0] is not None
    assert held(setup) == "released"


@pytest.mark.parametrize("change", ["adapter", "target", "gate", "frozen", "workspace", "deadline"])
def test_external_committed_export_recovers_without_new_io_or_configuration(tmp_path: Path, change: str) -> None:
    fixture = output_setup(tmp_path)
    setup = fixture["setup"]
    observation = setup.dispatch(fixture["executor"]())
    setup.apply(observation)
    target = setup.target
    if change == "adapter":
        setup.adapter.account_ref = "replacement"
    elif change == "target":
        target = replace(target, configuration_digest="f" * 64)
    elif change != "gate":
        with setup.repository.transaction() as connection:
            if change == "frozen":
                connection.execute("UPDATE workflow_schema_meta SET admission_mode='frozen'")
            elif change == "workspace":
                connection.execute("UPDATE workspaces SET enabled=0")
            else:
                connection.execute("UPDATE workflow_steps SET active_deadline=0")
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("committed proof performed new external I/O")
    setup.adapter.inspect = setup.adapter.export_outputs = forbidden
    executor = ExternalStepExecutor(setup.store, target, setup.adapter, clock=setup.clock, enabled=False)
    assert asyncio.run(executor.inspect(observation.binding)) == observation
    assert len(setup.transport.sends) == len(fixture["exports"]) == 1


@pytest.mark.parametrize("state,evidence", [("running", "accepted"), ("unknown", "unknown"), ("succeeded", "transport"), ("failed", "agent_reported")])
def test_external_exports_are_only_queried_after_qualified_original_success(tmp_path: Path, state: str, evidence: str) -> None:
    fixture = output_setup(tmp_path)
    setup = fixture["setup"]
    setup.adapter.inspect = lambda task: ExternalAgentStatus(state, evidence, task.external_id, session_id=task.session_id)
    observation = setup.dispatch(fixture["executor"]())
    assert not fixture["exports"] and observation.external_outputs is None


def source_setup(tmp_path: Path, *, bundle_output: bool = False, admit: bool = True,
                 following_steps: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
    from test_workflow_source import repo_with_commit
    schema = {"type": "object", "required": ["repository_id"], "properties": {"repository_id": {"const": "repo-a"}}}
    specs = [{"output_key": "report", "kind": "code_revision", "required": True, "result_schema": schema}]
    if bundle_output:
        specs.append({"output_key": "bundle", "kind": "artifact", "required": True, "max_bytes": 10 * 1024 * 1024})
    fixture = output_setup(tmp_path, kind="code_revision", schema=schema, specs=specs, admit=admit,
        following_steps=following_steps)
    repo, base, head, tree = repo_with_commit(tmp_path)
    fixture["source"] = asyncio.run(SourceExporter(repo, fixture["artifacts"]).export(OWNER, WORKSPACE,
        repository_id="repo-a", base_commit=base, head_commit=head, tree_id=tree)).revision
    publish_descriptor(fixture)
    if bundle_output:
        original = fixture["setup"].adapter.export_outputs
        def export(task: ExternalTaskRef, specs: Any) -> ExternalOutputSet:
            result = original(task, specs)
            return replace(result, outputs=(*result.outputs, ExternalArtifactOutput("bundle", fixture["source"].source_bundle)))
        fixture["setup"].adapter.export_outputs = export
    return fixture


def publish_descriptor(fixture: dict[str, Any], value: dict[str, Any] | None = None) -> None:
    value = value or {"schema_version": 1, "source_revision": reference_values((NamedReference("source", fixture["source"]),))[0],
        "executor_id": "external-developer"}
    path = fixture["artifacts"].root.parent / "source-descriptor.json"
    path.write_bytes(canonical_json(value))
    manifest = fixture["artifacts"].put_file(OWNER, WORKSPACE, path, name=path.name, content_type="application/json")
    fixture["manifest"].clear()
    fixture["manifest"].update(manifest)


def test_external_code_revision_checks_archive_and_binds_bundle_in_original_closure(tmp_path: Path) -> None:
    fixture = source_setup(tmp_path)
    setup = fixture["setup"]
    observation = setup.dispatch(fixture["executor"]())
    assert observation.external_outputs.source_revisions == (NamedReference("report", fixture["source"]),)
    setup.apply(observation)
    with setup.repository.transaction(write=False) as connection:
        refs = connection.execute("SELECT output_key,artifact_id FROM workflow_artifact_refs ORDER BY output_key").fetchall()
        assert [tuple(row) for row in refs] == [("external-source:report", fixture["source"].source_bundle.artifact_id),
            ("output:report", fixture["manifest"]["artifact_id"])]
        setup.store.validate_snapshot(connection)
    assert len(setup.transport.sends) == 1


@pytest.mark.parametrize("change", ["schema", "executor", "repository", "base", "head", "tree", "kind"])
def test_external_code_descriptor_is_exact_and_bound_to_complete_source(tmp_path: Path, change: str) -> None:
    fixture = source_setup(tmp_path)
    value = {"schema_version": 1, "source_revision": reference_values((NamedReference("source", fixture["source"]),))[0],
        "executor_id": "external-developer"}
    if change == "schema":
        value["schema_version"] = True
    elif change == "executor":
        value["executor_id"] = True
    else:
        field = {"repository": "repository_id", "base": "base_commit", "head": "head_commit", "tree": "tree_id", "kind": "kind"}[change]
        value["source_revision"][field] = "wrong" if change in {"repository", "kind"} else "f" * 40
    publish_descriptor(fixture, value)
    with pytest.raises(WorkflowDispatchError):
        fixture["setup"].dispatch(fixture["executor"]())
    assert held(fixture["setup"]) == "held"


class TrackingReader:
    def __init__(self, handle: Any, *, chunk_size: int = 65536, error: BaseException | None = None,
                 started: threading.Event | None = None, release: threading.Event | None = None) -> None:
        self.handle = handle
        self.chunk_size = chunk_size
        self.error = error
        self.started, self.release = started, release
        self.requests: list[int] = []

    def __enter__(self) -> TrackingReader:
        return self

    def __exit__(self, *args: Any) -> None:
        self.handle.close()

    def read(self, size: int) -> bytes:
        assert 0 < size <= 65536
        self.requests.append(size)
        if self.started is not None:
            self.started.set()
            assert self.release.wait(5)
        if self.error is not None:
            raise self.error
        return self.handle.read(min(size, self.chunk_size))

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.handle.seek(offset, whence)


class TrackingArtifacts:
    def __init__(self, store: ArtifactStore, **options: Any) -> None:
        self.store, self.options = store, options
        self.handles: dict[str, list[TrackingReader]] = {}

    def get(self, *args: str) -> dict[str, Any] | None:
        return self.store.get(*args)

    def open_verified(self, owner: str, workspace: str, artifact_id: str) -> TrackingReader:
        handle = TrackingReader(self.store.open_verified(owner, workspace, artifact_id), **self.options)
        self.handles.setdefault(artifact_id, []).append(handle)
        return handle


def test_external_source_bundle_and_declared_output_share_one_owned_read(tmp_path: Path) -> None:
    fixture = source_setup(tmp_path, bundle_output=True)
    artifacts = TrackingArtifacts(fixture["artifacts"])
    original = original_set(fixture)
    proof = WorkflowExternalOutputs(artifacts).verify(fixture["setup"].resolved, original.task, original)
    assert {key for key, _ in proof.artifact_refs()} == {"output:bundle", "output:report", "external-source:report"}
    assert all(len(handles) == 1 and handles[0].handle.closed for handles in artifacts.handles.values())


@pytest.mark.parametrize("chunk_size,error", [(7, None), (65536, OSError("private source read error"))])
def test_external_output_reads_complete_chunks_and_closes_descriptors(tmp_path: Path, chunk_size: int, error: Any) -> None:
    fixture = output_setup(tmp_path, kind="structured")
    artifacts = TrackingArtifacts(fixture["artifacts"], chunk_size=chunk_size, error=error)
    original = original_set(fixture)
    verifier = WorkflowExternalOutputs(artifacts)
    if error is None:
        assert verifier.verify(fixture["setup"].resolved, original.task, original).export == original
    else:
        with pytest.raises(WorkflowExternalOutputsError) as observed:
            verifier.verify(fixture["setup"].resolved, original.task, original)
        assert observed.value.__cause__ is error
    assert all(reader.handle.closed for handles in artifacts.handles.values() for reader in handles)


@pytest.mark.parametrize("change", ["cancel", "frozen", "workspace", "generation", "deadline", "reservation"])
def test_external_output_authority_is_checked_before_adapter_export(tmp_path: Path, change: str) -> None:
    fixture = output_setup(tmp_path)
    setup = fixture["setup"]
    def inspect(task: ExternalTaskRef) -> ExternalAgentStatus:
        with setup.repository.transaction() as connection:
            updates = {"cancel": "UPDATE workflow_runs SET cancel_requested=1",
                "frozen": "UPDATE workflow_schema_meta SET admission_mode='frozen'",
                "workspace": "UPDATE workspaces SET enabled=0", "generation": "UPDATE workflow_steps SET is_current=0",
                "deadline": "UPDATE workflow_steps SET active_deadline=0", "reservation": "UPDATE capability_reservations SET state='released'"}
            connection.execute(updates[change])
        return ExternalAgentStatus("succeeded", "confirmed", task.external_id)
    setup.adapter.inspect = inspect
    with pytest.raises(WorkflowDispatchError):
        setup.dispatch(fixture["executor"]())
    assert not fixture["exports"]


@pytest.mark.parametrize("failed", [False, True])
def test_external_output_repeated_cancellation_joins_io_and_retains_failure(tmp_path: Path, failed: bool) -> None:
    fixture = output_setup(tmp_path)
    setup = fixture["setup"]
    setup.adapter.inspect = lambda task: ExternalAgentStatus("running", "accepted", task.external_id)
    initial = setup.dispatch(fixture["executor"]())
    started, release = threading.Event(), threading.Event()
    cause = OSError("private output failure") if failed else None
    artifacts = TrackingArtifacts(fixture["artifacts"], error=cause, started=started, release=release)
    executor = ExternalStepExecutor(setup.store, setup.target, setup.adapter, clock=setup.clock, outputs=WorkflowExternalOutputs(artifacts))
    setup.adapter.inspect = lambda task: ExternalAgentStatus("succeeded", "confirmed", task.external_id)
    async def scenario() -> None:
        pending = asyncio.create_task(executor.inspect(initial.binding))
        assert await asyncio.to_thread(started.wait, 5)
        for _ in range(3):
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError) as observed:
            await pending
        if failed:
            error: BaseException | None = observed.value
            seen: set[int] = set()
            while error is not None and id(error) not in seen and error is not cause:
                seen.add(id(error))
                error = error.__cause__ or error.__context__
            assert error is cause
    try:
        asyncio.run(scenario())
    finally:
        release.set()
    assert held(setup) == "held"
    assert all(reader.handle.closed for handles in artifacts.handles.values() for reader in handles)
    with setup.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_artifact_refs").fetchone()[0] == 0


@pytest.mark.parametrize("field,value", [("execution_id", "different"), ("kind", "task"), ("owner_id", "other")])
def test_external_output_read_authorization_uses_the_entire_binding(tmp_path: Path, field: str, value: str) -> None:
    fixture = output_setup(tmp_path)
    setup = fixture["setup"]
    observation = setup.dispatch(fixture["executor"]())
    with pytest.raises(WorkflowRepositoryError, match="dispatch_conflict"):
        setup.store.authorize_outputs(replace(observation.binding, **{field: value}), setup.target, now=setup.clock())


def test_external_configuration_change_during_export_cannot_produce_a_binding(tmp_path: Path) -> None:
    fixture = output_setup(tmp_path)
    setup = fixture["setup"]
    original = setup.adapter.export_outputs
    def export(task: ExternalTaskRef, specs: Any) -> ExternalOutputSet:
        result = original(task, specs)
        setup.adapter.account_ref = "replacement"
        return result
    setup.adapter.export_outputs = export
    with pytest.raises(WorkflowDispatchError):
        setup.dispatch(fixture["executor"]())
    assert held(setup) == "held"


@pytest.mark.parametrize("change", ["blob", "tree", "duplicate", "missing", "symlink", "path", "tail", "manifest_pax", "manifest_duplicate_key"])
def test_external_code_archive_checks_members_blobs_and_complete_tail(tmp_path: Path, change: str) -> None:
    fixture = source_setup(tmp_path)
    store, source = fixture["artifacts"], fixture["source"]
    original = store.read(OWNER, WORKSPACE, source.source_bundle.artifact_id)
    path = tmp_path / "changed-source.tar"
    with tarfile.open(fileobj=io.BytesIO(original), mode="r:") as archive:
        members = [(member, archive.extractfile(member).read()) for member in archive]
    if change == "blob":
        member, raw = members[-1]
        members[-1] = (member, b"X" * len(raw))
    elif change == "tree":
        member, raw = members[0]
        value = json.loads(raw)
        value["entries"][-1]["oid"] = "f" * 40
        raw = canonical_json(value)
        member.size = len(raw)
        members[0] = (member, raw)
    elif change == "duplicate":
        members.append(members[-1])
    elif change == "missing":
        members = members[:-1]
    elif change == "symlink":
        members[-1][0].type = tarfile.SYMTYPE
        members[-1][0].linkname = "../escape"
    elif change == "path":
        members[-1][0].name = "../escape"
    elif change == "manifest_pax":
        members[0][0].pax_headers["mtime"] = "1"
    elif change == "manifest_duplicate_key":
        member, raw = members[0]
        raw = b'{"schema_version":1,' + raw[1:]
        member.size = len(raw)
        members[0] = (member, raw)
    with tarfile.open(path, "w") as output:
        for member, contents in members:
            output.addfile(member, io.BytesIO(contents) if member.isfile() else None)
    if change == "tail":
        with path.open("ab") as output:
            output.write(b"unregistered member after end of archive")
    manifest = store.put_file(OWNER, WORKSPACE, path, name=path.name, content_type="application/x-tar")
    fixture["source"] = replace(source, source_bundle=ArtifactReference(manifest["artifact_id"], manifest["sha256"], manifest["size"]))
    publish_descriptor(fixture)
    with pytest.raises(WorkflowDispatchError):
        fixture["setup"].dispatch(fixture["executor"]())
    assert held(fixture["setup"]) == "held"
