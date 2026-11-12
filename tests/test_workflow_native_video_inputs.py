"""Complete MP4 references through original Native Run authority and transport."""
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
from hub.infrastructure.workflow_native_memory_repository import (
    WorkflowNativeMemoryRepository,
)
from hub.infrastructure.workflow_run_executor import RunStepExecutor
from hub.platform.providers.openai_compatible import (
    OpenAICompatibleProvider,
    ProviderError,
)

# Two-frame16x16MP4 generated once with ffmpeg's testsrc2/mpeg4; tests need no codec runtime.
MP4 = base64.b64decode(
    'AAAAHGZ0eXBpc29tAAACAGlzb21pc28ybXA0MQAAA1ttb292AAAAbG12aGQAAAAAAAAAAAAAAAAAAAPoAAAD6AABAAABAAAAAAAA'
    'AAAAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAAChnRy'
    'YWsAAABcdGtoZAAAAAMAAAAAAAAAAAAAAAEAAAAAAAAD6AAAAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAA'
    'AAAAAAAAAEAAAAAAEAAAABAAAAAAACRlZHRzAAAAHGVsc3QAAAAAAAAAAQAAA+gAAAAAAAEAAAAAAf5tZGlhAAAAIG1kaGQAAAAA'
    'AAAAAAAAAAAAAEAAAABAAFXEAAAAAAAtaGRscgAAAAAAAAAAdmlkZQAAAAAAAAAAAAAAAFZpZGVvSGFuZGxlcgAAAAGpbWluZgAA'
    'ABR2bWhkAAAAAQAAAAAAAAAAAAAAJGRpbmYAAAAcZHJlZgAAAAAAAAABAAAADHVybCAAAAABAAABaXN0YmwAAADpc3RzZAAAAAAA'
    'AAABAAAA2W1wNHYAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAEAAQAEgAAABIAAAAAAAAAAESTGF2YzYzLjEuMTAyIG1wZWc0AAAA'
    'AAAAAAAAAAAAAAAY//8AAABfZXNkcwAAAAADgICATgABAASAgIBAIBEAAAAAAw1AAAAGQAWAgIAuAAABsAEAAAG1iRMAAAEAAAAB'
    'IADEjYgAFQCEAhRjAAABskxhdmM2My4xLjEwMgaAgIABAgAAABBwYXNwAAAAAQAAAAEAAAAUYnRydAAAAAAAAw1AAAAGQAAAABhz'
    'dHRzAAAAAAAAAAEAAAACAAAgAAAAABRzdHNzAAAAAAAAAAEAAAABAAAAHHN0c2MAAAAAAAAAAQAAAAEAAAACAAAAAQAAABxzdHN6'
    'AAAAAAAAAAAAAAACAAAAjwAAADkAAAAUc3RjbwAAAAAAAAABAAADhwAAAGF1ZHRhAAAAWW1ldGEAAAAAAAAAIWhkbHIAAAAAAAAA'
    'AG1kaXJhcHBsAAAAAAAAAAAAAAAALGlsc3QAAAAkqXRvbwAAABxkYXRhAAAAAQAAAABMYXZmNjMuMS4xMDIAAAAIZnJlZQAAANBt'
    'ZGF0AAABswAQBwAAAbYWFNhfBWY1mtoaoEW223kkzF7SSOpOCqZY7q+KSzgGZpb2B4WLfUrDbneLkRxX3hd8bZgfy/Rll+t60k6M'
    'idfZoIoIqnrQFgU/IoHIfDgtUBwOUIFurBb+EZVk93l3M0rUSKd8VKEWh79GN7oKjSMNWGbxrZnO1VpQi/G+9wbahWXArHsAAAG2'
    'XwohXx8rEgfCXML/ajsKqSVoz+j8Sy+QFCrv8alV/L1F+2oU4tO5NIAKQRpzybuiN3JqLTo='
)


