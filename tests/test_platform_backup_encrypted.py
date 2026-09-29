import json
import sqlite3
from pathlib import Path

import pytest

from hub.infrastructure.platform_backup import (
    BackupError,
    BackupRetentionPolicy,
    PlatformBackupService,
)
from hub.infrastructure.platform_db import PlatformRepository
from tools.platform.artifacts import ArtifactStore


OWNER = "owner@example.test"
KEY_V1 = b"\x01" * 32
KEY_V2 = b"\x02" * 32


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
    source.write_text("encrypted backup payload", encoding="utf-8")
    artifact = ArtifactStore(artifact_root).put_file(OWNER, "workspace-a", source)
    return db_path, artifact_root, artifact


def _write_created_at(root: Path, value: float) -> None:
    path = root / "encrypted-backup.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["created_at"] = value
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")


def test_encrypted_backup_round_trip_requires_key_and_contains_no_plaintext_files(tmp_path):
    db_path, artifact_root, artifact = _source_pair(tmp_path)
    service = PlatformBackupService()
    encrypted = tmp_path / "encrypted-backup"

    created = service.create_encrypted_backup(
        db_path, artifact_root, encrypted, KEY_V1, "platform-key-v1"
    )
    assert created["encrypted"] is True
    assert created["key_id"] == "platform-key-v1"
    assert not (encrypted / "platform.db").exists()
    assert not (encrypted / "backup.json").exists()
    assert not list(encrypted.rglob("content"))
    assert not list(encrypted.rglob("manifest.json"))
    assert KEY_V1.hex() not in (encrypted / "encrypted-backup.json").read_text()

    metadata = service.inspect_encrypted_backup(encrypted)
    assert metadata["verified"] is False
    verified = service.inspect_encrypted_backup(
        encrypted, key=KEY_V1, expected_key_id="platform-key-v1"
    )
    assert verified["verified"] is True

    restored = tmp_path / "restored"
    restored_summary = service.restore_encrypted_backup(
        encrypted, restored, KEY_V1, expected_key_id="platform-key-v1"
    )
    assert restored_summary["artifact_count"] == 1
    db = sqlite3.connect(str(restored / "platform.db"))
    assert db.execute("SELECT COUNT(*) FROM workspaces").fetchone() == (1,)
    db.close()
    assert ArtifactStore(restored / "artifacts").read(
        OWNER, "workspace-a", artifact["artifact_id"]
    ) == b"encrypted backup payload"


def test_encrypted_backup_round_trip_with_empty_artifact_root(tmp_path):
    db_path = tmp_path / "platform.db"
    platform = PlatformRepository(db_path)
    platform.init()
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    service = PlatformBackupService()
    encrypted = tmp_path / "encrypted-empty"

    service.create_encrypted_backup(db_path, artifact_root, encrypted, KEY_V1, "key-v1")
    restored = tmp_path / "restored-empty"
    summary = service.restore_encrypted_backup(encrypted, restored, KEY_V1)

    assert summary["artifact_count"] == 0
    assert (restored / "artifacts").is_dir()


def test_encrypted_backup_rejects_wrong_key_key_id_and_ciphertext_tamper(tmp_path):
    db_path, artifact_root, _artifact = _source_pair(tmp_path)
    service = PlatformBackupService()
    encrypted = tmp_path / "encrypted-backup"
    service.create_encrypted_backup(db_path, artifact_root, encrypted, KEY_V1, "key-v1")

    with pytest.raises(BackupError) as key_id:
        service.inspect_encrypted_backup(encrypted, key=KEY_V1, expected_key_id="key-v2")
    assert key_id.value.code == "key_id_mismatch"

    with pytest.raises(BackupError) as wrong_key:
        service.restore_encrypted_backup(encrypted, tmp_path / "wrong", KEY_V2)
    assert wrong_key.value.code == "encryption_invalid"
    assert not (tmp_path / "wrong").exists()

    payload = next((encrypted / "payload").rglob("*.enc"))
    raw = bytearray(payload.read_bytes())
    raw[-1] ^= 0x01
    payload.write_bytes(raw)
    with pytest.raises(BackupError) as tampered:
        service.inspect_encrypted_backup(encrypted, key=KEY_V1)
    assert tampered.value.code == "ciphertext_hash_mismatch"


