"""End-to-end bounded Native image inputs and immutable reference recovery."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from test_external_agent_memory_inputs import memory_item
from test_workflow_native_text_inputs import make_text
from test_workflow_repository import OWNER

from hub.application.workflow_dispatch import WorkflowDispatchError
from hub.application.workflow_media_inputs import WorkflowMediaInputs
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_identity import canonical_json
from hub.domain.workflow_media_inputs import WorkflowMediaInput
from hub.domain.workflow_native_memory import build_native_memory, parse_native_memory
from hub.infrastructure.workflow_native_memory_repository import (
    WorkflowNativeMemoryRepository,
)
from hub.infrastructure.workflow_run_executor import RunStepExecutor
from hub.platform.artifacts import ArtifactStore

PNG = b"\x89PNG\r\n\x1a\nsmall-image-test"


def _target(setup, *, image_input: bool = True):
    config = json.loads(setup.target.config_snapshot)
    config["provider_snapshot"]["capabilities"]["image_input"] = image_input
    return replace(setup.target, config_snapshot=canonical_json(config), inline_text_inputs=False,
        inline_media_inputs=True)


def _executor(setup, artifacts=None):
    return RunStepExecutor(setup.workflows, setup.target, clock=setup.clock,
        media_inputs=WorkflowMediaInputs(artifacts or setup.artifacts))


def test_media_target_requires_explicit_provider_capability_and_strict_flag(tmp_path: Path) -> None:
    setup = make_text(tmp_path, raw=PNG, content_type="image/png")
    with pytest.raises(ValueError, match="invalid_text_input_support"):
        replace(setup.target, inline_media_inputs=1)
    with pytest.raises(ValueError, match="unsupported_media_input_target"):
        _target(setup, image_input=False)


def test_native_image_metadata_is_frozen_without_bytes_and_worker_sends_verified_image(tmp_path: Path) -> None:
    setup = make_text(tmp_path, raw=PNG, content_type="image/png")
    setup.target = _target(setup)
    media = WorkflowMediaInputs(setup.artifacts)
    binding = setup.dispatch(_executor(setup)).binding
    run = setup.platform.get_run(OWNER, binding.run_id)
    snapshot = run["config_snapshot"]["workflow_memory"]
    assert snapshot == {"schema_version": 5, "resolved_digest": setup.resolved.digest, "inputs": [],
        "media_inputs": [{"ref_key": "input-0", "artifact_id": "native-input-a",
            "sha256": hashlib.sha256(PNG).hexdigest(), "size": len(PNG), "content_type": "image/png"}]}
    assert "workflow_media_mode" in run["config_snapshot"]
    assert PNG.decode("latin1") not in json.dumps(run["config_snapshot"], ensure_ascii=False)
    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == 13

    captured = []
    worker = setup.worker(captured, guard=False)
    worker.workflow_memory_guard = WorkflowNativeMemoryRepository(setup.workflows, media_inputs=media, clock=setup.clock)
    assert worker.run_once(OWNER)["state"] == "succeeded"
    user = next(message for message in captured[0] if message.get("role") == "user")
    assert user["content"][-1] == {"type": "image_url", "image_url": {
        "url": "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")}}
    with setup.workflows.transaction(write=False) as connection:
        checkpoint = connection.execute("SELECT runtime_checkpoint FROM runs WHERE run_id=?", (binding.run_id,)).fetchone()[0]
    assert base64.b64encode(PNG).decode("ascii") not in checkpoint
    assert "data:image/png" not in checkpoint
    from hub.infrastructure.platform_backup import PlatformBackupService
    from hub.infrastructure.workflow_repository import WorkflowRepository
    backup, restored_root = tmp_path / "backup", tmp_path / "restored"
    service = PlatformBackupService()
    summary = service.create_backup(setup.workflows.db_path, setup.artifacts.root, backup)
    assert summary["workflow"] == {"schema_version": 1, "minimum_reader_version": 13}
    service.restore_backup(backup, restored_root)
    restored = WorkflowRepository(restored_root / "platform.db")
    restored.init()
    assert asyncio.run(RunStepExecutor(restored, setup.target, clock=setup.clock,
        media_inputs=WorkflowMediaInputs(ArtifactStore(restored_root / "artifacts"))).lookup(
            binding.token, setup.resolved.digest, setup.resolved)) == binding


def test_media_snapshot_roundtrips_strictly_and_legacy_schema_stays_unchanged(tmp_path: Path) -> None:
    setup = make_text(tmp_path, raw=PNG, content_type="image/png")
    reference = setup.resolved.contract.input_refs[0].reference
    image = WorkflowMediaInput("input-0", reference, "image/png")
    snapshot = build_native_memory(setup.resolved, (), media=(image,), media_mode=True)
    assert snapshot.snapshot == build_native_memory(setup.resolved, (), media=(image,), media_mode=True).snapshot
    assert parse_native_memory(json.loads(snapshot.snapshot), setup.resolved) == snapshot
    legacy_setup = make_text(tmp_path / "legacy", memories=True)
    legacy = build_native_memory(legacy_setup.resolved, (memory_item(),))
    assert json.loads(legacy.snapshot)["schema_version"] == 1
    damaged = json.loads(snapshot.snapshot)
    damaged["media_inputs"][0]["content_type"] = "application/octet-stream"
    with pytest.raises(WorkflowValidationError, match="media_input_unsupported"):
        parse_native_memory(damaged, setup.resolved)


@pytest.mark.parametrize(("content_type", "raw"), [
    ("application/octet-stream", b"data"), ("image/svg+xml", b"<svg/>")])
def test_unapproved_or_non_image_artifacts_fail_before_run_creation(tmp_path: Path, content_type: str, raw: bytes) -> None:
    setup = make_text(tmp_path, raw=raw, content_type=content_type)
    setup.target = _target(setup)
    with pytest.raises(WorkflowDispatchError):
        setup.dispatch(_executor(setup))
    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_media_count_and_size_budgets_reject_before_artifact_io(tmp_path: Path) -> None:
    setup = make_text(tmp_path, raw=b"x", content_type="image/png", count=5)
    setup.target = _target(setup)
    with pytest.raises(WorkflowDispatchError, match="media_inputs_too_large"):
        setup.dispatch(_executor(setup))
    oversized = make_text(tmp_path / "large", raw=b"x" * (2 * 1024 * 1024 + 1), content_type="image/png")
    oversized.target = _target(oversized)
    with pytest.raises(WorkflowDispatchError, match="media_inputs_too_large"):
        oversized.dispatch(_executor(oversized))


def test_media_target_reports_unsupported_code_as_dispatch_error_before_io(tmp_path: Path) -> None:
    from test_workflow_native_code_inputs import _resolved_code_source
    from test_workflow_native_text_inputs import assert_no_run

    setup, resolved, _revision = _resolved_code_source(tmp_path)
    setup.target = _target(setup)

    class UnreadableArtifacts:
        def get(self, *_args):
            pytest.fail("unsupported code input reached media storage")

        def open_verified(self, *_args):
            pytest.fail("unsupported code input opened media storage")

    with pytest.raises(WorkflowDispatchError, match="media_input_unsupported") as caught:
        asyncio.run(_executor(setup, UnreadableArtifacts()).create_or_lookup(
            "a" * 64, resolved.digest, resolved))
    assert caught.value.uncertain is False
    assert_no_run(setup)


@pytest.mark.parametrize(("count", "size"), [(4, 1), (2, 2 * 1024 * 1024)])
def test_repeated_media_refs_keep_logical_parts_with_one_read_per_artifact(
        tmp_path: Path, count: int, size: int) -> None:
    from test_workflow_native_text_inputs import ObservedArtifacts

    raw = b"x" * size
    setup = make_text(tmp_path, raw=raw, content_type="image/png", count=count)
    observed = ObservedArtifacts(setup.artifacts)
    media = WorkflowMediaInputs(observed)
    inputs = media.collect(setup.resolved)
    assert len(inputs) == count and observed.opens == 1
    snapshot = build_native_memory(setup.resolved, (), media=inputs, media_mode=True)
    assert parse_native_memory(json.loads(snapshot.snapshot), setup.resolved) == snapshot
    parts = media.load(setup.resolved, inputs)
    assert len(parts) == count and observed.opens == 2
    expected = {"type": "image_url", "image_url": {
        "url": "data:image/png;base64," + base64.b64encode(raw).decode("ascii")}}
    assert all(part == expected for part in parts)
    assert all(handle.closed for handle in observed.handles)


@pytest.mark.parametrize(("count", "size"), [(5, 1), (3, 2 * 1024 * 1024),
                                                           (4, 1024 * 1024 + 1)])
def test_media_logical_budget_rejects_duplicates_before_manifest_or_descriptor_io(
        tmp_path: Path, count: int, size: int) -> None:
    from test_workflow_native_text_inputs import assert_no_run

    setup = make_text(tmp_path, raw=b"x" * size, content_type="image/png", count=count)
    setup.target = _target(setup)

    class UnreadableArtifacts:
        def get(self, *_args):
            pytest.fail("over-budget input reached manifest storage")

        def open_verified(self, *_args):
            pytest.fail("over-budget input opened an artifact")

    media = WorkflowMediaInputs(UnreadableArtifacts())
    with pytest.raises(WorkflowValidationError, match="media_inputs_too_large"):
        media.collect(setup.resolved)
    with pytest.raises(WorkflowDispatchError, match="media_inputs_too_large"):
        setup.dispatch(_executor(setup, UnreadableArtifacts()))
    assert_no_run(setup)


def test_media_load_rejects_corrupt_verified_descriptor(tmp_path: Path) -> None:
    setup = make_text(tmp_path, raw=PNG, content_type="image/png")
    setup.target = _target(setup)
    media = WorkflowMediaInputs(setup.artifacts)
    inputs = media.collect(setup.resolved)
    class CorruptArtifacts:
        def get(self, owner, workspace, artifact):
            return setup.artifacts.get(owner, workspace, artifact)
        def open_verified(self, owner, workspace, artifact):
            from io import BytesIO
            return BytesIO(PNG + b"changed")
    with pytest.raises(WorkflowValidationError, match="media_input_mismatch"):
        WorkflowMediaInputs(CorruptArtifacts()).load(setup.resolved, inputs)


def test_ready_checkpoint_keeps_media_ephemeral_and_replays_same_reference_only(tmp_path: Path) -> None:
    from hub.application.workflow_media_inputs import WorkflowMediaInputs
    from hub.infrastructure.workflow_native_memory_repository import (
        WorkflowNativeMemoryRepository,
    )
    from hub.platform.providers.base import ModelResponse

    setup = make_text(tmp_path, raw=PNG, content_type="image/png")
    setup.target = _target(setup)
    artifact_reads = []
    class ObservedArtifacts:
        def get(self, owner, workspace, artifact):
            return setup.artifacts.get(owner, workspace, artifact)
        def open_verified(self, owner, workspace, artifact):
            artifact_reads.append(artifact)
            return setup.artifacts.open_verified(owner, workspace, artifact)
    artifacts = ObservedArtifacts()
    media_inputs = WorkflowMediaInputs(artifacts)
    binding = setup.dispatch(_executor(setup, artifacts)).binding
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
    first_user = next(message for message in calls[0] if message["role"] == "user")
    second_user = next(message for message in calls[1] if message["role"] == "user")
    assert first_user["content"][-1]["type"] == "image_url"
    assert second_user["content"][-1]["type"] == "image_url"
    assert artifact_reads.count("native-input-a") == 3  # admission plus both attempts
    with setup.workflows.transaction(write=False) as connection:
        checkpoint = connection.execute("SELECT runtime_checkpoint FROM runs WHERE run_id=?", (binding.run_id,)).fetchone()[0]
    assert base64.b64encode(PNG).decode("ascii") not in checkpoint
    assert "data:image/png" not in checkpoint


def test_media_artifact_reference_is_required_by_history_and_backup_closure(tmp_path: Path) -> None:
    from hub.infrastructure.workflow_native_memory_repository import (
        validate_native_memory_runs,
    )

    setup = make_text(tmp_path, raw=PNG, content_type="image/png")
    setup.target = _target(setup)
    setup.dispatch(_executor(setup))
    with setup.workflows.transaction() as connection:
        connection.execute("DELETE FROM workflow_artifact_refs WHERE workflow_id='workflow-a'")
    with (setup.workflows.transaction(write=False) as connection,
          pytest.raises(WorkflowValidationError, match="native_memory_reference_mismatch")):
        validate_native_memory_runs(connection)


@pytest.mark.parametrize("reader", [1, 8, 9, 10, 11, 12])
def test_media_reader_downgrade_rejected_at_startup_gate_off_and_backup(
        tmp_path: Path, reader: int) -> None:
    from hub.infrastructure.platform_backup import BackupError, PlatformBackupService
    from hub.composition.workflows import assert_workflow_visibility
    from hub.infrastructure.workflow_repository import WorkflowRepositoryError

    setup = make_text(tmp_path, raw=PNG, content_type="image/png")
    setup.target = _target(setup)
    setup.dispatch(_executor(setup))
    with setup.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=?", (reader,))
    with pytest.raises(WorkflowRepositoryError):
        setup.workflows.init()
    with pytest.raises(WorkflowRepositoryError):
        assert_workflow_visibility(setup.workflows.db_path, enabled=False)
    with pytest.raises(BackupError):
        PlatformBackupService().create_backup(
            setup.workflows.db_path, setup.artifacts.root, tmp_path / "downgraded-backup")


@pytest.mark.parametrize("damage", ["mime", "flag", "reference", "missing_run"])
def test_media_history_corruption_rejected_by_repository_and_backup(
        tmp_path: Path, damage: str) -> None:
    from hub.infrastructure.platform_backup import BackupError, PlatformBackupService
    from hub.composition.workflows import assert_workflow_visibility
    from hub.infrastructure.workflow_repository import WorkflowRepositoryError

    setup = make_text(tmp_path, raw=PNG, content_type="image/png")
    setup.target = _target(setup)
    binding = setup.dispatch(_executor(setup)).binding
    config = setup.platform.get_run(OWNER, binding.run_id)["config_snapshot"]
    # Simulate already-corrupt history, including a broken foreign key, as a
    # damaged database imported from outside the repository transaction port.
    with closing(sqlite3.connect(setup.workflows.db_path)) as connection:
        if damage == "mime":
            config["workflow_memory"]["media_inputs"][0]["content_type"] = "text/plain"
        elif damage == "flag":
            config.pop("workflow_media_mode")
        elif damage == "reference":
            connection.execute("UPDATE workflow_artifact_refs SET sha256=? WHERE workflow_id='workflow-a'",
                               ("f" * 64,))
        else:
            connection.execute("UPDATE runs SET run_id='missing-media-run' WHERE run_id=?", (binding.run_id,))
        if damage in {"mime", "flag"}:
            connection.execute("UPDATE runs SET config_snapshot=? WHERE run_id=?",
                               (canonical_json(config).decode(), binding.run_id))
        connection.commit()
    with pytest.raises(WorkflowRepositoryError):
        setup.workflows.init()
    with pytest.raises(WorkflowRepositoryError):
        assert_workflow_visibility(setup.workflows.db_path, enabled=False)
    with pytest.raises(BackupError):
        PlatformBackupService().create_backup(
            setup.workflows.db_path, setup.artifacts.root, tmp_path / "corrupt-backup")


@pytest.mark.parametrize("encrypted", [False, True])
def test_media_restore_keeps_frozen_metadata_and_original_artifact(
        tmp_path: Path, encrypted: bool) -> None:
    from hub.infrastructure.platform_backup import PlatformBackupService
    from hub.infrastructure.platform_db import PlatformRepository
    from hub.infrastructure.workflow_repository import WorkflowRepository

    setup = make_text(tmp_path, raw=PNG, content_type="image/png", count=2)
    setup.target = _target(setup)
    binding = setup.dispatch(_executor(setup)).binding
    original = setup.platform.get_run(OWNER, binding.run_id)["config_snapshot"]
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    service = PlatformBackupService()
    if encrypted:
        summary = service.create_encrypted_backup(setup.workflows.db_path, setup.artifacts.root,
            backup, b"k" * 32, "native-media-key")["source"]
        service.restore_encrypted_backup(backup, restored, b"k" * 32)
    else:
        summary = service.create_backup(setup.workflows.db_path, setup.artifacts.root, backup)
        service.restore_backup(backup, restored)
    assert summary["workflow"] == {"schema_version": 1, "minimum_reader_version": 13}
    repository = WorkflowRepository(restored / "platform.db")
    repository.init()
    store = ArtifactStore(restored / "artifacts")
    executor = RunStepExecutor(repository, setup.target, clock=setup.clock,
                               media_inputs=WorkflowMediaInputs(store))
    assert asyncio.run(executor.lookup(binding.token, setup.resolved.digest, setup.resolved)) == binding
    run = PlatformRepository(repository.db_path).get_run(OWNER, binding.run_id)
    assert run["config_snapshot"] == original
    with repository.transaction(write=False) as connection:
        assert tuple(connection.execute("SELECT admission_mode,minimum_reader_version FROM workflow_schema_meta").fetchone()) == ("frozen", 13)
    frozen = parse_native_memory(original["workflow_memory"], setup.resolved)
    parts = WorkflowMediaInputs(store).load(setup.resolved, frozen.media)
    assert len(parts) == 2
    assert parts[0] == parts[1] == {"type": "image_url", "image_url": {
        "url": "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")}}


def test_text_input_order_cannot_change_when_media_support_is_installed(tmp_path: Path) -> None:
    from hub.application.workflow_text_inputs import WorkflowTextInputs

    setup = make_text(tmp_path, count=2)
    texts = WorkflowTextInputs(setup.artifacts).collect(setup.resolved)
    with pytest.raises(WorkflowValidationError, match="artifact_input_mismatch"):
        build_native_memory(setup.resolved, (), texts=tuple(reversed(texts)))


def test_media_bytes_cannot_be_silently_omitted_from_a_legacy_snapshot(tmp_path: Path) -> None:
    setup = make_text(tmp_path, raw=PNG, content_type="image/png")
    image = WorkflowMediaInputs(setup.artifacts).collect(setup.resolved)
    with pytest.raises(WorkflowValidationError):
        build_native_memory(setup.resolved, (), media=image, media_mode=False)


@pytest.mark.parametrize("change", ["workspace", "cancel", "pause", "deadline", "manifest"])
def test_media_read_rechecks_execution_before_provider_creation(tmp_path: Path, change: str) -> None:
    setup = make_text(tmp_path, raw=PNG, content_type="image/png")
    setup.target = _target(setup)
    setup.dispatch(_executor(setup))
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

    media = WorkflowMediaInputs(ChangedDuringRead())
    worker = setup.worker([], guard=False)
    worker.workflow_memory_guard = WorkflowNativeMemoryRepository(setup.workflows,
        media_inputs=media, clock=setup.clock)
    factories = []
    original_factory = worker.provider_factory
    worker.provider_factory = lambda profile: (factories.append(profile), original_factory(profile))[1]
    result = worker.run_once(OWNER)
    assert factories == []
    assert result["state"] == "failed"


def test_media_attaches_to_workflow_trigger_after_earlier_conversation_turn(tmp_path: Path) -> None:
    setup = make_text(tmp_path, raw=PNG, content_type="image/png")
    setup.target = _target(setup)
    setup.platform.append_turn(OWNER, "native-chat", "earlier-message", "earlier-run",
        text="Earlier question", client_token="earlier-turn", config_snapshot={}, now=102)
    with setup.workflows.transaction() as connection:
        connection.execute("UPDATE runs SET state='succeeded' WHERE run_id='earlier-run'")
    binding = setup.dispatch(_executor(setup)).binding
    captured = []
    worker = setup.worker(captured, guard=False)
    media = WorkflowMediaInputs(setup.artifacts)
    worker.workflow_memory_guard = WorkflowNativeMemoryRepository(setup.workflows,
        media_inputs=media, clock=setup.clock)
    assert worker.run_once(OWNER)["state"] == "succeeded"
    earlier = next(message for message in captured[0] if message.get("message_id") == "earlier-message")
    current = next(message for message in captured[0] if message.get("message_id") == "wf-msg-" + binding.token)
    assert earlier["content"] == "Earlier question"
    assert current["content"][-1]["type"] == "image_url"


@pytest.mark.parametrize("content_type", ["image/png", "image/jpeg", "image/webp"])
def test_original_media_worker_reaches_openai_transport_without_persisting_image(tmp_path: Path, content_type: str) -> None:
    from test_platform_provider import Broker, FakeTransport, _response

    from hub.platform.providers.openai_compatible import ProviderFactory

    setup = make_text(tmp_path, raw=PNG, content_type=content_type)
    provider_config = {"endpoint": "https://vision.example.test", "max_retries": 0}
    setup.platform.upsert_model(OWNER, {"profile_id": "local", "provider": "openai_compatible",
        "model": "vision-test", "provider_config": provider_config, "secret_ref": "env://MEDIA_TEST"})
    config = json.loads(_target(setup).config_snapshot)
    config["provider_snapshot"].update(provider="openai_compatible", model="vision-test",
        provider_config=provider_config)
    setup.target = replace(_target(setup), config_snapshot=canonical_json(config))
    binding = setup.dispatch(_executor(setup)).binding
    transport = FakeTransport([_response({"choices": [{"message": {"role": "assistant", "content": "seen"},
        "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 1}})])
    worker = setup.worker([], guard=False)
    worker.provider_factory = ProviderFactory(secret_broker=Broker({"env://MEDIA_TEST": "test-key"}), transport=transport)
    worker.workflow_memory_guard = WorkflowNativeMemoryRepository(setup.workflows,
        media_inputs=WorkflowMediaInputs(setup.artifacts), clock=setup.clock)
    assert worker.run_once(OWNER)["state"] == "succeeded"
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == "https://vision.example.test/v1/chat/completions"
    body = json.loads(call["body"])
    user = next(message for message in body["messages"] if message["role"] == "user")
    assert user["content"] == [{"type": "text", "text": setup.resolved.snapshot.decode()},
        {"type": "image_url", "image_url": {"url": "data:" + content_type + ";base64," + base64.b64encode(PNG).decode()}}]
    assert body["model"] == "vision-test"
    with setup.workflows.transaction(write=False) as connection:
        saved = connection.execute("SELECT config_snapshot,runtime_checkpoint FROM runs WHERE run_id=?", (binding.run_id,)).fetchone()
        messages = connection.execute("SELECT content FROM messages").fetchall()
    encoded = base64.b64encode(PNG).decode()
    assert all(encoded not in value and "data:image/" not in value for value in saved)
    assert all(encoded not in row[0] and "data:image/" not in row[0] for row in messages)


def test_media_provider_pending_recovery_never_reopens_artifact_or_resends_request(tmp_path: Path) -> None:
    from test_workflow_native_text_inputs import ObservedArtifacts

    setup = make_text(tmp_path, raw=PNG, content_type="image/png")
    setup.target = _target(setup)
    observed = ObservedArtifacts(setup.artifacts)
    binding = setup.dispatch(_executor(setup, observed)).binding
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
        checkpoint = connection.execute("SELECT runtime_checkpoint FROM runs WHERE run_id=?", (binding.run_id,)).fetchone()[0]
        assert json.loads(checkpoint)["phase"] == "provider_pending"
        connection.execute("UPDATE runs SET lease_expires_at=0 WHERE run_id=?", (binding.run_id,))
    assert worker().run_once(OWNER)["state"] == "unknown"
    assert observed.opens == 2  # admission plus the interrupted attempt
    assert len(calls) == 1
    assert "data:image/" not in checkpoint


def test_media_flag_does_not_change_legacy_positional_remote_test_registration(tmp_path: Path) -> None:
    from hub.domain.workflow_contract_types import CatalogRef
    from hub.infrastructure.workflow_run_executor import RegisteredRunTarget

    setup = make_text(tmp_path)
    target = RegisteredRunTarget(OWNER, setup.target.workspace_id, "node-a", CatalogRef("fixed-tests", 1),
        setup.target.policy_ref, "native-chat", canonical_json({"execution_node_id": "node-a"}),
        None, CatalogRef("coding-delivery-tests", 1), None, None, False, False, True)
    assert target.remote_test is True
    assert target.inline_media_inputs is False


def test_media_manifest_requires_integer_size_before_opening_descriptor(tmp_path: Path) -> None:
    from test_workflow_native_text_inputs import ObservedArtifacts

    setup = make_text(tmp_path, raw=b"x", content_type="image/png")
    observed = ObservedArtifacts(setup.artifacts, metadata=lambda value: {**value, "size": True})
    with pytest.raises(WorkflowValidationError, match="media_input_forbidden"):
        WorkflowMediaInputs(observed).collect(setup.resolved)
    assert observed.opens == 0
