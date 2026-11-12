"""Crash windows, disk accounting, backup compatibility and reference closure."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from typing import Any

import pytest

from hub.application.artifact_transfer_service import ArtifactTransferService
from hub.domain.artifact_transfer import ArtifactTransferError
from hub.infrastructure.artifact_transfer_repository import ArtifactTransferRepository
from hub.infrastructure.platform_backup import BackupError, PlatformBackupService
from hub.infrastructure.browser_capture_gc import BrowserCaptureGcRepository, BrowserCaptureGcError
from hub.platform.artifacts import ArtifactStore
from test_artifact_transfer import TransferFixture, transfer
from test_workflow_dispatch import setup
from test_workflow_repository import OWNER, WORKSPACE


class ProcessCrash(BaseException):
    """Fault injection bypassing ordinary application exception recovery."""


def restarted(fixture: TransferFixture) -> ArtifactTransferService:
    repository = ArtifactTransferRepository(fixture.repository.db_path, command_public_key=fixture.public_key, clock=fixture.workflow.clock)
    repository.init()
    return ArtifactTransferService(repository, ArtifactStore(fixture.service.artifacts.root, clock=fixture.workflow.clock), clock=fixture.workflow.clock)


@pytest.mark.parametrize("window", ["before_rename", "after_rename", "after_database_ready"])
def test_crash_recovers_fixed_id_without_republishing(transfer: TransferFixture, monkeypatch: Any, window: str) -> None:
    manifest = transfer.reserve()
    transfer.part(manifest.transfer_id)
    original = __import__("hub.platform.artifacts", fromlist=["rename_directory_no_replace"]).rename_directory_no_replace

    def interrupt(source: Path, destination: Path) -> None:
        if window == "before_rename":
            raise ProcessCrash()
        original(source, destination)
        raise ProcessCrash()

    with monkeypatch.context() as patch:
        if window == "after_database_ready":
            patch.setattr(transfer.service.artifacts, "mark_ready", lambda *args, **kwargs: (_ for _ in ()).throw(ProcessCrash()))
        else:
            patch.setattr("hub.platform.artifacts.rename_directory_no_replace", interrupt)
        with pytest.raises(ProcessCrash):
            transfer.commit(manifest.transfer_id)
    assert transfer.service.artifacts.get(OWNER, WORKSPACE, manifest.artifact_id) is None
    service = restarted(transfer)
    assert service.recover() == 1
    assert service.artifacts.read(OWNER, WORKSPACE, manifest.artifact_id) == b"payload"
    assert [item.name for item in service.artifacts.root.iterdir()] == [manifest.artifact_id]
    assert transfer.commit(manifest.transfer_id)["artifact_id"] == manifest.artifact_id
    assert transfer.repository.reservation(manifest.transfer_id)["state"] == "ready"


def test_file_written_before_part_receipt_is_reconciled_on_restart(transfer: TransferFixture, monkeypatch: Any) -> None:
    manifest = transfer.reserve()
    with monkeypatch.context() as patch:
        patch.setattr(transfer.repository, "record_part", lambda *args, **kwargs: (_ for _ in ()).throw(ProcessCrash()))
        with pytest.raises(ProcessCrash):
            transfer.part(manifest.transfer_id)
    service = restarted(transfer)
    assert service.recover() == 0
    assert service.status(manifest.transfer_id, owner_id=OWNER, node_id="node-a", worker_id="node-worker")["parts"][0]["offset"] == 0
    assert transfer.commit(manifest.transfer_id)["artifact_id"] == manifest.artifact_id


@pytest.mark.parametrize("damage", ["missing", "hash", "reference"])
def test_ready_with_missing_or_inconsistent_content_is_quarantined(transfer: TransferFixture, damage: str, tmp_path: Path) -> None:
    manifest = transfer.reserve()
    transfer.part(manifest.transfer_id)
    transfer.commit(manifest.transfer_id)
    directory = transfer.service.artifacts.root / manifest.artifact_id
    if damage == "missing":
        directory.rename(tmp_path / "retained-damaged-artifact")
    elif damage == "hash":
        (directory / "content").write_bytes(b"changed")
    else:
        with transfer.workflow.repository.transaction() as connection:
            connection.execute("DELETE FROM workflow_artifact_refs")
    assert restarted(transfer).recover() == 0
    assert transfer.repository.reservation(manifest.transfer_id)["state"] == "quarantined"
    assert transfer.service.artifacts.get(OWNER, WORKSPACE, manifest.artifact_id) is None


def test_expired_reservation_keeps_actual_failed_staging_charge(transfer: TransferFixture) -> None:
    manifest = transfer.reserve()
    transfer.part(manifest.transfer_id)
    transfer.workflow.clock.now = 2100
    restarted(transfer).recover()
    row = transfer.repository.reservation(manifest.transfer_id)
    assert row["state"] == "failed" and row["failure_code"] == "reservation_expired"
    assert row["staging_bytes"] == 7
    assert (transfer.service.staging_root / manifest.transfer_id / "0").read_bytes() == b"payload"
    assert transfer.service.gc_candidates()[0]["dry_run"] is True


def test_partial_write_disk_full_remains_charged(transfer: TransferFixture, monkeypatch: Any) -> None:
    manifest = transfer.reserve()
    original = os.fsync
    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError(errno.ENOSPC, "disk full")))
        with pytest.raises(ArtifactTransferError, match="staging_write_failed") as failure:
            transfer.part(manifest.transfer_id)
    assert isinstance(failure.value.__cause__, OSError)
    assert original is os.fsync
    assert transfer.repository.reservation(manifest.transfer_id)["staging_bytes"] == 7
    assert transfer.repository.reservation(manifest.transfer_id)["state"] == "failed"


def test_database_failure_after_file_rename_recovers_same_file(transfer: TransferFixture) -> None:
    manifest = transfer.reserve()
    transfer.part(manifest.transfer_id)
    with transfer.workflow.repository.transaction() as connection:
        connection.execute("CREATE TRIGGER fail_ready BEFORE UPDATE OF state ON artifact_transfers WHEN NEW.state='ready' BEGIN SELECT RAISE(ABORT,'fault'); END")
    with pytest.raises(ArtifactTransferError, match="transfer_store"):
        transfer.commit(manifest.transfer_id)
    assert transfer.service.artifacts.get(OWNER, WORKSPACE, manifest.artifact_id) is None
    with transfer.workflow.repository.transaction() as connection:
        connection.execute("DROP TRIGGER fail_ready")
    assert restarted(transfer).recover() == 1
    assert transfer.commit(manifest.transfer_id)["artifact_id"] == manifest.artifact_id


def test_v2_backup_restore_preserves_manifest_and_resets_absent_scratch_charge(transfer: TransferFixture, tmp_path: Path) -> None:
    manifest = transfer.reserve()
    transfer.part(manifest.transfer_id)
    result = transfer.commit(manifest.transfer_id)
    service = PlatformBackupService()
    backup = tmp_path / "backup"
    summary = service.create_backup(transfer.repository.db_path, transfer.service.artifacts.root, backup)
    assert summary["artifact_count"] == 1
    restored = tmp_path / "restored"
    service.restore_backup(backup, restored)
    store = ArtifactStore(restored / "artifacts")
    assert store.get(OWNER, WORKSPACE, manifest.artifact_id) == result
    assert store.read(OWNER, WORKSPACE, manifest.artifact_id) == b"payload"
    repo = ArtifactTransferRepository(restored / "platform.db")
    assert repo.reservation(manifest.transfer_id)["staging_bytes"] == 0


def test_backup_refuses_incomplete_transfer_and_unknown_schema(transfer: TransferFixture, tmp_path: Path) -> None:
    manifest = transfer.reserve()
    transfer.part(manifest.transfer_id)
    transfer.service.artifacts.root.mkdir(exist_ok=True)
    service = PlatformBackupService()
    with pytest.raises(BackupError, match="artifact_transfer_incomplete"):
        service.create_backup(transfer.repository.db_path, transfer.service.artifacts.root, tmp_path / "incomplete-backup")
    with transfer.workflow.repository.transaction() as connection:
        connection.execute("UPDATE artifact_transfer_schema_meta SET minimum_reader_version=999")
    with pytest.raises(BackupError, match="artifact_transfer_schema_unknown"):
        service.create_backup(transfer.repository.db_path, transfer.service.artifacts.root, tmp_path / "unknown-backup")


def test_reference_scan_protects_transfer_ids_and_refuses_unknown_schema(transfer: TransferFixture) -> None:
    manifest = transfer.reserve()
    gc = BrowserCaptureGcRepository(transfer.repository.db_path, transfer.service.artifacts.root)
    with transfer.repository.transaction() as connection:
        connection.execute("CREATE TEMP TABLE transfer_reference_candidates (artifact_id TEXT PRIMARY KEY) WITHOUT ROWID")
        connection.execute("INSERT INTO temp.transfer_reference_candidates VALUES(?)", (manifest.artifact_id,))
        assert manifest.artifact_id in gc._referenced(connection, gc._reference_surfaces(connection), "transfer_reference_candidates")
    with transfer.repository.transaction(write=True) as connection:
        connection.execute("UPDATE artifact_transfer_schema_meta SET schema_version=2")
    with transfer.repository.transaction(verify_schema=False) as connection:
        with pytest.raises(BrowserCaptureGcError, match="capture_gc_reference_surface_unknown"):
            gc._reference_surfaces(connection)


def test_reference_inventory_materializes_only_requested_cleanup_ids(transfer: TransferFixture) -> None:
    manifest = transfer.reserve()
    with transfer.repository.transaction(write=True) as connection:
        connection.execute("UPDATE platform_commands SET result=? WHERE command_id=?",
                           (json.dumps({"artifact_id": "outside-this-cleanup-batch"}), transfer.command_id))
    assert transfer.repository.referenced_ids((manifest.artifact_id,)) == frozenset({manifest.artifact_id})
    assert transfer.repository.referenced_ids(()) == frozenset()


def _publish_then_exit(db_path: str, artifact_root: str, public_key: bytes, transfer_id: str, window: str) -> None:
    import hashlib

    repository = ArtifactTransferRepository(Path(db_path), command_public_key=public_key, clock=lambda: 104)
    service = ArtifactTransferService(repository, ArtifactStore(Path(artifact_root), clock=lambda: 104), clock=lambda: 104)
    if window == "before_rename":
        import hub.platform.artifacts as artifact_module
        artifact_module.rename_directory_no_replace = lambda *args, **kwargs: os._exit(73)
    elif window == "after_database_ready":
        original = repository.publish_ready
        def interrupt(*args: Any, **kwargs: Any) -> None:
            original(*args, **kwargs)
            os._exit(73)
        repository.publish_ready = interrupt
    else:
        repository.publish_ready = lambda *args, **kwargs: os._exit(73)
    service.commit(transfer_id, owner_id=OWNER, node_id="node-a", worker_id="node-worker", command_id="export-a",
                   size=7, sha256=hashlib.sha256(b"payload").hexdigest())


@pytest.mark.parametrize("window", ["before_rename", "after_rename", "after_database_ready"])
def test_actual_process_exit_after_rename_preserves_one_fixed_artifact(transfer: TransferFixture, window: str) -> None:
    import multiprocessing

    manifest = transfer.reserve()
    transfer.part(manifest.transfer_id)
    process = multiprocessing.get_context("spawn").Process(target=_publish_then_exit,
        args=(str(transfer.repository.db_path), str(transfer.service.artifacts.root), transfer.public_key, manifest.transfer_id, window))
    process.start()
    process.join(timeout=20)
    assert process.exitcode == 73
    assert transfer.service.artifacts.get(OWNER, WORKSPACE, manifest.artifact_id) is None
    assert restarted(transfer).recover() == 1
    assert transfer.commit(manifest.transfer_id)["artifact_id"] == manifest.artifact_id
    assert len(list(transfer.service.artifacts.root.iterdir())) == 1


def test_expired_publication_cannot_become_ready_on_recovery(transfer: TransferFixture, monkeypatch: Any) -> None:
    manifest = transfer.reserve()
    transfer.part(manifest.transfer_id)
    with monkeypatch.context() as patch:
        patch.setattr(transfer.repository, "publish_ready", lambda *args, **kwargs: (_ for _ in ()).throw(ProcessCrash()))
        with pytest.raises(ProcessCrash):
            transfer.commit(manifest.transfer_id)
    transfer.workflow.clock.now = 4001
    assert restarted(transfer).recover() == 0
    assert transfer.repository.reservation(manifest.transfer_id)["state"] == "quarantined"
    assert transfer.service.artifacts.get(OWNER, WORKSPACE, manifest.artifact_id) is None
    assert transfer.repository.staging_bytes(owner_id=OWNER, workspace_id=WORKSPACE) == 14


def test_staging_symlink_unknown_file_and_scan_overflow_fail_closed(transfer: TransferFixture, tmp_path: Path) -> None:
    manifest = transfer.reserve()
    external = tmp_path / "external"
    external.write_bytes(b"do not change")
    path = transfer.service.staging_root / manifest.transfer_id / "0"
    path.symlink_to(external)
    with pytest.raises(ArtifactTransferError, match="transfer_corrupt|unsafe_staging_path|part_conflict"):
        transfer.part(manifest.transfer_id)
    assert external.read_bytes() == b"do not change"
    with pytest.raises(ArtifactTransferError, match="unsafe_staging_path"):
        restarted(transfer).recover()
    with pytest.raises(ArtifactTransferError, match="transfer_scan_limit"):
        transfer.repository.manifests(limit=0)


def test_reference_kept_in_dry_run_even_after_quarantine(transfer: TransferFixture) -> None:
    manifest = transfer.reserve()
    transfer.part(manifest.transfer_id)
    transfer.commit(manifest.transfer_id)
    (transfer.service.artifacts.root / manifest.artifact_id / "content").write_bytes(b"changed")
    restarted(transfer).recover()
    item = transfer.service.gc_candidates()[0]
    assert item["referenced"] is True and item["dry_run"] is True
    assert (transfer.service.artifacts.root / manifest.artifact_id / "content").exists()


def test_provenance_tampering_and_future_manifest_are_rejected(transfer: TransferFixture, tmp_path: Path) -> None:
    manifest = transfer.reserve()
    transfer.part(manifest.transfer_id)
    transfer.commit(manifest.transfer_id)
    path = transfer.service.artifacts.root / manifest.artifact_id / "manifest.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["provenance_ref"]["generation"] = 2
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(BackupError, match="artifact_transfer_reference_invalid"):
        PlatformBackupService().create_backup(transfer.repository.db_path, transfer.service.artifacts.root, tmp_path / "bad-provenance")
    assert restarted(transfer).recover() == 0
    assert transfer.repository.reservation(manifest.transfer_id)["state"] == "quarantined"
    raw["schema_version"] = 999
    raw["ready"] = True
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(BackupError, match="artifact_schema_unknown"):
        PlatformBackupService().create_backup(transfer.repository.db_path, transfer.service.artifacts.root, tmp_path / "future-schema")


def test_v2_encrypted_backup_round_trip(transfer: TransferFixture, tmp_path: Path) -> None:
    manifest = transfer.reserve()
    transfer.part(manifest.transfer_id)
    transfer.commit(manifest.transfer_id)
    backup = tmp_path / "encrypted"
    restored = tmp_path / "decrypted"
    service = PlatformBackupService()
    service.create_encrypted_backup(transfer.repository.db_path, transfer.service.artifacts.root, backup, b"k" * 32, "fixture")
    service.restore_encrypted_backup(backup, restored, b"k" * 32, expected_key_id="fixture")
    assert ArtifactStore(restored / "artifacts").read(OWNER, WORKSPACE, manifest.artifact_id) == b"payload"