def video_target(setup, *, video_input: bool = True, image_input: bool = False):
    config = json.loads(setup.target.config_snapshot)
    config["provider_snapshot"]["capabilities"].update(
        image_input=image_input, audio_input=False, video_input=video_input)
    return replace(setup.target, config_snapshot=canonical_json(config),
        inline_text_inputs=False, inline_media_inputs=True)


def video_setup(tmp_path: Path, *, raw: bytes = MP4, content_type: str = "video/mp4", count: int = 1):
    setup = make_text(tmp_path, raw=raw, content_type=content_type, count=count)
    setup.target = video_target(setup)
    return setup


def video_part(raw: bytes = MP4):
    return {"type": "video_url", "video_url": {
        "url": "data:video/mp4;base64," + base64.b64encode(raw).decode("ascii")}}


def test_native_video_freezes_only_metadata_and_sends_complete_video(tmp_path: Path) -> None:
    setup = video_setup(tmp_path)
    binding = setup.dispatch(RunStepExecutor(setup.workflows, setup.target, clock=setup.clock,
        media_inputs=WorkflowMediaInputs(setup.artifacts))).binding
    run = setup.platform.get_run(OWNER, binding.run_id)
    reference = setup.resolved.contract.input_refs[0].reference
    assert run["config_snapshot"]["workflow_memory"] == {
        "schema_version": 7, "resolved_digest": setup.resolved.digest, "inputs": [],
        "video_inputs": [{"ref_key": "input-0", "artifact_id": reference.artifact_id,
            "sha256": hashlib.sha256(MP4).hexdigest(), "size": len(MP4), "content_type": "video/mp4"}],
    }
    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == 15
    transport = FakeTransport([_response({"choices": [{"message": {"content": "whole clip described"}}]})])
    provider = OpenAICompatibleProvider(model="video-test", endpoint="http://localhost", api_key="secret",
        transport=transport)
    worker = setup.worker([], guard=False)
    worker.provider_factory = lambda _profile: provider
    worker.workflow_memory_guard = WorkflowNativeMemoryRepository(setup.workflows,
        media_inputs=WorkflowMediaInputs(setup.artifacts), clock=setup.clock)
    assert worker.run_once(OWNER)["state"] == "succeeded"
    payload = json.loads(transport.calls[0]["body"])
    user = next(item for item in payload["messages"] if item["role"] == "user")
    assert user["content"][-1] == video_part()
    assert base64.b64decode(user["content"][-1]["video_url"]["url"].split(",", 1)[1]) == MP4
    with setup.workflows.transaction(write=False) as connection:
        checkpoint = connection.execute("SELECT runtime_checkpoint FROM runs WHERE run_id=?", (binding.run_id,)).fetchone()[0]
    assert base64.b64encode(MP4).decode() not in checkpoint
    assert "video_url" not in checkpoint
    assert base64.b64encode(MP4).decode() not in json.dumps(run["config_snapshot"])


def test_provider_preserves_complete_video_and_text_order() -> None:
    transport = FakeTransport([_response({"choices": [{"message": {"content": "ok"}}]})])
    provider = OpenAICompatibleProvider(model="video-test", endpoint="http://localhost", api_key="secret",
        transport=transport)
    content = [{"type": "text", "text": "before"}, video_part(), {"type": "text", "text": "after"}]
    assert provider.complete([{"role": "user", "content": content}], []).text == "ok"
    assert json.loads(transport.calls[0]["body"])["messages"][0]["content"] == content


def test_video_domain_roundtrip_uses_schema_seven_and_exact_reference(tmp_path: Path) -> None:
    from hub.domain.workflow_media_inputs import WorkflowVideoInput
    from hub.domain.workflow_native_memory import (
        build_native_memory,
        parse_native_memory,
    )

    setup = make_text(tmp_path, raw=MP4, content_type="video/mp4")
    reference = setup.resolved.contract.input_refs[0].reference
    video = WorkflowVideoInput("input-0", reference, "video/mp4")
    snapshot = build_native_memory(setup.resolved, (), video=(video,), video_mode=True)
    assert json.loads(snapshot.snapshot)["schema_version"] == 7
    assert parse_native_memory(json.loads(snapshot.snapshot), setup.resolved) == snapshot
    assert snapshot.video == (video,)


