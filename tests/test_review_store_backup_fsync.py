"""Review regression: published backups are fsynced (files, dirs, parent)."""

import os

import pytest

import hub.infrastructure.platform_backup as platform_backup
from hub.infrastructure.platform_backup import PlatformBackupService
from tests.test_platform_backup_encrypted import KEY_V1, _source_pair


@pytest.fixture
def synced(monkeypatch):
    inodes = set()
    real = os.fsync

    def record(fd):
        inodes.add(os.fstat(fd).st_ino)
        return real(fd)

    monkeypatch.setattr(platform_backup.os, "fsync", record)
    return inodes


def _expect_durable(root, synced):
    paths = [root, root.parent, *root.rglob("*")]
    missing = [str(p) for p in paths if not p.is_symlink() and p.stat().st_ino not in synced]
    assert missing == []


def test_plain_backup_and_restore_are_fsynced(tmp_path, synced):
    db_path, artifact_root, _artifact = _source_pair(tmp_path)
    service = PlatformBackupService()
    backup = tmp_path / "out" / "backup"
    service.create_backup(db_path, artifact_root, backup)
    _expect_durable(backup, synced)

    synced.clear()
    restored = tmp_path / "restored" / "pair"
    service.restore_backup(backup, restored)
    _expect_durable(restored, synced)


def test_encrypted_backup_and_restore_are_fsynced(tmp_path, synced):
    db_path, artifact_root, _artifact = _source_pair(tmp_path)
    service = PlatformBackupService()
    encrypted = tmp_path / "out" / "encrypted"
    service.create_encrypted_backup(db_path, artifact_root, encrypted, KEY_V1, "key-v1")
    _expect_durable(encrypted, synced)

    synced.clear()
    restored = tmp_path / "restored" / "pair"
    service.restore_encrypted_backup(encrypted, restored, KEY_V1)
    _expect_durable(restored, synced)