def test_encrypted_manifest_and_payload_symlink_are_rejected_without_escape(tmp_path):
    db_path, artifact_root, _artifact = _source_pair(tmp_path)
    service = PlatformBackupService()
    encrypted = tmp_path / "encrypted-backup"
    service.create_encrypted_backup(db_path, artifact_root, encrypted, KEY_V1, "key-v1")

    payload = next((encrypted / "payload").rglob("*.enc"))
    payload.unlink()
    payload.symlink_to(db_path)
    with pytest.raises(BackupError) as symlink:
        service.inspect_encrypted_backup(encrypted)
    assert symlink.value.code == "unsafe_path"

    service.create_encrypted_backup(db_path, artifact_root, tmp_path / "encrypted-path", KEY_V1, "key-v1")
    path_backup = tmp_path / "encrypted-path"
    manifest_path = path_backup / "encrypted-backup.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../escape"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BackupError) as traversal:
        service.inspect_encrypted_backup(path_backup)
    assert traversal.value.code == "invalid_encrypted_manifest"

    service.create_encrypted_backup(db_path, artifact_root, tmp_path / "encrypted-extra", KEY_V1, "key-v1")
    extra_backup = tmp_path / "encrypted-extra"
    (extra_backup / "unexpected.txt").write_text("must reject", encoding="utf-8")
    with pytest.raises(BackupError) as extra:
        service.inspect_encrypted_backup(extra_backup)
    assert extra.value.code == "invalid_encrypted_manifest"


def test_encrypted_restore_never_overwrites_and_cleans_failed_plaintext_stage(tmp_path):
    db_path, artifact_root, _artifact = _source_pair(tmp_path)
    service = PlatformBackupService()
    encrypted = tmp_path / "encrypted-backup"
    service.create_encrypted_backup(db_path, artifact_root, encrypted, KEY_V1, "key-v1")

    target = tmp_path / "restored"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(BackupError) as existing:
        service.restore_encrypted_backup(encrypted, target, KEY_V1)
    assert existing.value.code == "destination_exists"
    assert marker.read_text(encoding="utf-8") == "keep"

    with pytest.raises(BackupError) as failed:
        service.restore_encrypted_backup(encrypted, tmp_path / "failed", KEY_V2)
    assert failed.value.code == "encryption_invalid"
    assert not list(tmp_path.glob(".failed.*"))


def test_retention_is_dry_run_bounded_and_skips_unmarked_or_symlink_entries(tmp_path):
    db_path, artifact_root, _artifact = _source_pair(tmp_path)
    service = PlatformBackupService()
    root = tmp_path / "backups"
    root.mkdir()
    for index in range(4):
        target = root / f"backup-{index}"
        service.create_encrypted_backup(db_path, artifact_root, target, KEY_V1, "key-v1")
        _write_created_at(target, 100.0 + index)

    (root / "unmarked").mkdir()
    (root / "unmarked" / "keep.txt").write_text("keep", encoding="utf-8")
    (root / "link").symlink_to(root / "backup-0", target_is_directory=True)

    policy = BackupRetentionPolicy(keep_last=2, max_age_s=None, max_total_bytes=None)
    plan = service.plan_retention(root, policy, now=200.0)
    assert plan["apply"] is False
    assert plan["delete"] == ["backup-0", "backup-1"]
    assert "backup-2" in plan["retain"] and "backup-3" in plan["retain"]
    assert {item["name"] for item in plan["skipped"]} == {"unmarked", "link"}
    assert (root / "backup-0").exists()

    applied = service.prune_retention(root, policy, now=200.0, apply=True)
    assert applied["deleted"] == ["backup-0", "backup-1"]
    assert not (root / "backup-0").exists()
    assert not (root / "backup-1").exists()
    assert (root / "backup-2").exists()
    assert (root / "backup-3").exists()
    assert (root / "unmarked").exists()
    assert (root / "link").is_symlink()


def test_retention_age_and_byte_limits_are_deterministic_and_keep_last_wins(tmp_path):
    db_path, artifact_root, _artifact = _source_pair(tmp_path)
    service = PlatformBackupService()
    root = tmp_path / "backups"
    root.mkdir()
    for index in range(3):
        target = root / f"backup-{index}"
        service.create_encrypted_backup(db_path, artifact_root, target, KEY_V1, "key-v1")
        _write_created_at(target, 100.0 + index * 10)

    age_plan = service.plan_retention(
        root,
        BackupRetentionPolicy(keep_last=1, max_age_s=15.0, max_total_bytes=None),
        now=130.0,
    )
    assert age_plan["delete"] == ["backup-0", "backup-1"]
    assert age_plan["retain"] == ["backup-2"]

    # The newest backup is retained by keep_last even when it exceeds the
    # byte budget; older backups are selected in deterministic age/name order.
    newest_bytes = sum(path.stat().st_size for path in (root / "backup-2").rglob("*.enc"))
    policy = BackupRetentionPolicy(
        keep_last=1,
        max_age_s=200.0,
        max_total_bytes=newest_bytes,
    )
    plan = service.plan_retention(root, policy, now=130.0)
    assert plan["delete"] == ["backup-0", "backup-1"]
    assert plan["retain"] == ["backup-2"]
    assert plan["delete_bytes"] > 0


def test_retention_rejects_unbounded_policy_values(tmp_path):
    with pytest.raises(BackupError) as keep:
        BackupRetentionPolicy(keep_last=10001, max_age_s=None, max_total_bytes=None)
    assert keep.value.code == "invalid_retention_policy"
    with pytest.raises(BackupError) as age:
        BackupRetentionPolicy(keep_last=1, max_age_s=-1, max_total_bytes=None)
    assert age.value.code == "invalid_retention_policy"