def test_ready_video_checkpoint_rereads_artifact_after_restart(tmp_path: Path) -> None:
    from hub.platform.providers.base import ModelResponse

    setup = make_text(tmp_path, raw=MP4, content_type="video/mp4")
    setup.target = video_target(setup)
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
        == "video_url" for attempt in calls)
    assert reads.count("native-input-a") == 2
    with setup.workflows.transaction(write=False) as connection:
        checkpoint = connection.execute("SELECT runtime_checkpoint FROM runs WHERE run_id=?",
            (binding.run_id,)).fetchone()[0]
    assert base64.b64encode(MP4).decode("ascii") not in checkpoint


def test_video_provider_pending_recovery_never_reopens_or_resends(tmp_path: Path) -> None:
    from test_workflow_native_text_inputs import ObservedArtifacts

    setup = make_text(tmp_path, raw=MP4, content_type="video/mp4")
    setup.target = video_target(setup)
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
    assert base64.b64encode(MP4).decode("ascii") not in checkpoint


@pytest.mark.parametrize("change", ["workspace", "cancel", "pause", "deadline", "manifest"])
def test_video_read_rechecks_current_execution_before_provider_creation(tmp_path: Path, change: str) -> None:
    setup = make_text(tmp_path, raw=MP4, content_type="video/mp4")
    setup.target = video_target(setup)
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


def test_video_reference_is_required_by_history_and_reader_fifteen_gate(tmp_path: Path) -> None:
    from hub.infrastructure.workflow_native_memory_repository import (
        validate_native_memory_runs,
    )
    from hub.infrastructure.workflow_repository import WorkflowRepositoryError

    setup = make_text(tmp_path, raw=MP4, content_type="video/mp4")
    setup.target = video_target(setup)
    setup.dispatch(RunStepExecutor(setup.workflows, setup.target,
        clock=setup.clock, media_inputs=WorkflowMediaInputs(setup.artifacts)))
    with setup.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=14")
    with pytest.raises(WorkflowRepositoryError):
        setup.workflows.init()
    with setup.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=15")
        connection.execute("DELETE FROM workflow_artifact_refs WHERE workflow_id='workflow-a'")
    with (setup.workflows.transaction(write=False) as connection,
          pytest.raises(WorkflowValidationError, match="native_memory_reference_mismatch")):
        validate_native_memory_runs(connection)


@pytest.mark.parametrize("encrypted", [False, True])
def test_video_backup_restore_preserves_original_binding_metadata_and_clip(tmp_path: Path, encrypted: bool) -> None:
    from hub.domain.workflow_native_memory import parse_native_memory
    from hub.infrastructure.platform_backup import PlatformBackupService
    from hub.infrastructure.platform_db import PlatformRepository
    from hub.infrastructure.workflow_repository import WorkflowRepository
    from hub.platform.artifacts import ArtifactStore

    setup = video_setup(tmp_path)
    binding = setup.dispatch(RunStepExecutor(setup.workflows, setup.target, clock=setup.clock,
        media_inputs=WorkflowMediaInputs(setup.artifacts))).binding
    config = setup.platform.get_run(OWNER, binding.run_id)["config_snapshot"]
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    service = PlatformBackupService()
    if encrypted:
        summary = service.create_encrypted_backup(setup.workflows.db_path, setup.artifacts.root,
            backup, b"k" * 32, "video-test")["source"]
        service.restore_encrypted_backup(backup, restored, b"k" * 32)
    else:
        summary = service.create_backup(setup.workflows.db_path, setup.artifacts.root, backup)
        service.restore_backup(backup, restored)
    assert summary["workflow"] == {"schema_version": 1, "minimum_reader_version": 15}
    repository = WorkflowRepository(restored / "platform.db")
    repository.init()
    inputs = WorkflowMediaInputs(ArtifactStore(restored / "artifacts"))
    recovered = RunStepExecutor(repository, setup.target, clock=setup.clock, media_inputs=inputs)
    assert asyncio.run(recovered.lookup(binding.token, setup.resolved.digest, setup.resolved)) == binding
    assert PlatformRepository(repository.db_path).get_run(OWNER, binding.run_id)["config_snapshot"] == config
    snapshot = parse_native_memory(config["workflow_memory"], setup.resolved)
    assert inputs.load_video(setup.resolved, snapshot.video, capabilities={"video_input": True}) == (video_part(),)
    with repository.transaction(write=False) as connection:
        assert tuple(connection.execute("SELECT admission_mode,minimum_reader_version FROM workflow_schema_meta").fetchone()) == ("frozen", 15)


