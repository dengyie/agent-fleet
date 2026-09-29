import json
import os
import sqlite3
from pathlib import Path

import pytest

from hub.infrastructure.platform_backup import BackupError, PlatformBackupService
from hub.infrastructure.platform_db import PlatformRepository
from tools.platform.artifacts import ArtifactStore


OWNER = "owner@example.test"


def _source_pair(tmp_path: Path):
    db_path = tmp_path / "platform.db"
    platform = PlatformRepository(db_path)
    platform.init()
    platform.upsert_workspace(OWNER, {
        "workspace_id": "workspace-a",
        "name": "Workspace",
        "backend": "directory",
        "root_path": str(tmp_path / "workspace"),
        "enabled": True,
    })
    artifact_root = tmp_path / "artifacts"
    source = tmp_path / "report.md"
    source.write_text("backup payload", encoding="utf-8")
    artifact = ArtifactStore(artifact_root).put_file(OWNER, "workspace-a", source)
    return db_path, artifact_root, artifact


def test_backup_round_trip_uses_online_sqlite_copy_and_artifact_inventory(tmp_path):
    db_path, artifact_root, artifact = _source_pair(tmp_path)
    # Keep a committed row in WAL while the backup opens its own connection.
    writer = sqlite3.connect(str(db_path))
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE wal_probe(value TEXT NOT NULL)")
    writer.execute("INSERT INTO wal_probe(value) VALUES ('committed')")
    writer.commit()

    backup_root = tmp_path / "backup"
    service = PlatformBackupService()
    summary = service.create_backup(db_path, artifact_root, backup_root)
    writer.close()

    assert summary["schema_version"] == 1
    assert summary["artifact_count"] == 1
    envelope = json.loads((backup_root / "backup.json").read_text(encoding="utf-8"))
    assert str(db_path) not in json.dumps(envelope)
    assert "backup payload" not in json.dumps(envelope)
    assert "platform.db-wal" not in json.dumps(envelope)

    restored = tmp_path / "restored"
    restored_summary = service.restore_backup(backup_root, restored)
    assert restored_summary["artifact_count"] == 1
    restored_db = sqlite3.connect(str(restored / "platform.db"))
    assert restored_db.execute("SELECT value FROM wal_probe").fetchone() == ("committed",)
    restored_db.close()
    assert ArtifactStore(restored / "artifacts").read(
        OWNER, "workspace-a", artifact["artifact_id"]
    ) == b"backup payload"


def test_inspect_rejects_corrupt_artifact_before_restore_and_keeps_target_absent(tmp_path):
    db_path, artifact_root, artifact = _source_pair(tmp_path)
    backup_root = tmp_path / "backup"
    service = PlatformBackupService()
    service.create_backup(db_path, artifact_root, backup_root)
    content = backup_root / "artifacts" / artifact["artifact_id"] / "content"
    content.write_bytes(b"tampered data!")

    with pytest.raises(BackupError) as exc:
        service.restore_backup(backup_root, tmp_path / "restored")
    assert exc.value.code == "artifact_hash_mismatch"
    assert not (tmp_path / "restored").exists()
    assert str(tmp_path) not in str(exc.value)


def test_inspect_rejects_symlink_and_path_traversal_without_writing(tmp_path):
    db_path, artifact_root, artifact = _source_pair(tmp_path)
    service = PlatformBackupService()
    backup_root = tmp_path / "backup"
    service.create_backup(db_path, artifact_root, backup_root)
    content = backup_root / "artifacts" / artifact["artifact_id"] / "content"
    content.unlink()
    content.symlink_to(db_path)
    with pytest.raises(BackupError) as symlink:
        service.inspect_backup(backup_root)
    assert symlink.value.code == "unsafe_path"

    content.unlink()
    content.write_bytes(b"backup payload")
    envelope_path = backup_root / "backup.json"
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope["artifacts"][0]["artifact_id"] = "../escape"
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(BackupError) as traversal:
        service.inspect_backup(backup_root)
    assert traversal.value.code == "invalid_artifact_path"


def test_existing_restore_destination_is_never_overwritten(tmp_path):
    db_path, artifact_root, _artifact = _source_pair(tmp_path)
    service = PlatformBackupService()
    backup_root = tmp_path / "backup"
    service.create_backup(db_path, artifact_root, backup_root)
    target = tmp_path / "restored"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(BackupError) as exc:
        service.restore_backup(backup_root, target)
    assert exc.value.code == "destination_exists"
    assert marker.read_text(encoding="utf-8") == "keep"


def test_create_backup_never_overwrites_existing_target(tmp_path):
    db_path, artifact_root, _artifact = _source_pair(tmp_path)
    target = tmp_path / "backup"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(BackupError) as exc:
        PlatformBackupService().create_backup(db_path, artifact_root, target)
    assert exc.value.code == "destination_exists"
    assert marker.read_text(encoding="utf-8") == "keep"


