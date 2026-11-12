"""Explicit Windows kernel/source tests; the disk sink is a protocol test double."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
import ctypes as C
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import mmap
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tracemalloc
from typing import Any
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from fixtures.workflow.windows_artifact_probe import private_directory
from fleet_core.artifact_export import ArtifactExportError
from fleet_core.artifact_receive import ArtifactReceiveError
from fleet_core.control import generate_ed25519_keypair
from fleet_core.platform_command import PlatformCommand
from tools.platform import artifact_windows as win
from tools.platform.artifact_export import ArtifactExporter, OutputFile
from tools.platform.artifact_transfer import ArtifactReceiver, InputDirectory
from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig

CHUNK = 1024 * 1024
CREDENTIAL = "node-a:" + "n" * 40


class DiskSink:
    """Check exact transfer requests and retain bounded metadata across child exits."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.metadata_path, self.payload = root / "transfer.json", root / "payload.bin"
        self.metadata: dict[str, Any] | None = json.loads(self.metadata_path.read_text(encoding="utf-8")) if self.metadata_path.exists() else None
        self.requests: list[tuple[str, str, int]] = []
        self.drop: str | None = None
        self.after: Callable[[str], None] | None = None

    def _save(self) -> None:
        self.metadata_path.write_text(json.dumps(self.metadata, sort_keys=True, separators=(",", ":")), encoding="utf-8")

    def _reserve(self, body: bytes) -> tuple[int, dict[str, Any]]:
        data = json.loads(body)
        assert set(data) == {"command_id", "output_key", "name", "size", "sha256"}
        assert data["command_id"] == "export-a" and data["output_key"] == "report"
        assert type(data["size"]) is int and 0 <= data["size"] <= 256 * CHUNK
        if self.metadata is None:
            self.metadata = data | {"transfer_id": "transfer-a", "artifact_id": "artifact-a",
                "parts": {}, "state": "reserved", "reservations": 0, "puts": 0, "commits": 0}
            with self.payload.open("xb"):
                assert self.payload.is_file()
        assert all(self.metadata[key] == value for key, value in data.items())
        self.metadata["reservations"] += 1
        self._save()
        expiry = datetime.fromtimestamp(1000, timezone.utc).isoformat()
        return 201, {"ok": True, "transfer": {"transfer_id": "transfer-a", "artifact_id": "artifact-a",
            "chunk_size": CHUNK, "expires_at": expiry}}

    def _status(self) -> tuple[int, dict[str, Any]]:
        assert self.metadata is not None
        row = self.metadata
        return 200, {"ok": True, "transfer": {key: row[key] for key in
            ("transfer_id", "artifact_id", "size", "sha256", "state")} | {"chunk_size": CHUNK,
            "parts": [row["parts"][key] for key in sorted(row["parts"], key=int)]}}

    def _part(self, path: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        assert self.metadata is not None and self.metadata["state"] != "ready"
        offset = int(path.rsplit("/", 1)[1])
        assert offset >= 0 and offset % CHUNK == 0 and offset < self.metadata["size"]
        assert len(body) == min(CHUNK, self.metadata["size"] - offset)
        digest = hashlib.sha256(body).hexdigest()
        assert headers["X-Content-SHA256"] == digest
        assert headers["Content-Type"] == "application/octet-stream"
        part = {"offset": offset, "length": len(body), "sha256": digest}
        previous = self.metadata["parts"].get(str(offset))
        if previous is not None:
            assert previous == part
            return 200, {"ok": True, "part": part}
        with self.payload.open("r+b") as target:
            target.seek(offset)
            target.write(body)
            target.flush()
            os.fsync(target.fileno())
        self.metadata["parts"][str(offset)] = part
        self.metadata["state"] = "uploading"
        self.metadata["puts"] += 1
        self._save()
        return 200, {"ok": True, "part": part}

    def _commit(self, body: bytes) -> tuple[int, dict[str, Any]]:
        assert self.metadata is not None
        data = json.loads(body)
        assert data == {key: self.metadata[key] for key in ("command_id", "size", "sha256")}
        assert set(self.metadata["parts"]) == {str(offset) for offset in range(0, data["size"], CHUNK)}
        digest = hashlib.sha256()
        with self.payload.open("rb") as source:
            for chunk in iter(lambda: source.read(65536), b""):
                digest.update(chunk)
        assert self.payload.stat().st_size == data["size"] and digest.hexdigest() == data["sha256"]
        self.metadata["commits"] += 1
        self.metadata["state"] = "ready"
        self._save()
        artifact = {key: self.metadata[key] for key in ("artifact_id", "size", "sha256", "name")}
        artifact.update(workspace_id="workspace", schema_version=2,
            provenance_ref={"producer_binding": {"command_id": "export-a"}, "output_key": "report"})
        return 200, {"ok": True, "artifact": artifact}

    def request_bounded_json(self, method: str, url: str, body: bytes | None,
                             headers: dict[str, str], **kwargs: Any) -> tuple[int, dict[str, Any]]:
        assert kwargs["deadline"] > 0 and not kwargs["should_cancel"]()
        parsed = urlsplit(url)
        assert parsed.scheme == "https" and parsed.netloc == "hub.test" and not parsed.query
        assert headers["X-Platform-Node-Credential"] == CREDENTIAL
        assert headers["X-Platform-Command-ID"] == "export-a" and headers["X-Platform-Worker-ID"] == "worker"
        assert headers["Accept-Encoding"] == "identity"
        if body is not None:
            assert headers["Content-Length"] == str(len(body))
        path = parsed.path.removeprefix("/api/platform/v1/node/artifact-transfers")
        self.requests.append((method, path, len(body or b"")))
        if method == "POST" and path == "":
            stage, result = "reserve", self._reserve(body)
        elif method == "GET" and path == "/transfer-a":
            stage, result = "status", self._status()
        elif method == "PUT" and path.startswith("/transfer-a/parts/"):
            stage, result = "part", self._part(path, body, headers)
        else:
            assert method == "POST" and path == "/transfer-a/commit"
            stage, result = "commit", self._commit(body)
        if self.after is not None:
            self.after(stage)
        if self.drop == stage:
            self.drop = None
            raise OSError("protocol fixture lost an already stored response")
        return result


@dataclass
class ExportFixture:
    root: Path
    journal: Path
    source: Path
    sink: DiskSink
    public_key: bytes
    command: dict[str, Any]

    def exporter(self, **changes: Any) -> ArtifactExporter:
        values = dict(node_id="node-a", credential=CREDENTIAL, worker_id="worker", hub_url="https://hub.test",
            public_key=self.public_key, outputs={("workspace", "report"): OutputFile(self.root, "nested/report.bin")},
            journal_path=self.journal / "exports.db", transport=self.sink, clock=lambda: 100.)
        return ArtifactExporter(**(values | changes))


@pytest.fixture
def export_fixture(tmp_path: Path) -> ExportFixture:
    assert os.name == "nt" and win.windows_supported()
    root, journal, sink = [private_directory(tmp_path / name) for name in ("outputs", "journals", "sink")]
    nested = private_directory(root / "nested")
    source = nested / "report.bin"
    with win.root_handle(nested) as directory:
        with os.fdopen(win.open_child(directory, "report.bin", writable=True, create=True, exclusive=True), "wb") as output:
            output.write(b"payload")
    private, public = generate_ed25519_keypair()
    command = PlatformCommand.create(command_id="export-a", target_node="node-a", action="artifact.export",
        resource_id="workspace", arguments={"output_key": "report"}, run_id="run-a", expires_at=1000,
        retry_class="idempotent_with_key").signed(private).as_dict()
    return ExportFixture(root, journal, source, DiskSink(sink), public, command)


def _acl(path: Path, permission: str) -> None:
    result = subprocess.run(["icacls", str(path), "/grant", permission], capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_signed_windows_export_has_fixed_identity_and_durable_receipt(export_fixture: ExportFixture) -> None:
    node = export_fixture.exporter()
    receipt = node.export(export_fixture.command)
    assert node.available
    assert receipt == {"output_key": "report", "artifact": {"artifact_id": "artifact-a", "size": 7,
        "sha256": hashlib.sha256(b"payload").hexdigest()}}
    assert export_fixture.sink.payload.read_bytes() == b"payload"
    record = node.journal.get("export-a")
    assert record["state"] == "ready" and record["transfer_id"] == "transfer-a"
    identity = json.loads(record["source_identity"])
    assert set(identity) == {"dev", "ino", "size", "mtime_ns", "ctime_ns", "mode", "uid", "nlink", "acl", "root_dev", "root_ino"}
    assert all(type(value) is str for value in identity.values()) and int(identity["ino"]) > 0
    assert identity["uid"].startswith("S-1-")
    requests = list(export_fixture.sink.requests)
    assert export_fixture.exporter().export(export_fixture.command) == receipt
    assert export_fixture.sink.requests == requests
    assert node.pending_receipts() == (("export-a", receipt),)
    assert CREDENTIAL.encode() not in node.journal.path.read_bytes()
    assert str(export_fixture.root) not in json.dumps(receipt)
    node.acknowledge("export-a", receipt)
    assert node.pending_receipts() == ()


def test_native_source_identity_is_stable_across_handle_reads(export_fixture: ExportFixture) -> None:
    with win.source_handle(export_fixture.root, "nested/report.bin") as (source, identity):
        assert source.read() == b"payload"
        current = win.source_identity(source.fileno())
        assert current == {key: identity[key] for key in current}
        with win.source_handle(export_fixture.root, "nested/report.bin") as (_second, repeated):
            assert repeated == identity


def test_native_source_and_nested_parent_cannot_be_renamed_or_written(export_fixture: ExportFixture) -> None:
    with win.source_handle(export_fixture.root, "nested/report.bin"):
        with pytest.raises(PermissionError):
            export_fixture.source.write_bytes(b"changed")
        with pytest.raises(PermissionError):
            export_fixture.source.rename(export_fixture.source.with_name("changed.bin"))
        with pytest.raises(PermissionError):
            export_fixture.source.parent.rename(export_fixture.root / "changed-parent")
    renamed = export_fixture.source.with_name("preserved-after-close.bin")
    export_fixture.source.rename(renamed)
    assert renamed.read_bytes() == b"payload"


def test_native_source_rejects_an_existing_writable_handle(export_fixture: ExportFixture) -> None:
    with export_fixture.source.open("r+b"):
        with pytest.raises(ArtifactReceiveError) as error:
            with win.source_handle(export_fixture.root, "nested/report.bin"):
                pytest.fail("source with an existing writer was admitted")
    assert isinstance(error.value.__cause__, OSError) and error.value.__cause__.winerror == 32
    assert export_fixture.source.read_bytes() == b"payload"


def test_native_source_rejects_a_live_writable_mapping(export_fixture: ExportFixture) -> None:
    with export_fixture.source.open("r+b") as descriptor:
        view = mmap.mmap(descriptor.fileno(), 0, access=mmap.ACCESS_WRITE)
    try:
        with pytest.raises(ArtifactReceiveError) as error:
            with win.source_handle(export_fixture.root, "nested/report.bin"):
                pytest.fail("source with a writable mapping was admitted")
        assert isinstance(error.value.__cause__, OSError) and error.value.__cause__.winerror == 32
    finally:
        view.close()


@pytest.mark.parametrize("kind", ["hardlink", "directory", "junction", "file-acl", "nested-acl"])
def test_unsafe_windows_source_never_starts_transfer(export_fixture: ExportFixture, tmp_path: Path, kind: str) -> None:
    relative = "nested/report.bin"
    if kind == "hardlink":
        os.link(export_fixture.source, export_fixture.source.with_name("hardlink.bin"))
    elif kind == "directory":
        relative = "nested"
    elif kind == "junction":
        outside = private_directory(tmp_path / "outside")
        (outside / "report.bin").write_bytes(b"outside stays unchanged")
        path, target = str(export_fixture.root / "linked").replace("'", "''"), str(outside).replace("'", "''")
        script = f"New-Item -ItemType Junction -Path '{path}' -Target '{target}' | Out-Null"
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, timeout=20)
        assert result.returncode == 0, result.stderr
        relative = "linked/report.bin"
    else:
        _acl(export_fixture.source if kind == "file-acl" else export_fixture.source.parent, "*S-1-1-0:(W)")
    node = export_fixture.exporter(outputs={("workspace", "report"): OutputFile(export_fixture.root, relative)})
    with pytest.raises(ArtifactExportError, match="unsafe_output_path"):
        node.export(export_fixture.command)
    assert export_fixture.sink.requests == [] and node.journal.get("export-a") is None
    assert export_fixture.source.read_bytes() == b"payload"
    if kind == "junction":
        assert (outside / "report.bin").read_bytes() == b"outside stays unchanged"


@pytest.mark.parametrize("path", ["nested/report.bin:secret", "nested/NUL", "nested/report.", "nested//report.bin"])
def test_windows_output_registration_rejects_native_ambiguity(export_fixture: ExportFixture, path: str) -> None:
    with pytest.raises(ArtifactExportError, match="invalid_output_file"):
        OutputFile(export_fixture.root, path)


def test_writer_open_is_refused_before_reservation(export_fixture: ExportFixture) -> None:
    node = export_fixture.exporter()
    with export_fixture.source.open("r+b"):
        with pytest.raises(ArtifactExportError, match="unsafe_output_path"):
            node.export(export_fixture.command)
    assert node.journal.get("export-a") is None and export_fixture.sink.requests == []


@pytest.mark.parametrize("stage", ["reserve", "part", "commit"])
def test_lost_response_resumes_original_ids_and_parts(export_fixture: ExportFixture, stage: str) -> None:
    export_fixture.sink.drop = stage
    with pytest.raises(ArtifactExportError, match="export_io_failed"):
        export_fixture.exporter().export(export_fixture.command)
    restarted = export_fixture.exporter()
    assert restarted.recover() == ()
    receipt = restarted.pending_receipts()[0][1]
    assert receipt["artifact"]["artifact_id"] == "artifact-a"
    assert restarted.journal.get("export-a")["transfer_id"] == "transfer-a"
    assert export_fixture.sink.metadata["puts"] == 1
    assert export_fixture.sink.payload.read_bytes() == b"payload"


def test_changed_source_cannot_resume_original_export(export_fixture: ExportFixture) -> None:
    export_fixture.sink.drop = "part"
    with pytest.raises(ArtifactExportError, match="export_io_failed"):
        export_fixture.exporter().export(export_fixture.command)
    export_fixture.source.write_bytes(b"changed")
    restarted = export_fixture.exporter()
    assert restarted.recover() == (("export-a", "export_source_changed"),)
    assert restarted.journal.get("export-a")["state"] == "reconciliation"
    assert export_fixture.sink.metadata["commits"] == 0 and restarted.pending_receipts() == ()


def test_write_during_upload_is_denied_and_fixed_bytes_commit(export_fixture: ExportFixture) -> None:
    def attempt(stage: str) -> None:
        if stage == "part":
            with pytest.raises(PermissionError):
                export_fixture.source.write_bytes(b"changed")
    export_fixture.sink.after = attempt
    receipt = export_fixture.exporter().export(export_fixture.command)
    assert receipt["artifact"]["sha256"] == hashlib.sha256(b"payload").hexdigest()
    assert export_fixture.source.read_bytes() == export_fixture.sink.payload.read_bytes() == b"payload"


@pytest.mark.parametrize("permission", ["*S-1-1-0:(R)", "*S-1-1-0:(W)"])
def test_acl_change_during_upload_never_commits(export_fixture: ExportFixture, permission: str) -> None:
    def change(stage: str) -> None:
        if stage == "part":
            _acl(export_fixture.source, permission)
    export_fixture.sink.after = change
    node = export_fixture.exporter()
    with pytest.raises(ArtifactExportError):
        node.export(export_fixture.command)
    assert node.journal.get("export-a")["state"] == "reconciliation"
    assert export_fixture.sink.metadata["commits"] == 0 and node.pending_receipts() == ()
    assert export_fixture.source.read_bytes() == b"payload"


@pytest.mark.parametrize("field", ["LastWriteTime", "ChangeTime", "FileAttributes"])
def test_native_metadata_change_after_part_never_commits(export_fixture: ExportFixture, field: str) -> None:
    expected: dict[str, str] = {}

    def change(stage: str) -> None:
        if stage != "part":
            return
        native = win._native()
        handle = native.kernel.CreateFileW(str(export_fixture.source), 0x180, 7, None, 3, 0x00200000, None)
        assert handle not in {None, C.c_void_p(-1).value}
        try:
            before, update, after, status = win._Basic(), win._Basic(), win._Basic(), win._Status()
            native.checked(native.kernel.GetFileInformationByHandleEx(handle, 0, C.byref(before), C.sizeof(before)))
            value = before.FileAttributes ^ 2 if field == "FileAttributes" else getattr(before, field) + 20000017
            setattr(update, field, value)
            native.status(native.nt.NtSetInformationFile(handle, C.byref(status), C.byref(update), C.sizeof(update), 4))
            native.checked(native.kernel.GetFileInformationByHandleEx(handle, 0, C.byref(after), C.sizeof(after)))
            assert getattr(after, field) == value
            key = {"LastWriteTime": "mtime_ns", "ChangeTime": "ctime_ns", "FileAttributes": "mode"}[field]
            expected[key] = str(value if field == "FileAttributes" else value * 100)
        finally:
            native.close(handle)

    export_fixture.sink.after = change
    node = export_fixture.exporter()
    with pytest.raises(ArtifactExportError, match="export_source_changed"):
        node.export(export_fixture.command)
    assert node.journal.get("export-a")["state"] == "reconciliation"
    assert export_fixture.sink.metadata["commits"] == 0 and node.pending_receipts() == ()
    with win.source_handle(export_fixture.root, "nested/report.bin") as (source, identity):
        assert all(identity[key] == value for key, value in expected.items())
        assert source.read() == b"payload"
    renamed = export_fixture.source.with_name("preserved-after-metadata-error.bin")
    export_fixture.source.rename(renamed)
    assert renamed.read_bytes() == b"payload"


def test_ready_outbox_survives_absent_source(export_fixture: ExportFixture) -> None:
    node = export_fixture.exporter()
    receipt = node.export(export_fixture.command)
    export_fixture.source.rename(export_fixture.source.with_name("preserved-original.bin"))
    requests = list(export_fixture.sink.requests)
    restarted = export_fixture.exporter()
    assert restarted.pending_receipts() == (("export-a", receipt),)
    assert restarted.export(export_fixture.command) == receipt
    assert export_fixture.sink.requests == requests


@pytest.mark.parametrize("target", ["root", "journal"])
def test_export_advertisement_requires_protected_windows_roots(export_fixture: ExportFixture, target: str) -> None:
    node = export_fixture.exporter()
    assert node.available
    _acl(getattr(export_fixture, target), "*S-1-1-0:(W)")
    assert not node.available
    with pytest.raises(ArtifactExportError, match="unsafe_output_path"):
        node.export(export_fixture.command)
    assert export_fixture.sink.requests == []


def test_pending_export_with_unsafe_root_becomes_reconciliation(export_fixture: ExportFixture) -> None:
    export_fixture.sink.drop = "part"
    first = export_fixture.exporter()
    with pytest.raises(ArtifactExportError):
        first.export(export_fixture.command)
    _acl(export_fixture.root, "*S-1-1-0:(W)")
    restarted = export_fixture.exporter()
    assert restarted.available is False
    assert restarted.recover() == (("export-a", "unsafe_output_path"),)
    assert restarted.journal.get("export-a")["state"] == "reconciliation"
    assert export_fixture.sink.metadata["commits"] == 0


@pytest.mark.parametrize("damage", ["command", "receipt"])
def test_corrupt_ready_outbox_cannot_be_replayed(export_fixture: ExportFixture, damage: str) -> None:
    node = export_fixture.exporter()
    node.export(export_fixture.command)
    with node.journal.transaction() as connection:
        connection.execute("UPDATE artifact_exports SET " + damage + "='[]'")
    with pytest.raises(ArtifactExportError, match="export_reconciliation_required"):
        export_fixture.exporter().pending_receipts()
    assert node.journal.get("export-a")["state"] == "reconciliation"


def test_unknown_export_schema_preserves_original_store(export_fixture: ExportFixture) -> None:
    node = export_fixture.exporter()
    with node.journal.transaction() as connection:
        connection.execute("UPDATE artifact_export_meta SET schema_version=2")
    with pytest.raises(ArtifactExportError, match="export_journal_schema"):
        export_fixture.exporter()


def test_three_uncertain_attempts_stop_original_export(export_fixture: ExportFixture) -> None:
    node = export_fixture.exporter()
    for _ in range(3):
        export_fixture.sink.drop = "reserve"
        with pytest.raises(ArtifactExportError, match="export_io_failed"):
            node.export(export_fixture.command)
    assert node.journal.get("export-a")["state"] == "reconciliation"
    assert node.recover() == () and not node.deferred("export-a")


def test_limits_cancel_and_zero_bytes_keep_original_protocol(export_fixture: ExportFixture) -> None:
    with pytest.raises(ArtifactExportError, match="artifact_too_large"):
        export_fixture.exporter(max_bytes=6).export(export_fixture.command)
    with pytest.raises(ArtifactExportError, match="export_cancelled"):
        export_fixture.exporter(should_cancel=lambda: True).export(export_fixture.command)
    assert export_fixture.sink.requests == []
    export_fixture.source.write_bytes(b"")
    receipt = export_fixture.exporter().export(export_fixture.command)
    assert receipt["artifact"]["size"] == 0 and export_fixture.sink.metadata["puts"] == 0


def test_native_export_stream_is_bounded_for_twelve_mebibytes(export_fixture: ExportFixture) -> None:
    block = b"x" * CHUNK
    with export_fixture.source.open("wb") as output:
        for _ in range(12):
            output.write(block)
    tracemalloc.start()
    try:
        receipt = export_fixture.exporter().export(export_fixture.command)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert receipt["artifact"]["size"] == 12 * CHUNK and peak < 8 * CHUNK
    assert export_fixture.sink.metadata["puts"] == 12


def test_native_receive_and_export_share_original_two_slots(export_fixture: ExportFixture, tmp_path: Path) -> None:
    inputs = private_directory(tmp_path / "inputs")
    commands = export_fixture.journal / "commands.db"
    sender = export_fixture.exporter(slot_path=commands)
    receiver = ArtifactReceiver(node_id="node-a", credential=CREDENTIAL, worker_id="worker", hub_url="https://hub.test",
        public_key=export_fixture.public_key, inputs={"source": InputDirectory(inputs)},
        journal_path=export_fixture.journal / "receives.db", slot_path=commands, clock=lambda: 100.)
    with ExitStack() as holds:
        holds.enter_context(receiver._slot())
        holds.enter_context(sender._slot("held"))
        with pytest.raises(ArtifactExportError, match="export_concurrency_limit"):
            sender.export(export_fixture.command)
    assert export_fixture.sink.requests == []


def test_node_manifest_removes_export_after_root_acl_change(export_fixture: ExportFixture, tmp_path: Path) -> None:
    runtime = NodeRuntime(NodeRuntimeConfig(node_id="node-a", credential=CREDENTIAL, worker_id="worker",
        hub_url="https://hub.test", public_key=export_fixture.public_key, workspace_root=tmp_path / "workspace",
        journal_path=export_fixture.journal / "commands.db", capabilities=frozenset({"artifact.export"}),
        artifact_outputs=(("workspace", "report", OutputFile(export_fixture.root, "nested/report.bin")),)),
        transport=export_fixture.sink, clock=lambda: 100.)
    try:
        assert runtime.manifest()["capabilities"] == {"artifact.export": True}
        _acl(export_fixture.root, "*S-1-1-0:(W)")
        assert runtime.manifest()["capabilities"] == {}
    finally:
        runtime.close_all()


def _crash_export(root: str, journal: str, sink: str, public: bytes, command: dict[str, Any], stage: str) -> None:
    transport = DiskSink(Path(sink))
    def crash(current: str) -> None:
        if current == stage:
            os._exit(73)
    transport.after = crash
    node = ArtifactExporter(node_id="node-a", credential=CREDENTIAL, worker_id="worker", hub_url="https://hub.test",
        public_key=public, outputs={("workspace", "report"): OutputFile(Path(root), "nested/report.bin")},
        journal_path=Path(journal), transport=transport, clock=lambda: 100.)
    if stage == "ready":
        original = node.journal.ready
        def ready(*args: Any, **kwargs: Any) -> None:
            original(*args, **kwargs)
            os._exit(73)
        node.journal.ready = ready
    node.export(command)


@pytest.mark.parametrize("stage", ["reserve", "part", "commit", "ready"])
def test_real_windows_exit_recovers_same_artifact_and_releases_handles(export_fixture: ExportFixture, stage: str) -> None:
    journal = export_fixture.journal / "crash.db"
    child = multiprocessing.get_context("spawn").Process(target=_crash_export,
        args=(str(export_fixture.root), str(journal), str(export_fixture.sink.root), export_fixture.public_key, export_fixture.command, stage))
    child.start()
    try:
        child.join(25)
        assert child.exitcode == 73
        with export_fixture.source.open("r+b") as released:
            assert released.read() == b"payload"
        sink = DiskSink(export_fixture.sink.root)
        node = export_fixture.exporter(journal_path=journal, transport=sink)
        assert node.recover() == ()
        receipt = node.pending_receipts()[0][1]
        assert receipt["artifact"]["artifact_id"] == "artifact-a"
        assert node.journal.get("export-a")["transfer_id"] == "transfer-a"
        assert sink.metadata["puts"] == 1 and sink.payload.read_bytes() == b"payload"
    finally:
        if child.is_alive():
            child.terminate()
        child.join(10)
        assert not child.is_alive()