@pytest.mark.parametrize("change", ["capability", "mime", "empty", "item_size", "total_size", "count"])
def test_video_rejected_admission_creates_no_run_message_or_reader_upgrade(tmp_path: Path, change: str) -> None:
    size = 2 * 1024 * 1024
    setup = make_text(tmp_path, raw=b"x" * (size + 1) if change == "item_size" else
        b"x" * size if change == "total_size" else b"" if change == "empty" else MP4,
        content_type="video/webm" if change == "mime" else "video/mp4",
        count=5 if change == "count" else 3 if change == "total_size" else 1)
    setup.target = video_target(setup, video_input=change != "capability", image_input=change == "capability")
    with setup.workflows.transaction(write=False) as connection:
        old_reader = connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0]
    with pytest.raises(WorkflowDispatchError):
        setup.dispatch(RunStepExecutor(setup.workflows, setup.target, clock=setup.clock,
            media_inputs=WorkflowMediaInputs(setup.artifacts)))
    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == old_reader


def test_repeated_video_positions_read_once_per_attempt_and_preserve_order(tmp_path: Path) -> None:
    from test_workflow_native_text_inputs import ObservedArtifacts

    setup = video_setup(tmp_path, count=4)
    observed = ObservedArtifacts(setup.artifacts)
    inputs = WorkflowMediaInputs(observed)
    binding = setup.dispatch(RunStepExecutor(setup.workflows, setup.target, clock=setup.clock,
        media_inputs=inputs)).binding
    assert observed.opens == 1
    transport = FakeTransport([_response({"choices": [{"message": {"content": "ok"}}]})])
    worker = setup.worker([], guard=False)
    worker.provider_factory = lambda _profile: OpenAICompatibleProvider(model="video-test", endpoint="http://localhost",
        api_key="secret", transport=transport)
    worker.workflow_memory_guard = WorkflowNativeMemoryRepository(setup.workflows, media_inputs=inputs, clock=setup.clock)
    assert worker.run_once(OWNER)["state"] == "succeeded"
    assert observed.opens == 2
    user = next(m for m in json.loads(transport.calls[0]["body"])["messages"] if m["role"] == "user")
    assert user["content"][-4:] == [video_part()] * 4
    metadata = setup.platform.get_run(OWNER, binding.run_id)["config_snapshot"]["workflow_memory"]["video_inputs"]
    assert [row["ref_key"] for row in metadata] == [f"input-{i}" for i in range(4)]


