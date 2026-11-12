"""Bounded Native WAV/MP3 inputs and immutable reference recovery."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_platform_provider import FakeTransport, _response
from test_workflow_native_text_inputs import make_text
from test_workflow_repository import OWNER

from hub.application.workflow_dispatch import WorkflowDispatchError
from hub.application.workflow_media_inputs import WorkflowMediaInputs
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_identity import canonical_json
from hub.domain.workflow_media_inputs import WorkflowAudioInput, parse_audio_inputs
from hub.domain.workflow_native_memory import build_native_memory, parse_native_memory
from hub.infrastructure.workflow_native_memory_repository import (
    WorkflowNativeMemoryRepository,
)
from hub.infrastructure.workflow_run_executor import RunStepExecutor
from hub.platform.artifacts import ArtifactStore
from hub.platform.providers.openai_compatible import (
    OpenAICompatibleProvider,
    ProviderError,
)

WAV = b"RIFF\x04\x00\x00\x00WAVE"
MP3 = b"\xff\xfb\x90\x64audio"


def _target(setup, *, audio_input: bool = True):
    config = json.loads(setup.target.config_snapshot)
    config["provider_snapshot"]["capabilities"]["image_input"] = not audio_input
    config["provider_snapshot"]["capabilities"]["audio_input"] = audio_input
    return replace(setup.target, config_snapshot=canonical_json(config), inline_text_inputs=False,
        inline_media_inputs=True)


@pytest.mark.parametrize(("content_type", "raw", "audio_format"), [
    ("audio/wav", WAV, "wav"), ("audio/mpeg", MP3, "mp3"),
])
def test_audio_metadata_is_frozen_without_bytes_and_worker_sends_provider_audio(
        tmp_path: Path, content_type: str, raw: bytes, audio_format: str) -> None:
    setup = make_text(tmp_path, raw=raw, content_type=content_type)
    setup.target = _target(setup)
    executor = RunStepExecutor(setup.workflows, setup.target, clock=setup.clock,
        media_inputs=WorkflowMediaInputs(setup.artifacts))
    binding = setup.dispatch(executor).binding
    run = setup.platform.get_run(OWNER, binding.run_id)
    reference = setup.resolved.contract.input_refs[0].reference
    assert run["config_snapshot"]["workflow_memory"] == {
        "schema_version": 6, "resolved_digest": setup.resolved.digest, "inputs": [],
        "audio_inputs": [{"ref_key": "input-0", "artifact_id": reference.artifact_id,
            "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw), "content_type": content_type}],
    }
    assert run["config_snapshot"]["workflow_media_mode"] == {"revision": 1}
    assert raw.decode("latin1") not in json.dumps(run["config_snapshot"], ensure_ascii=False)
    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == 14

    transport = FakeTransport([_response({"choices": [{"message": {"content": "transcribed"}}]})])
    provider = OpenAICompatibleProvider(model="test", endpoint="http://localhost", api_key="secret",
        transport=transport)
    worker = setup.worker([], guard=False)
    worker.provider_factory = lambda _profile: provider
    worker.workflow_memory_guard = WorkflowNativeMemoryRepository(setup.workflows,
        media_inputs=WorkflowMediaInputs(setup.artifacts), clock=setup.clock)
    assert worker.run_once(OWNER)["state"] == "succeeded"
    payload = json.loads(transport.calls[0]["body"])
    user = next(item for item in payload["messages"] if item["role"] == "user")
    assert user["content"][-1] == {"type": "input_audio", "input_audio": {
        "data": base64.b64encode(raw).decode("ascii"), "format": audio_format}}
    with setup.workflows.transaction(write=False) as connection:
        checkpoint = connection.execute("SELECT runtime_checkpoint FROM runs WHERE run_id=?",
            (binding.run_id,)).fetchone()[0]
    assert base64.b64encode(raw).decode("ascii") not in checkpoint
    assert "input_audio" not in checkpoint


def test_audio_capability_and_mime_are_checked_before_run_creation(tmp_path: Path) -> None:
    setup = make_text(tmp_path, raw=WAV, content_type="audio/wav")
    setup.target = _target(setup, audio_input=False)
    executor = RunStepExecutor(setup.workflows, setup.target, clock=setup.clock,
        media_inputs=WorkflowMediaInputs(setup.artifacts))
    with pytest.raises(WorkflowDispatchError):
        setup.dispatch(executor)
    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 0

    unsupported = make_text(tmp_path / "unsupported", raw=b"data", content_type="audio/ogg")
    unsupported.target = _target(unsupported)
    with pytest.raises(WorkflowDispatchError):
        unsupported.dispatch(RunStepExecutor(unsupported.workflows, unsupported.target,
            clock=unsupported.clock, media_inputs=WorkflowMediaInputs(unsupported.artifacts)))

    oversized = make_text(tmp_path / "count", raw=WAV, content_type="audio/wav", count=5)
    oversized.target = _target(oversized)
    with pytest.raises(WorkflowDispatchError, match="media_inputs_too_large"):
        oversized.dispatch(RunStepExecutor(oversized.workflows, oversized.target,
            clock=oversized.clock, media_inputs=WorkflowMediaInputs(oversized.artifacts)))
    with oversized.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_audio_snapshot_parser_is_strict_and_preserves_image_schema_five(tmp_path: Path) -> None:
    setup = make_text(tmp_path, raw=WAV, content_type="audio/wav")
    reference = setup.resolved.contract.input_refs[0].reference
    audio = WorkflowAudioInput("input-0", reference, "audio/wav")
    memory = build_native_memory(setup.resolved, (), audio= (audio,), audio_mode=True)
    value = json.loads(memory.snapshot)
    assert value["schema_version"] == 6
    assert value["audio_inputs"][0]["ref_key"] == "input-0"
    assert parse_native_memory(value, setup.resolved) == memory
    value["audio_inputs"][0]["content_type"] = "audio/ogg"
    with pytest.raises(WorkflowValidationError):
        parse_native_memory(value, setup.resolved)
    image_setup = make_text(tmp_path / "image", raw=b"png", content_type="image/png")
    image_ref = image_setup.resolved.contract.input_refs[0].reference
    from hub.domain.workflow_media_inputs import WorkflowMediaInput
    image_memory = build_native_memory(image_setup.resolved, (),
        media=(WorkflowMediaInput("input-0", image_ref, "image/png"),), media_mode=True)
    assert image_memory.snapshot == canonical_json({"schema_version": 5,
        "resolved_digest": image_setup.resolved.digest, "inputs": [],
        "media_inputs": [{"ref_key": "input-0", "artifact_id": image_ref.artifact_id,
            "sha256": hashlib.sha256(b"png").hexdigest(), "size": 3, "content_type": "image/png"}]})


def test_audio_metadata_rejects_boolean_size_and_wrong_mime(tmp_path: Path) -> None:
    from hub.domain.workflow_contract_types import ArtifactReference

    reference = ArtifactReference("audio-a", hashlib.sha256(WAV).hexdigest(), len(WAV))
    with pytest.raises(WorkflowValidationError):
        parse_audio_inputs([{"ref_key": "input-0", "artifact_id": "audio-a", "sha256": reference.sha256,
            "size": True, "content_type": "audio/wav"}])
    with pytest.raises(WorkflowValidationError):
        setup = make_text(tmp_path, raw=WAV, content_type="audio/wav")
        audio = WorkflowAudioInput("input-0", setup.resolved.contract.input_refs[0].reference, "audio/ogg")
        build_native_memory(setup.resolved, (), audio=(audio,), audio_mode=True)


def test_ready_audio_checkpoint_rereads_artifact_after_restart(tmp_path: Path) -> None:
    from hub.platform.providers.base import ModelResponse

    setup = make_text(tmp_path, raw=WAV, content_type="audio/wav")
    setup.target = _target(setup)
    artifacts = WorkflowMediaInputs(setup.artifacts)
    binding = setup.dispatch(RunStepExecutor(setup.workflows, setup.target,
        clock=setup.clock, media_inputs=artifacts)).binding
    reads = []

    class ObservedArtifacts:
        def get(self, *args):
            return setup.artifacts.get(*args)

        def open_verified(self, *args):
            reads.append(args[-1])
            return setup.artifacts.open_verified(*args)

    media_inputs = WorkflowMediaInputs(ObservedArtifacts())
    calls = []

    class Provider:
        def complete(self, messages, tools, *, request_observer=None):
            calls.append(messages)
            if len(calls) == 1:
                return ModelResponse(kind="tool_call", tool="workspace.list", arguments={"path": ""})
            return ModelResponse(kind="final", text="done")

    worker = setup.worker([], guard=False)
    worker.provider_factory = lambda _profile: Provider()
    worker.workflow_memory_guard = WorkflowNativeMemoryRepository(setup.workflows,
        media_inputs=media_inputs, clock=setup.clock)
    original_save = setup.platform.save_runtime_checkpoint

    class SimulatedExit(BaseException):
        pass

    def exit_after_ready(*args, **kwargs):
        result = original_save(*args, **kwargs)
        if args[2]["phase"] == "ready":
            raise SimulatedExit()
        return result

    setup.platform.save_runtime_checkpoint = exit_after_ready
    with pytest.raises(SimulatedExit):
        worker.run_once(OWNER)
    setup.platform.save_runtime_checkpoint = original_save
    with setup.platform._connect() as connection:
        connection.execute("UPDATE runs SET lease_expires_at=0 WHERE run_id=?", (binding.run_id,))

    restarted = setup.worker([], guard=False)
    restarted.provider_factory = lambda _profile: Provider()
    restarted.workflow_memory_guard = WorkflowNativeMemoryRepository(setup.workflows,
        media_inputs=media_inputs, clock=setup.clock)
    assert restarted.run_once(OWNER)["state"] == "succeeded"
    assert len(calls) == 2
    assert all(next(message for message in attempt if message["role"] == "user")["content"][-1]["type"]
        == "input_audio" for attempt in calls)
    assert reads.count("native-input-a") == 2
    with setup.workflows.transaction(write=False) as connection:
        checkpoint = connection.execute("SELECT runtime_checkpoint FROM runs WHERE run_id=?",
            (binding.run_id,)).fetchone()[0]
    assert base64.b64encode(WAV).decode("ascii") not in checkpoint


def test_audio_provider_pending_recovery_never_reopens_or_resends(tmp_path: Path) -> None:
    from test_workflow_native_text_inputs import ObservedArtifacts

    setup = make_text(tmp_path, raw=WAV, content_type="audio/wav")
    setup.target = _target(setup)
    observed = ObservedArtifacts(setup.artifacts)
    binding = setup.dispatch(RunStepExecutor(setup.workflows, setup.target,
        clock=setup.clock, media_inputs=WorkflowMediaInputs(observed))).binding
    calls = []

    class SimulatedExit(BaseException):
        pass

    class InterruptedProvider:
        def complete(self, messages, tools, *, request_observer=None):
            calls.append(messages)
            raise SimulatedExit()

    def worker():
        result = setup.worker([], guard=False)
        result.provider_factory = lambda _profile: InterruptedProvider()
        result.workflow_memory_guard = WorkflowNativeMemoryRepository(setup.workflows,
            media_inputs=WorkflowMediaInputs(observed), clock=setup.clock)
        return result

    with pytest.raises(SimulatedExit):
        worker().run_once(OWNER)
    with setup.workflows.transaction() as connection:
        checkpoint = connection.execute("SELECT runtime_checkpoint FROM runs WHERE run_id=?",
            (binding.run_id,)).fetchone()[0]
        assert json.loads(checkpoint)["phase"] == "provider_pending"
        connection.execute("UPDATE runs SET lease_expires_at=0 WHERE run_id=?", (binding.run_id,))
    assert worker().run_once(OWNER)["state"] == "unknown"
    assert observed.opens == 2
    assert len(calls) == 1
    assert base64.b64encode(WAV).decode("ascii") not in checkpoint


@pytest.mark.parametrize("change", ["workspace", "cancel", "pause", "deadline", "manifest"])
def test_audio_read_rechecks_current_execution_before_provider_creation(tmp_path: Path, change: str) -> None:
    setup = make_text(tmp_path, raw=WAV, content_type="audio/wav")
    setup.target = _target(setup)
    setup.dispatch(RunStepExecutor(setup.workflows, setup.target,
        clock=setup.clock, media_inputs=WorkflowMediaInputs(setup.artifacts)))
    changed = False

    class ChangedDuringRead:
        def get(self, owner, workspace, artifact):
            value = setup.artifacts.get(owner, workspace, artifact)
            return {**value, "sha256": "0" * 64} if changed and change == "manifest" else value

        def open_verified(self, owner, workspace, artifact):
            nonlocal changed
            handle = setup.artifacts.open_verified(owner, workspace, artifact)
            with setup.workflows.transaction() as connection:
                if change == "workspace":
                    connection.execute("UPDATE workspaces SET enabled=0")
                elif change == "cancel":
                    connection.execute("UPDATE workflow_runs SET cancel_requested=1")
                elif change == "pause":
                    connection.execute("UPDATE workflow_runs SET pause_requested=1")
                elif change == "deadline":
                    connection.execute("UPDATE workflow_steps SET active_deadline=?", (setup.clock(),))
            changed = True
            return handle

    worker = setup.worker([], guard=False)
    worker.workflow_memory_guard = WorkflowNativeMemoryRepository(setup.workflows,
        media_inputs=WorkflowMediaInputs(ChangedDuringRead()), clock=setup.clock)
    factories = []
    original_factory = worker.provider_factory
    worker.provider_factory = lambda profile: (factories.append(profile), original_factory(profile))[1]
    result = worker.run_once(OWNER)
    assert factories == []
    assert result["state"] == "failed"


def test_audio_reference_is_required_by_history_and_reader_fourteen_gate(tmp_path: Path) -> None:
    from hub.infrastructure.workflow_native_memory_repository import (
        validate_native_memory_runs,
    )
    from hub.infrastructure.workflow_repository import WorkflowRepositoryError

    setup = make_text(tmp_path, raw=WAV, content_type="audio/wav")
    setup.target = _target(setup)
    setup.dispatch(RunStepExecutor(setup.workflows, setup.target,
        clock=setup.clock, media_inputs=WorkflowMediaInputs(setup.artifacts)))
    with setup.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=13")
    with pytest.raises(WorkflowRepositoryError):
        setup.workflows.init()
    with setup.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=14")
        connection.execute("DELETE FROM workflow_artifact_refs WHERE workflow_id='workflow-a'")
    with (setup.workflows.transaction(write=False) as connection,
          pytest.raises(WorkflowValidationError, match="native_memory_reference_mismatch")):
        validate_native_memory_runs(connection)


@pytest.mark.parametrize("encrypted", [False, True])
def test_audio_restore_preserves_frozen_metadata_and_original_artifact(tmp_path: Path, encrypted: bool) -> None:
    from hub.infrastructure.platform_backup import PlatformBackupService
    from hub.infrastructure.platform_db import PlatformRepository
    from hub.infrastructure.workflow_repository import WorkflowRepository

    setup = make_text(tmp_path, raw=WAV, content_type="audio/wav")
    setup.target = _target(setup)
    binding = setup.dispatch(RunStepExecutor(setup.workflows, setup.target,
        clock=setup.clock, media_inputs=WorkflowMediaInputs(setup.artifacts))).binding
    original = setup.platform.get_run(OWNER, binding.run_id)["config_snapshot"]
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    service = PlatformBackupService()
    if encrypted:
        summary = service.create_encrypted_backup(setup.workflows.db_path, setup.artifacts.root,
            backup, b"k" * 32, "native-audio-key")["source"]
        service.restore_encrypted_backup(backup, restored, b"k" * 32)
    else:
        summary = service.create_backup(setup.workflows.db_path, setup.artifacts.root, backup)
        service.restore_backup(backup, restored)
    assert summary["workflow"] == {"schema_version": 1, "minimum_reader_version": 14}
    repository = WorkflowRepository(restored / "platform.db")
    repository.init()
    store = ArtifactStore(restored / "artifacts")
    executor = RunStepExecutor(repository, setup.target, clock=setup.clock,
        media_inputs=WorkflowMediaInputs(store))
    assert asyncio.run(executor.lookup(binding.token, setup.resolved.digest, setup.resolved)) == binding
    assert PlatformRepository(repository.db_path).get_run(OWNER, binding.run_id)["config_snapshot"] == original
    with repository.transaction(write=False) as connection:
        assert tuple(connection.execute(
            "SELECT admission_mode,minimum_reader_version FROM workflow_schema_meta").fetchone()) == ("frozen", 14)
    snapshot = parse_native_memory(original["workflow_memory"], setup.resolved)
    assert WorkflowMediaInputs(store).load_audio(setup.resolved, snapshot.audio,
        capabilities={"audio_input": True}) == ({"type": "input_audio", "input_audio": {
            "data": base64.b64encode(WAV).decode("ascii"), "format": "wav"}},)


@pytest.mark.parametrize("part", [
    {"type": "input_audio", "input_audio": {"data": "!!!!", "format": "wav"}},
    {"type": "input_audio", "input_audio": {"data": "YQ==", "format": "flac"}},
    {"type": "input_audio", "input_audio": {"data": "YQ==", "format": "wav", "extra": 1}},
    {"type": "input_audio", "input_audio": {"data": "YQ==", "format": "wav"}, "extra": 1},
])
def test_provider_rejects_malformed_audio_before_transport(part) -> None:

    transport = FakeTransport([])
    provider = OpenAICompatibleProvider(model="test", endpoint="http://localhost", api_key="secret",
        transport=transport)
    with pytest.raises(ProviderError, match="provider_message_invalid"):
        provider.complete([{"role": "user", "content": [part]}], [])
    assert transport.calls == []

def test_provider_serializes_ordered_wav_mp3_parts_and_preserves_image_wire() -> None:
    transport = FakeTransport([_response({"choices": [{"message": {"content": "ok"}}]})])
    provider = OpenAICompatibleProvider(model="test", endpoint="http://localhost", api_key="secret",
        transport=transport)
    content = [
        {"type": "text", "text": "first"},
        {"type": "input_audio", "input_audio": {"data": base64.b64encode(WAV).decode(), "format": "wav"}},
        {"type": "text", "text": "middle"},
        {"type": "input_audio", "input_audio": {"data": base64.b64encode(MP3).decode(), "format": "mp3"}},
    ]
    provider.complete([{"role": "user", "content": content}], [])
    assert json.loads(transport.calls[0]["body"])["messages"][0]["content"] == content


@pytest.mark.parametrize("raws", [
    [b"x"] * 5,
    [b"x" * (2 * 1024 * 1024 + 1)],
    [b"x" * (2 * 1024 * 1024), b"x" * (2 * 1024 * 1024), b"x"],
])
def test_provider_audio_reference_and_byte_budgets_fail_before_transport(raws: list[bytes]) -> None:
    transport = FakeTransport([])
    provider = OpenAICompatibleProvider(model="test", endpoint="http://localhost", api_key="secret",
        transport=transport)
    content = [{"type": "input_audio", "input_audio": {
        "data": base64.b64encode(raw).decode("ascii"),
        "format": "wav"}} for raw in raws]
    with pytest.raises(ProviderError, match="provider_message_invalid"):
        provider.complete([{"role": "user", "content": content}], [])
    assert transport.calls == []