def test_database_corruption_fails_inspection_before_restore_target_creation(tmp_path):
    db_path, artifact_root, _artifact = _source_pair(tmp_path)
    service = PlatformBackupService()
    backup_root = tmp_path / "backup"
    service.create_backup(db_path, artifact_root, backup_root)
    database = backup_root / "platform.db"
    with database.open("r+b") as handle:
        handle.seek(0)
        handle.write(b"not a sqlite database")

    with pytest.raises(BackupError) as exc:
        service.restore_backup(backup_root, tmp_path / "restored")
    assert exc.value.code in {"database_integrity_failed", "database_hash_mismatch"}
    assert not (tmp_path / "restored").exists()


def test_backup_rejects_malformed_artifact_metadata_and_source_symlink(tmp_path):
    db_path, artifact_root, artifact = _source_pair(tmp_path)
    manifest = artifact_root / artifact["artifact_id"] / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["size"] = True
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BackupError) as malformed:
        PlatformBackupService().create_backup(db_path, artifact_root, tmp_path / "backup")
    assert malformed.value.code == "invalid_manifest"

    source_dir = tmp_path / "source-symlink"
    source_dir.symlink_to(artifact_root, target_is_directory=True)
    with pytest.raises(BackupError) as symlink:
        PlatformBackupService().create_backup(db_path, source_dir, tmp_path / "backup-2")
    assert symlink.value.code == "unsafe_path"


@pytest.mark.parametrize('encrypted', [False, True])
def test_backup_cannot_include_run_reference_without_published_artifact(tmp_path, monkeypatch, encrypted):
    from threading import Event, Thread
    import hub.infrastructure.platform_backup as backup_module

    repo = PlatformRepository(tmp_path / 'platform.db')
    repo.init()
    repo.upsert_workspace(OWNER, {'workspace_id': 'home', 'root_path': str(tmp_path / 'workspace')})
    repo.create_conversation(OWNER, 'conv-race', title='', workspace_id='home')
    repo.append_turn(OWNER, 'conv-race', 'msg-race', 'run-race', text='publish',
                     client_token='race', config_snapshot={}, now=1)
    root = tmp_path / 'artifacts'
    root.mkdir()
    source = tmp_path / 'result.txt'
    source.write_text('published result')
    published = Event()
    attempted = Event()
    errors = []
    original_inventory = backup_module._read_artifact_inventory
    publishers = []

    def publish():
        try:
            attempted.set()
            item = ArtifactStore(root).put_file(OWNER, 'home', source)
            repo.set_run_state(OWNER, 'run-race', 'succeeded',
                               result_text=item['artifact_id'], now=2)
            published.set()
        except Exception as exc:
            errors.append(exc)

    def inventory_with_race(artifact_root):
        inventory = original_inventory(artifact_root)
        if artifact_root == root:
            thread = Thread(target=publish, daemon=True)
            publishers.append(thread)
            thread.start()
            assert attempted.wait(2)
            # Old implementation lets publication + DB commit overtake the
            # backup. A publication barrier keeps it pending until copy ends.
            published.wait(0.5)
        return inventory

    monkeypatch.setattr(backup_module, '_read_artifact_inventory', inventory_with_race)
    service = PlatformBackupService()
    backup = tmp_path / 'backup'
    restored = tmp_path / 'restored'
    try:
        if encrypted:
            key = bytes(range(32))
            service.create_encrypted_backup(repo.db_path, root, backup, key, 'test-key')
            assert service.inspect_encrypted_backup(backup, key=key)['verified'] is True
            service.restore_encrypted_backup(backup, restored, key)
        else:
            service.create_backup(repo.db_path, root, backup)
            service.restore_backup(backup, restored)
    finally:
        for thread in publishers: thread.join(5)
    assert not errors
    assert published.is_set()
    run = PlatformRepository(restored / 'platform.db').get_run(OWNER, 'run-race')
    if run['state'] == 'succeeded':
        assert ArtifactStore(restored / 'artifacts').read(OWNER, 'home', run['result_text']) == b'published result'
    else:
        assert run['state'] == 'queued'


def _publish_in_process(root, source, attempted, published):
    attempted.set()
    ArtifactStore(Path(root)).put_file(OWNER, 'home', Path(source))
    published.set()


def test_artifact_backup_barrier_excludes_separate_process_publication(tmp_path):
    import multiprocessing
    from tools.platform.artifact_lock import artifact_snapshot_barrier

    context = multiprocessing.get_context('spawn')
    attempted, published = context.Event(), context.Event()
    root, source = tmp_path / 'artifacts', tmp_path / 'result.txt'
    source.write_text('child artifact')
    child = context.Process(target=_publish_in_process, args=(str(root), str(source), attempted, published))
    try:
        with artifact_snapshot_barrier(root):
            child.start()
            assert attempted.wait(5)
            assert not published.wait(0.3)
        assert published.wait(5)
        child.join(5)
        assert child.exitcode == 0
        assert len(ArtifactStore(root).list(OWNER, 'home')) == 1
    finally:
        if child.is_alive():
            child.terminate()
            child.join(5)