@pytest.mark.parametrize("part", [
    {"type": "video_url", "video_url": {"url": "https://video.example.test/clip.mp4"}},
    {"type": "video_url", "video_url": {"url": "file:///private/clip.mp4"}},
    {"type": "video_url", "video_url": {"url": "data:video/webm;base64,YQ=="}},
    {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,"}},
    {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,!!!!"}},
    {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,YQ==\n"}},
    {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,YR=="}},
    {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,YQ==", "detail": "low"}},
    {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,YQ=="}, "extra": 1},
    {"type": "video_url", "video_url": {"url": False}},
    {"type": "video_url", "video_url": {}},
])
def test_malformed_video_parts_fail_before_provider_transport(part) -> None:
    transport = FakeTransport([])
    provider = OpenAICompatibleProvider(model="video-test", endpoint="http://localhost", api_key="secret", transport=transport)
    with pytest.raises(ProviderError, match="provider_message_invalid"):
        provider.complete([{"role": "user", "content": [part]}], [])
    assert transport.calls == []


@pytest.mark.parametrize("raws", [[MP4] * 5, [b"x" * (2 * 1024 * 1024 + 1)],
    [b"x" * (2 * 1024 * 1024), b"x" * (2 * 1024 * 1024), b"x"]])
def test_video_wire_budgets_apply_across_messages_before_transport(raws) -> None:
    transport = FakeTransport([])
    provider = OpenAICompatibleProvider(model="video-test", endpoint="http://localhost", api_key="secret", transport=transport)
    with pytest.raises(ProviderError, match="provider_message_invalid"):
        provider.complete([{"role": "user", "content": [video_part(raw)]} for raw in raws], [])
    assert transport.calls == []


@pytest.mark.parametrize("field,value", [("schema_version", True), ("schema_version", 8),
    ("video_inputs", []), ("extra", "data:video/mp4;base64,YQ==")])
def test_video_snapshot_rejects_invalid_versions_empty_refs_and_extra_bytes(tmp_path: Path, field, value) -> None:
    from hub.domain.workflow_native_memory import parse_native_memory

    setup = video_setup(tmp_path)
    binding = setup.dispatch(RunStepExecutor(setup.workflows, setup.target, clock=setup.clock,
        media_inputs=WorkflowMediaInputs(setup.artifacts))).binding
    snapshot = setup.platform.get_run(OWNER, binding.run_id)["config_snapshot"]["workflow_memory"]
    snapshot[field] = value
    with pytest.raises(WorkflowValidationError):
        parse_native_memory(snapshot, setup.resolved)


def test_native_video_worker_posts_complete_mp4_over_actual_http(tmp_path: Path) -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    payloads = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payloads.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            raw = json.dumps({"choices": [{"message": {"content": "received full video"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        setup = video_setup(tmp_path)
        setup.dispatch(RunStepExecutor(setup.workflows, setup.target, clock=setup.clock,
            media_inputs=WorkflowMediaInputs(setup.artifacts)))
        worker = setup.worker([], guard=False)
        worker.provider_factory = lambda _profile: OpenAICompatibleProvider(model="video-test",
            endpoint=f"http://127.0.0.1:{server.server_port}/v1/chat/completions", api_key="test-key", allow_network=True)
        worker.workflow_memory_guard = WorkflowNativeMemoryRepository(setup.workflows,
            media_inputs=WorkflowMediaInputs(setup.artifacts), clock=setup.clock)
        assert worker.run_once(OWNER)["state"] == "succeeded"
        assert len(payloads) == 1 and payloads[0][0] == "/v1/chat/completions"
        user = next(m for m in payloads[0][1]["messages"] if m["role"] == "user")
        assert user["content"][-1] == video_part()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


def test_video_request_total_serialized_budget_fails_before_transport() -> None:
    transport = FakeTransport([])
    provider = OpenAICompatibleProvider(model="video-test", endpoint="http://localhost", api_key="secret", transport=transport)
    messages = [{"role": "user", "content": [video_part(b"v" * (2 * 1024 * 1024))] * 2}]
    messages.extend({"role": "user", "content": "x" * (128 * 1024)} for _ in range(30))
    with pytest.raises(ProviderError, match="request_too_large"):
        provider.complete(messages, [])
    assert transport.calls == []


@pytest.mark.parametrize("change", ["owner", "workspace", "hash", "size", "mime", "stream_tail", "stream_short"])
def test_video_authorization_and_complete_stream_reject_mismatches(tmp_path: Path, change: str) -> None:
    import io

    setup = video_setup(tmp_path)
    opens = []

    class ChangedArtifacts:
        def get(self, *args):
            value = setup.artifacts.get(*args)
            field, replacement = {"owner": ("owner_id", "other@example.test"), "workspace": ("workspace_id", "other"),
                "hash": ("sha256", "0" * 64), "size": ("size", len(MP4) + 1), "mime": ("content_type", "image/png")}.get(change, (None, None))
            return {**value, field: replacement} if field else value

        def open_verified(self, *args):
            opens.append(args[-1])
            return io.BytesIO(MP4 + b"extra" if change == "stream_tail" else MP4[:-1])

    with pytest.raises(WorkflowValidationError):
        WorkflowMediaInputs(ChangedArtifacts()).collect(setup.resolved, capabilities={"video_input": True})
    assert len(opens) == (1 if change.startswith("stream_") else 0)


@pytest.mark.parametrize("reader", [1, 6, 13, 14])
def test_video_older_reader_rejects_history_even_without_dispatch_gate(tmp_path: Path, monkeypatch, reader: int) -> None:
    from hub import web
    from hub.infrastructure import workflow_schema
    from hub.infrastructure.platform_backup import BackupError, PlatformBackupService
    from hub.infrastructure.workflow_repository import WorkflowRepositoryError

    setup = video_setup(tmp_path)
    setup.dispatch(RunStepExecutor(setup.workflows, setup.target, clock=setup.clock,
        media_inputs=WorkflowMediaInputs(setup.artifacts)))
    monkeypatch.setattr(workflow_schema, "READER_VERSION", reader)
    with pytest.raises(WorkflowRepositoryError):
        setup.workflows.init()
    with pytest.raises(BackupError):
        PlatformBackupService().create_backup(setup.workflows.db_path, setup.artifacts.root, tmp_path / "backup")
    monkeypatch.setattr(web, "FLEET_HOME", tmp_path)
    with pytest.raises(WorkflowRepositoryError):
        web.make_app(require_token=False, platform_enabled=True, platform_db=setup.workflows.db_path,
            platform_workflows_enabled=False, serve_frontend=False)


@pytest.mark.parametrize("damage", ["mime", "flag", "reference", "missing_run"])
def test_video_corrupt_history_rejects_startup_and_backup(tmp_path: Path, damage: str) -> None:
    import sqlite3
    from contextlib import closing

    from hub.composition.workflows import assert_workflow_visibility
    from hub.infrastructure.platform_backup import BackupError, PlatformBackupService
    from hub.infrastructure.workflow_repository import WorkflowRepositoryError

    setup = video_setup(tmp_path)
    binding = setup.dispatch(RunStepExecutor(setup.workflows, setup.target, clock=setup.clock,
        media_inputs=WorkflowMediaInputs(setup.artifacts))).binding
    config = setup.platform.get_run(OWNER, binding.run_id)["config_snapshot"]
    with closing(sqlite3.connect(setup.workflows.db_path)) as connection:
        if damage == "mime":
            config["workflow_memory"]["video_inputs"][0]["content_type"] = "image/png"
        elif damage == "flag":
            config.pop("workflow_media_mode")
        elif damage == "reference":
            connection.execute("UPDATE workflow_artifact_refs SET sha256=? WHERE workflow_id='workflow-a'", ("0" * 64,))
        else:
            connection.execute("UPDATE runs SET run_id='missing-video-run' WHERE run_id=?", (binding.run_id,))
        if damage in {"mime", "flag"}:
            connection.execute("UPDATE runs SET config_snapshot=? WHERE run_id=?", (canonical_json(config).decode(), binding.run_id))
        connection.commit()
    with pytest.raises(WorkflowRepositoryError):
        setup.workflows.init()
    with pytest.raises(WorkflowRepositoryError):
        assert_workflow_visibility(setup.workflows.db_path, enabled=False)
    with pytest.raises(BackupError):
        PlatformBackupService().create_backup(setup.workflows.db_path, setup.artifacts.root, tmp_path / "backup")

