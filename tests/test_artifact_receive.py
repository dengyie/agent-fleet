"""Node receive boundaries use real files and an independent durable journal."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import os
from pathlib import Path
import tracemalloc
from typing import Any, Iterator

import pytest

from fleet_core.control import generate_ed25519_keypair
from fleet_core.platform_command import PlatformCommand
from tools.platform.artifact_transfer import ArtifactReceiver, ArtifactReceiveError, InputDirectory


class ContentTransport:
    def __init__(self, data: bytes, *, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.data, self.status = data, status
        self.headers = headers or {"Content-Length": str(len(data))}
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.read_sizes: list[int] = []
        self.closed = False

    @contextmanager
    def get_stream(self, url: str, headers: dict[str, str], **kwargs: Any) -> Iterator[Any]:
        self.requests.append((url, headers))
        parent = self
        class Response(io.BytesIO):
            status = parent.status
            headers = parent.headers
            def read(self, size: int = -1) -> bytes:
                assert 0 < size <= 65536
                parent.read_sizes.append(size)
                return super().read(size)
        try:
            with Response(self.data) as response:
                yield response
        finally:
            self.closed = True


@pytest.fixture
def receive(tmp_path: Path) -> Any:
    private, public = generate_ed25519_keypair()
    root = tmp_path / "inputs"
    root.mkdir(mode=0o700)
    transport = ContentTransport(b"source archive bytes")
    def command(data: bytes = transport.data, **changes: Any) -> dict[str, Any]:
        arguments = {"artifact_id": "artifact-a", "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data), "input_alias": "source", "suffix": ".tar", "workflow_id": "wf-a",
            "step_key": "review", "generation": 1, "ref_key": "bundle", "input_digest": "a" * 64}
        arguments.update(changes.pop("arguments", {}))
        params = dict(command_id="receive-" + "a" * 64, target_node="node-b", action="artifact.receive",
            resource_id="workspace", grant_id="a" * 64, arguments=arguments, expires_at=1000,
            retry_class="idempotent_with_key")
        params.update(changes)
        return PlatformCommand.create(**params).signed(private).as_dict()
    def receiver(**changes: Any) -> ArtifactReceiver:
        params = dict(node_id="node-b", credential="node-b:" + "n" * 40, worker_id="worker-b",
            hub_url="https://hub.test", public_key=public, inputs={"source": InputDirectory(root, frozenset({".tar"}))},
            journal_path=tmp_path / "receive.db", transport=transport, clock=lambda: 100.)
        params.update(changes)
        return ArtifactReceiver(**params)
    return type("ReceiveFixture", (), dict(root=root, transport=transport, command=staticmethod(command), receiver=staticmethod(receiver)))()


def test_stream_receive_is_verified_atomic_and_durable_after_restart(receive: Any) -> None:
    node = receive.receiver()
    receipt = node.receive(receive.command())
    assert set(receipt) == {"grant_id", "artifact_id", "sha256", "size", "input_alias"}
    assert receipt["input_alias"] == "source"
    target = receive.root / ("a" * 64 + ".tar")
    assert target.read_bytes() == receive.transport.data
    assert receive.transport.closed
    assert max(receive.transport.read_sizes) <= 65536
    assert receive.transport.requests[0][0] == "https://hub.test/api/platform/v1/node/artifact-inputs/" + "a" * 64 + "/content"
    assert "?" not in receive.transport.requests[0][0]
    assert receive.receiver().receive(receive.command()) == receipt
    assert len(receive.transport.requests) == 1
    assert receive.receiver().pending_receipts() == ((receive.command()["command_id"], receipt),)
    node.acknowledge(receive.command()["command_id"], receipt)
    assert receive.receiver().pending_receipts() == ()


@pytest.mark.parametrize("change", [
    {"target_node": "node-c"}, {"grant_id": "b" * 64},
    {"arguments": {"download_url": "https://evil.test/file"}},
    {"arguments": {"input_alias": "../outside"}}, {"arguments": {"input_alias": "/tmp"}},
    {"arguments": {"suffix": ".exe"}}, {"arguments": {"size": True}},
    {"expires_at": 100}, {"arguments": {"sha256": "x" * 64}},
])
def test_invalid_signed_receive_does_not_touch_network(receive: Any, change: Any) -> None:
    with pytest.raises(ArtifactReceiveError):
        receive.receiver().receive(receive.command(**change))
    assert receive.transport.requests == []
    assert list(receive.root.iterdir()) == []


def test_unsigned_and_tampered_commands_are_rejected(receive: Any) -> None:
    command = receive.command()
    command["signature"] = None
    with pytest.raises(ArtifactReceiveError, match="invalid_signature"):
        receive.receiver().receive(command)
    command = receive.command()
    command["arguments"]["size"] += 1
    with pytest.raises(ArtifactReceiveError):
        receive.receiver().receive(command)


@pytest.mark.parametrize("status,headers", [(302, {"Location": "https://other.test/file"}),
    (200, {"Content-Length": "999"}), (200, {"Content-Length": "20", "Content-Encoding": "gzip"}),
    (404, {}), (200, {})])
def test_redirects_encoded_and_invalid_length_responses_fail_closed(receive: Any, status: int, headers: Any) -> None:
    receive.transport.status, receive.transport.headers = status, headers
    with pytest.raises(ArtifactReceiveError):
        receive.receiver().receive(receive.command())
    assert not (receive.root / ("a" * 64 + ".tar")).exists()
    assert receive.transport.closed


def test_hash_failure_and_short_read_never_publish(receive: Any) -> None:
    command = receive.command()
    receive.transport.data = b"X" * len(receive.transport.data)
    with pytest.raises(ArtifactReceiveError, match="artifact_hash_mismatch"):
        receive.receiver().receive(command)
    receive.transport.data = b"short"
    with pytest.raises(ArtifactReceiveError, match="invalid_content_length"):
        receive.receiver().receive(command)
    assert not (receive.root / ("a" * 64 + ".tar")).exists()


def test_existing_target_and_symlink_are_never_overwritten(receive: Any, tmp_path: Path) -> None:
    target = receive.root / ("a" * 64 + ".tar")
    target.write_bytes(b"existing")
    with pytest.raises(ArtifactReceiveError, match="input_target_conflict"):
        receive.receiver().receive(receive.command())
    assert target.read_bytes() == b"existing"


def test_symlink_ancestor_is_rejected(receive: Any, tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(receive.root, target_is_directory=True)
    with pytest.raises(ArtifactReceiveError, match="unsafe_input_path"):
        receive.receiver(inputs={"source": InputDirectory(alias, frozenset({".tar"}))}).receive(receive.command())
    assert receive.transport.requests == []


def test_missing_windows_handle_guards_do_not_advertise_capability(receive: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr("tools.platform.artifact_transfer.receive_supported", lambda: False)
    node = receive.receiver()
    assert not node.available
    with pytest.raises(ArtifactReceiveError, match="capability_unavailable"):
        node.receive(receive.command())
    assert receive.transport.requests == []


def test_receipt_corruption_or_file_tampering_is_not_replayed(receive: Any) -> None:
    node = receive.receiver()
    node.receive(receive.command())
    target = receive.root / ("a" * 64 + ".tar")
    target.chmod(0o600)
    target.write_bytes(b"changed")
    with pytest.raises(ArtifactReceiveError, match="artifact_hash_mismatch"):
        receive.receiver().receive(receive.command())
    assert len(receive.transport.requests) == 1


def test_receive_stream_has_bounded_memory(receive: Any) -> None:
    data = b"x" * (12 * 1024 * 1024)
    receive.transport.data, receive.transport.headers = data, {"Content-Length": str(len(data))}
    tracemalloc.start()
    try:
        receive.receiver().receive(receive.command(data))
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * 1024 * 1024


def test_explicit_cancellation_and_deadline_leave_no_published_file(receive: Any) -> None:
    with pytest.raises(ArtifactReceiveError, match="receive_cancelled"):
        receive.receiver(should_cancel=lambda: True).receive(receive.command())
    assert receive.transport.requests == []
    clock = iter([100., 100., 100., 1001., 1001., 1001.])
    with pytest.raises(ArtifactReceiveError, match="receive_expired"):
        receive.receiver(clock=lambda: next(clock, 1001.)).receive(receive.command())
    assert not (receive.root / ("a" * 64 + ".tar")).exists()


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "fifo"])
def test_unsafe_staging_entries_are_not_followed(receive: Any, tmp_path: Path, attack: str) -> None:
    outside = tmp_path / "outside"
    outside.write_bytes(b"preserve outside")
    stage = receive.root / ("." + "a" * 64 + ".part")
    if attack == "symlink":
        stage.symlink_to(outside)
    elif attack == "hardlink":
        os.link(outside, stage)
    else:
        os.mkfifo(stage)
    with pytest.raises(ArtifactReceiveError):
        receive.receiver().receive(receive.command())
    assert outside.read_bytes() == b"preserve outside"
    assert not (receive.root / ("a" * 64 + ".tar")).exists()


def test_directory_swap_during_download_cannot_escape_pinned_root(receive: Any, tmp_path: Path) -> None:
    original = receive.transport.get_stream
    moved = tmp_path / "moved-inputs"
    outside = tmp_path / "outside"
    outside.mkdir()
    @contextmanager
    def swap(*args: Any, **kwargs: Any) -> Iterator[Any]:
        receive.root.rename(moved)
        receive.root.symlink_to(outside, target_is_directory=True)
        with original(*args, **kwargs) as response:
            yield response
    receive.transport.get_stream = swap
    with pytest.raises(ArtifactReceiveError, match="unsafe_input_path"):
        receive.receiver().receive(receive.command())
    assert list(outside.iterdir()) == []
    assert not (moved / ("a" * 64 + ".tar")).exists()


def test_concurrent_receivers_use_nonblocking_per_grant_lock(receive: Any) -> None:
    import threading
    entered, release = threading.Event(), threading.Event()
    original = receive.transport.get_stream
    errors: list[BaseException] = []
    @contextmanager
    def pause(*args: Any, **kwargs: Any) -> Iterator[Any]:
        entered.set()
        assert release.wait(5)
        with original(*args, **kwargs) as response:
            yield response
    receive.transport.get_stream = pause
    first, second = receive.receiver(), receive.receiver()
    def worker() -> None:
        try:
            first.receive(receive.command())
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(ArtifactReceiveError, match="receive_busy"):
            second.receive(receive.command())
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive() and errors == []
    assert len(receive.transport.requests) == 1


def _crash_receive(params: dict[str, Any], command: dict[str, Any], data: bytes, window: str) -> None:
    receiver = ArtifactReceiver(**params, transport=ContentTransport(data))
    original_publish = receiver._publish
    original_download = receiver._download
    original_ready = receiver.journal.mark
    def publish(*args: Any, **kwargs: Any) -> None:
        if window == "before_rename":
            os._exit(73)
        original_publish(*args, **kwargs)
        if window == "after_rename":
            os._exit(73)
    def download(*args: Any, **kwargs: Any) -> None:
        if window == "partial":
            root_fd, request, record, _deadline = args
            with os.fdopen(receiver._stage(root_fd, request, record), "wb") as output:
                output.write(b"partial")
                output.flush()
                os.fsync(output.fileno())
            os._exit(73)
        original_download(*args, **kwargs)
    def mark(command_id: str, state: str, *, now: float) -> None:
        original_ready(command_id, state, now=now)
        if window == "after_ready" and state == "ready":
            os._exit(73)
    receiver._publish, receiver._download, receiver.journal.mark = publish, download, mark
    receiver.receive(command)
    os._exit(74)


@pytest.mark.parametrize("window", ["partial", "before_rename", "after_rename", "after_ready"])
def test_process_crash_recovers_original_target_and_receipt(receive: Any, window: str) -> None:
    import multiprocessing
    receiver = receive.receiver()
    params = dict(node_id=receiver.node_id, credential=receiver.credential, worker_id=receiver.worker_id,
        hub_url=receiver.origin, public_key=receiver.public_key, inputs=receiver.inputs,
        journal_path=receiver.journal.path)
    command = receive.command(expires_at=9_999_999_999)
    child = multiprocessing.get_context("spawn").Process(target=_crash_receive,
        args=(params, command, receive.transport.data, window))
    child.start()
    child.join(timeout=15)
    assert not child.is_alive() and child.exitcode == 73
    assert receiver.recover() == ()
    pending = receiver.pending_receipts()
    if window == "after_ready":
        assert pending == ((command["command_id"], {"grant_id": "a" * 64,
            "artifact_id": "artifact-a", "sha256": hashlib.sha256(receive.transport.data).hexdigest(),
            "size": len(receive.transport.data), "input_alias": "source"}),)
    else:
        assert len(pending) == 1
    assert (receive.root / ("a" * 64 + ".tar")).read_bytes() == receive.transport.data
    assert len(receive.transport.requests) == (1 if window == "partial" else 0)
    assert [p.name for p in receive.root.iterdir() if not p.name.startswith(".")] == ["a" * 64 + ".tar"]


@pytest.mark.parametrize("corruption", ["file", "command", "receipt"])
def test_tampered_ready_outbox_requires_reconciliation(receive: Any, corruption: str) -> None:
    node = receive.receiver()
    node.receive(receive.command())
    if corruption == "file":
        (receive.root / ("a" * 64 + ".tar")).write_bytes(b"tampered")
    else:
        with node.journal.transaction() as connection:
            connection.execute("UPDATE artifact_receives SET " + corruption + "='null'")
    with pytest.raises(ArtifactReceiveError, match="receive_reconciliation_required"):
        receive.receiver().pending_receipts()
    assert node.journal.pending() == ()


def test_two_node_receive_slots_are_shared_across_receiver_instances(receive: Any) -> None:
    import threading
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    errors: list[BaseException] = []
    original = receive.transport.get_stream
    @contextmanager
    def pause(*args: Any, **kwargs: Any) -> Iterator[Any]:
        command_id = args[1]["X-Platform-Command-ID"]
        entered[0 if command_id.endswith("a" * 64) else 1].set()
        assert release.wait(5)
        with original(*args, **kwargs) as response:
            yield response
    receive.transport.get_stream = pause
    def worker(grant: str) -> None:
        try:
            receive.receiver().receive(receive.command(grant_id=grant, command_id="receive-" + grant))
        except BaseException as exc:
            errors.append(exc)
    threads = [threading.Thread(target=worker, args=(char * 64,)) for char in ("a", "b")]
    for thread in threads:
        thread.start()
    try:
        assert all(event.wait(5) for event in entered)
        with pytest.raises(ArtifactReceiveError, match="receive_concurrency_limit"):
            receive.receiver().receive(receive.command(grant_id="c" * 64, command_id="receive-" + "c" * 64))
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5)
    assert errors == [] and not any(thread.is_alive() for thread in threads)
    assert len(receive.receiver().pending_receipts()) == 2


def test_final_download_attempt_does_not_disable_publication_recovery(receive: Any, monkeypatch: Any) -> None:
    node = receive.receiver()
    def crash(*args: Any, **kwargs: Any) -> None:
        raise SystemExit("crash after verification")
    monkeypatch.setattr(node, "_publish", crash)
    with pytest.raises(SystemExit):
        node.receive(receive.command())
    with node.journal.transaction() as connection:
        connection.execute("UPDATE artifact_receives SET attempts=3")
    restarted = receive.receiver()
    assert restarted.recover() == ()
    assert len(restarted.pending_receipts()) == 1
    assert len(receive.transport.requests) == 1


@pytest.mark.parametrize("artifact_id", ["_legacy-artifact", "-legacy-artifact"])
def test_legacy_urlsafe_artifact_ids_remain_valid(receive: Any, artifact_id: str) -> None:
    receipt = receive.receiver().receive(receive.command(arguments={"artifact_id": artifact_id}))
    assert receipt["artifact_id"] == artifact_id


@pytest.mark.parametrize("encoded", ["[]", "null", "17", '"invalid"'])
def test_corrupt_recovery_envelope_is_typed_and_never_downloads(receive: Any, encoded: str) -> None:
    from hub.domain.artifact_receive import ReceiveRequest
    node = receive.receiver()
    command = receive.command()
    request = ReceiveRequest.parse(command, node_id=node.node_id, public_key=node.public_key, now=100, max_bytes=node.max_bytes)
    info = receive.root.stat()
    node.journal.begin(request, command=command, device=info.st_dev, inode=info.st_ino, now=100)
    with node.journal.transaction() as connection:
        connection.execute("UPDATE artifact_receives SET command=?", (encoded,))
    with pytest.raises(ArtifactReceiveError, match="receive_journal_corrupt"):
        node.recover()
    assert receive.transport.requests == []


def test_unknown_receive_journal_schema_is_preserved(receive: Any) -> None:
    node = receive.receiver()
    with node.journal.transaction() as connection:
        connection.execute("UPDATE artifact_receive_meta SET schema_version=2")
    before = node.journal.path.read_bytes()
    with pytest.raises(ArtifactReceiveError, match="receive_journal_schema"):
        receive.receiver()
    assert node.journal.path.read_bytes() == before


def test_zero_byte_input_publishes_an_empty_verified_file(receive: Any) -> None:
    receive.transport.data = b""
    receive.transport.headers = {"Content-Length": "0"}
    node = receive.receiver()
    receipt = node.receive(receive.command(data=b""))
    assert receipt["size"] == 0
    assert receipt["sha256"] == hashlib.sha256(b"").hexdigest()
    target = receive.root / (receipt["grant_id"] + ".tar")
    assert target.is_file() and target.read_bytes() == b""
    assert node.pending_receipts() == ((receive.command(data=b"")["command_id"], receipt),)
    assert len(receive.transport.requests) == 1
