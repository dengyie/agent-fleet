"""Run explicitly on an authorized Windows host; no emulated kernel results."""
from __future__ import annotations

import ctypes as C
import hashlib
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from fleet_core.artifact_receive import ArtifactReceiveError
from tools.platform import artifact_windows as win

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import test_artifact_receive as portable
from fleet_core.control import generate_ed25519_keypair
from fleet_core.platform_command import PlatformCommand
from tools.platform.artifact_transfer import ArtifactReceiver, InputDirectory
from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig


class _SecurityAttributes(C.Structure):
    _fields_ = [("length", win.U32), ("descriptor", win.HANDLE), ("inherit", win.U32)]


def private_directory(path: Path) -> Path:
    native = win._native()
    native._bind(native.kernel, "CreateDirectoryW", win.U32, [C.c_wchar_p, C.POINTER(_SecurityAttributes)])
    with native.private_security() as descriptor:
        attributes = _SecurityAttributes(C.sizeof(_SecurityAttributes), descriptor, 0)
        native.checked(native.kernel.CreateDirectoryW(str(path), C.byref(attributes)))
    return path


@pytest.fixture
def private_root(tmp_path: Path) -> Path:
    assert os.name == "nt" and win.windows_supported()
    return private_directory(tmp_path / "private-inputs")


def test_hub_download_capacity_uses_private_child_and_preserves_parent_acl(private_root: Path) -> None:
    from hub.infrastructure.artifact_capacity import capacity_directory
    from hub.platform import artifact_windows as hub_windows
    from hub.platform.artifact_lock import artifact_io_slot, artifact_io_slot_count

    # A database parent may legitimately inherit a broader application ACL.
    result = subprocess.run(["icacls", str(private_root), "/grant", "*S-1-1-0:(W)"],
        capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    with pytest.raises(ArtifactReceiveError, match="unsafe_input_path"):
        with hub_windows.root_handle(private_root):
            pytest.fail("ordinary artifact roots must retain their existing ACL requirement")
    with capacity_directory(private_root / "platform.db") as descriptor:
        identity = hub_windows.file_info(descriptor)
        with artifact_io_slot(descriptor, "probe", 1):
            with capacity_directory(private_root / "platform.db") as reopened:
                assert hub_windows.file_info(reopened) == identity
                assert artifact_io_slot_count(reopened, "probe", 1) == 1
    with hub_windows.root_handle(private_root / ".artifact-capacity") as descriptor:
        assert hub_windows.file_info(descriptor) == identity
        assert artifact_io_slot_count(descriptor, "probe", 1) == 0
    with pytest.raises(ArtifactReceiveError, match="unsafe_input_path"):
        with hub_windows.root_handle(private_root):
            pytest.fail("capacity initialization must not rewrite the database parent ACL")


def test_hub_download_capacity_refuses_existing_untrusted_child(private_root: Path) -> None:
    from hub.infrastructure.artifact_capacity import ArtifactCapacityError, capacity_directory

    child = private_directory(private_root / ".artifact-capacity")
    marker = child / "preserve.txt"
    marker.write_bytes(b"existing evidence")
    result = subprocess.run(["icacls", str(child), "/grant", "*S-1-1-0:(W)"],
        capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    with pytest.raises(ArtifactCapacityError, match="unsafe_input_path"):
        with capacity_directory(private_root / "platform.db"):
            pytest.fail("an unsafe existing child must not be silently replaced or repaired")
    assert marker.read_bytes() == b"existing evidence"


def test_native_pinned_file_identity_and_no_replace_publish(private_root: Path) -> None:
    with win.root_handle(private_root) as root:
        descriptor = win.open_child(root, ".stage.part", writable=True, create=True, exclusive=True)
        with os.fdopen(descriptor, "wb") as output:
            output.write(b"fixed bytes")
            output.flush()
            os.fsync(output.fileno())
            identity = win.file_info(output.fileno())
        win.publish(root, ".stage.part", "result.tar")
        assert win.entry(root, ".stage.part") is None
        assert win.entry(root, "result.tar") == identity
        with os.fdopen(win.open_child(root, "result.tar", writable=False), "rb") as source:
            assert source.read() == b"fixed bytes"
        with os.fdopen(win.open_child(root, ".second.part", writable=True, create=True, exclusive=True), "wb") as output:
            output.write(b"preserve second")
            output.flush()
            os.fsync(output.fileno())
        with pytest.raises(FileExistsError):
            win.publish(root, ".second.part", "result.tar")
    assert (private_root / "result.tar").read_bytes() == b"fixed bytes"
    assert (private_root / ".second.part").read_bytes() == b"preserve second"


def test_native_root_and_ancestor_cannot_be_renamed_while_pinned(private_root: Path, tmp_path: Path) -> None:
    with win.root_handle(private_root):
        with pytest.raises(PermissionError):
            private_root.rename(tmp_path / "moved-root")
        with pytest.raises(PermissionError):
            tmp_path.rename(tmp_path.with_name(tmp_path.name + "-moved"))


def test_native_hardlink_and_junction_entries_are_refused(private_root: Path, tmp_path: Path) -> None:
    outside = private_directory(tmp_path / "outside")
    source = outside / "source.bin"
    source.write_bytes(b"preserve outside")
    os.link(source, private_root / "linked.bin")
    junction = private_root / "junction"
    script = '$ErrorActionPreference="Stop"; New-Item -ItemType Junction -Path ' + repr(str(junction)) + ' -Target ' + repr(str(outside)) + ' | Out-Null'
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    with win.root_handle(private_root) as root:
        for name in ("linked.bin", "junction"):
            with pytest.raises((ArtifactReceiveError, OSError)):
                win.open_child(root, name, writable=False)
    with pytest.raises((ArtifactReceiveError, OSError)):
        with win.root_handle(junction):
            pytest.fail("junction must not be opened as an input root")
    assert source.read_bytes() == b"preserve outside"


def test_native_root_with_untrusted_write_acl_is_refused(private_root: Path) -> None:
    result = subprocess.run(["icacls", str(private_root), "/grant", "*S-1-1-0:(W)"], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    with pytest.raises(ArtifactReceiveError, match="unsafe_input_path"):
        with win.root_handle(private_root):
            pytest.fail("Everyone writable root must fail closed")


def test_native_inherited_untrusted_write_acl_is_refused(tmp_path: Path) -> None:
    native = win._native()
    native._bind(native.kernel, "CreateDirectoryW", win.U32, [C.c_wchar_p, C.POINTER(_SecurityAttributes)])
    parent = tmp_path / "inherited-parent"
    native.checked(native.kernel.CreateDirectoryW(str(parent), None))
    result = subprocess.run(["icacls", str(parent), "/grant", "*S-1-1-0:(OI)(CI)(W)"], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    root = parent / "unprotected-child"
    native.checked(native.kernel.CreateDirectoryW(str(root), None))
    with pytest.raises(ArtifactReceiveError, match="unsafe_input_path"):
        with win.root_handle(root):
            pytest.fail("inherited Everyone write must fail closed")


def _hold_lock(root: str, ready: Any, release: Any) -> None:
    with win.root_handle(Path(root)) as descriptor, win.lock_file(descriptor, ".grant.lock"):
        ready.send("locked")
        assert release.poll(10)
        assert release.recv() == "release"


def _assert_locked(root: int, name: str) -> None:
    with pytest.raises(OSError) as caught:
        with win.lock_file(root, name):
            pytest.fail("conflicting byte lock must not succeed")
    assert caught.value.winerror == 33


def test_native_lock_conflict_and_process_close_release(private_root: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready_parent, ready_child = context.Pipe(duplex=False)
    release_child, release_parent = context.Pipe(duplex=False)
    child = context.Process(target=_hold_lock, args=(str(private_root), ready_child, release_child))
    child.start()
    try:
        assert ready_parent.poll(10) and ready_parent.recv() == "locked"
        with win.root_handle(private_root) as root:
            _assert_locked(root, ".grant.lock")
        release_parent.send("release")
        child.join(10)
        assert child.exitcode == 0
        with win.root_handle(private_root) as root, win.lock_file(root, ".grant.lock"):
            assert win.entry(root, ".grant.lock") is not None
    finally:
        for pipe in (ready_parent, ready_child, release_parent, release_child):
            pipe.close()
        child.join(10)
        assert not child.is_alive()


def _hold_slot(root: str, ready: Any, release: Any) -> None:
    from tools.platform.artifact_lock import artifact_io_slot
    with win.root_handle(Path(root)) as descriptor, artifact_io_slot(descriptor, "commands.db", 2):
        ready.send("locked")
        assert release.poll(10) and release.recv() == "exit"
        os._exit(73)


def _assert_slots_full(descriptor: int) -> None:
    from tools.platform.artifact_lock import ArtifactSlotError, artifact_io_slot
    with pytest.raises(ArtifactSlotError, match="transfer_concurrency_limit"):
        with artifact_io_slot(descriptor, "commands.db", 2):
            pytest.fail("third byte stream must fail")


def _close_channels(channels: Any) -> None:
    for ready, release in channels:
        for pipe in (*ready, *release):
            pipe.close()


def test_native_fixed_two_slots_across_processes_release_on_exit(private_root: Path) -> None:
    from tools.platform.artifact_lock import artifact_io_slot_count
    context = multiprocessing.get_context("spawn")
    channels = [(context.Pipe(duplex=False), context.Pipe(duplex=False)) for _ in range(2)]
    children = [context.Process(target=_hold_slot, args=(str(private_root), ready[1], release[0])) for ready, release in channels]
    for child in children:
        child.start()
    try:
        for ready, _release in channels:
            assert ready[0].poll(10) and ready[0].recv() == "locked"
        with win.root_handle(private_root) as descriptor:
            assert artifact_io_slot_count(descriptor, "commands.db", 2) == 2
            _assert_slots_full(descriptor)
        for _ready, release in channels:
            release[1].send("exit")
        for child in children:
            child.join(10)
            assert child.exitcode == 73
        with win.root_handle(private_root) as descriptor:
            assert artifact_io_slot_count(descriptor, "commands.db", 2) == 0
    finally:
        _close_channels(channels)
        for child in children:
            child.join(10)
            assert not child.is_alive()


def test_native_exclusive_stage_does_not_truncate_existing_bytes(private_root: Path) -> None:
    with win.root_handle(private_root) as root:
        with os.fdopen(win.open_child(root, ".stage.part", writable=True, create=True, exclusive=True), "wb") as output:
            output.write(b"original")
        with pytest.raises(FileExistsError):
            win.open_child(root, ".stage.part", writable=True, create=True, exclusive=True)
    assert (private_root / ".stage.part").read_bytes() == b"original"


@pytest.fixture
def receive(tmp_path: Path) -> Any:
    root = private_directory(tmp_path / "receive-inputs")
    journal = private_directory(tmp_path / "receive-journal")
    private, public = generate_ed25519_keypair()
    transport = portable.ContentTransport(b"source archive bytes")
    def command(data: bytes = transport.data, **changes: Any) -> dict[str, Any]:
        arguments = {"artifact_id": "artifact-a", "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data), "input_alias": "source", "suffix": ".tar", "workflow_id": "wf-a",
            "step_key": "review", "generation": 1, "ref_key": "bundle", "input_digest": "a" * 64}
        arguments.update(changes.pop("arguments", {}))
        params = dict(command_id="receive-" + "a" * 64, target_node="node-b", action="artifact.receive",
            resource_id="workspace", grant_id="a" * 64, arguments=arguments, expires_at=1000,
            retry_class="idempotent_with_key")
        return PlatformCommand.create(**(params | changes)).signed(private).as_dict()
    def receiver(**changes: Any) -> ArtifactReceiver:
        params = dict(node_id="node-b", credential="node-b:" + "n" * 40, worker_id="worker-b",
            hub_url="https://hub.test", public_key=public, inputs={"source": InputDirectory(root, frozenset({".tar"}))},
            journal_path=journal / "receive.db", transport=transport, clock=lambda: 100.)
        return ArtifactReceiver(**(params | changes))
    return type("WindowsReceiveFixture", (), dict(root=root, transport=transport,
        command=staticmethod(command), receiver=staticmethod(receiver)))()


@pytest.mark.parametrize("check", [
    portable.test_stream_receive_is_verified_atomic_and_durable_after_restart,
    portable.test_hash_failure_and_short_read_never_publish,
    portable.test_receipt_corruption_or_file_tampering_is_not_replayed,
    portable.test_receive_stream_has_bounded_memory,
    portable.test_explicit_cancellation_and_deadline_leave_no_published_file,
    portable.test_concurrent_receivers_use_nonblocking_per_grant_lock,
    portable.test_two_node_receive_slots_are_shared_across_receiver_instances,
    portable.test_unknown_receive_journal_schema_is_preserved,
    portable.test_zero_byte_input_publishes_an_empty_verified_file,
])
def test_original_receive_contract_on_windows(receive: Any, check: Any) -> None:
    check(receive)


@pytest.mark.parametrize("window", ["partial", "before_rename", "after_rename", "after_ready"])
def test_original_receive_crash_windows(receive: Any, window: str) -> None:
    portable.test_process_crash_recovers_original_target_and_receipt(receive, window)


@pytest.mark.parametrize("corruption", ["file", "command", "receipt"])
def test_original_receive_ready_reconciliation_windows(receive: Any, corruption: str) -> None:
    portable.test_tampered_ready_outbox_requires_reconciliation(receive, corruption)


def test_receive_availability_checks_real_root_acl(receive: Any) -> None:
    node = receive.receiver()
    assert node.available
    result = subprocess.run(["icacls", str(receive.root), "/grant", "*S-1-1-0:(W)"], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert not node.available
    with pytest.raises(ArtifactReceiveError, match="capability_unavailable"):
        node.receive(receive.command())
    assert receive.transport.requests == []


def test_receive_only_configuration_does_not_advertise_windows_export(receive: Any, tmp_path: Path) -> None:
    receiver = receive.receiver()
    journal = private_directory(tmp_path / "receive-only-journal")
    node = NodeRuntime(NodeRuntimeConfig(node_id=receiver.node_id, credential=receiver.credential,
        workspace_root=tmp_path / "workspace", journal_path=journal / "commands.db",
        hub_url=receiver.origin, public_key=receiver.public_key, worker_id=receiver.worker_id,
        capabilities=frozenset({"artifact.receive"}), artifact_inputs=tuple(receiver.inputs.items())),
        clock=receiver.clock, transport=receiver.transport)
    try:
        assert node.artifact_exporter is None
        assert node.manifest()["capabilities"] == {"artifact.receive": True}
    finally:
        node.close_all()


def test_node_runtime_manifest_requires_protected_receive_paths(receive: Any, tmp_path: Path) -> None:
    receiver = receive.receiver()
    journal = private_directory(tmp_path / "runtime-journal")
    node = NodeRuntime(NodeRuntimeConfig(node_id=receiver.node_id, credential=receiver.credential,
        workspace_root=tmp_path / "workspace", journal_path=journal / "commands.db",
        hub_url=receiver.origin, public_key=receiver.public_key, worker_id=receiver.worker_id,
        capabilities=frozenset({"artifact.receive"}), artifact_inputs=tuple(receiver.inputs.items())),
        clock=receiver.clock, transport=receiver.transport)
    try:
        assert node.manifest()["capabilities"] == {"artifact.receive": True}
        result = subprocess.run(["icacls", str(receive.root), "/grant", "*S-1-1-0:(W)"], capture_output=True, timeout=20)
        assert result.returncode == 0, result.stderr
        assert node.manifest()["capabilities"] == {}
    finally:
        node.close_all()


@pytest.mark.parametrize("attack", ["hardlink", "junction"])
def test_receive_staging_attack_preserves_outside_bytes(receive: Any, tmp_path: Path, attack: str) -> None:
    outside = private_directory(tmp_path / "attack-target")
    source = outside / "original.bin"
    source.write_bytes(b"preserve outside")
    stage = receive.root / ("." + "a" * 64 + ".part")
    if attack == "hardlink":
        os.link(source, stage)
    else:
        script = "New-Item -ItemType Junction -Path " + repr(str(stage)) + " -Target " + repr(str(outside)) + " | Out-Null"
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, timeout=20)
        assert result.returncode == 0, result.stderr
    with pytest.raises(ArtifactReceiveError):
        receive.receiver().receive(receive.command())
    assert source.read_bytes() == b"preserve outside"
    assert not (receive.root / ("a" * 64 + ".tar")).exists()


def _crash_lock(root: str, ready: Any) -> None:
    with win.root_handle(Path(root)) as descriptor, win.lock_file(descriptor, ".crash.lock"):
        ready.send("locked")
        os._exit(73)


def test_native_process_exit_releases_kernel_lock(private_root: Path) -> None:
    context = multiprocessing.get_context("spawn")
    parent, sender = context.Pipe(duplex=False)
    child = context.Process(target=_crash_lock, args=(str(private_root), sender))
    child.start()
    try:
        assert parent.poll(10) and parent.recv() == "locked"
        child.join(10)
        assert child.exitcode == 73
        with win.root_handle(private_root) as descriptor, win.lock_file(descriptor, ".crash.lock"):
            assert win.entry(descriptor, ".crash.lock") is not None
    finally:
        parent.close()
        sender.close()
        child.join(10)
        assert not child.is_alive()
