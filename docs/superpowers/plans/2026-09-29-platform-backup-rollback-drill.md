# Platform Encrypted Backup and Rollback Drill

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to execute this plan task-by-task with verification checkpoints.

**Goal:** Exercise the platform's encrypted backup, integrity inspection, atomic restore, and rollback boundary against the isolated pxed test instance without touching production.

**Architecture:** Read the running test instance's SQLite database and artifact root through the existing `PlatformBackupService` contract. Create an encrypted backup in a temporary directory, inspect it with and without the temporary key, restore it into a new empty target, verify the restored database/artifact pair, then remove all drill-only material. No Supervisor, Nginx, Cloudflare, provider, Komari, Node, or service-action gate is changed.

**Tech Stack:** Python 3, repository `.venv`, `hub.infrastructure.platform_backup`, SSH to pxed, isolated `/data/agent-fleet-platform-test`.

## Global Constraints

- Operate only on `/data/agent-fleet-platform-test` on pxed.
- Never write to the HK production deployment or its credentials.
- Use a temporary key that is not persisted in the repository or an Obsidian note.
- Do not register Supervisor or change any feature gate.
- Restore only to a new temporary target; never overwrite the running test database.
- Record only bounded counts, sizes, hashes/statuses, and paths needed for repeatability; do not record key material or database contents.

### Task 1: Capture the isolated instance boundary

**Files:**
- Read: `/data/agent-fleet-platform-test/run.pid`
- Read: `/data/agent-fleet-platform-test/var/platform/platform.db`
- Read: `/data/agent-fleet-platform-test/logs/web.log`

- [x] Verify the process is the expected loopback-only command and the database/log paths exist.
- [x] Record the current PID and a bounded log/error count before the drill (`795927`, `2754` lines, `0` error lines).

### Task 2: Run encrypted backup and inspection

**Interfaces:**
- `PlatformBackupService.create_encrypted_backup(source_db, source_artifacts, output_dir, key, key_id=...)`
- `PlatformBackupService.inspect_encrypted_backup(backup_dir, key=None)`

- [x] Generate a process-local random AES key and non-secret drill key id.
- [x] Create the encrypted backup in a temporary pxed directory.
- [x] Inspect the public manifest without a key and fully validate/decrypt it with the temporary key.
- [x] Confirm the manifest contains no key material or plaintext content.

### Task 3: Restore into a new target and verify rollback safety

**Interfaces:**
- `PlatformBackupService.restore_encrypted_backup(backup_dir, target_dir, key=...)`

- [x] Restore into a new temporary target.
- [x] Re-inspect the restored pair and compare bounded file names, sizes, and hashes with the backup manifest.
- [x] Confirm the running source database remains unchanged and the target is atomically published.
- [x] Exercise no-overwrite behavior by attempting a second restore to the same target and confirming the stable failure (`destination_exists`).

### Task 4: Clean up and document evidence

**Files:**
- Modify: `docs/platform/agent-fleet-self-hosted-platform.md`
- Modify: Obsidian `01.项目/agent-fleet/agent-fleet 文档索引.md`
- Modify: Obsidian `Note/Infra/agent-fleet 部署与运维.md`
- Modify: Obsidian `00.MOC/AI-DOC-ROUTER.md` only if the existing route needs a new trigger.

- [x] Remove the temporary key, backup, restore target, and helper script from pxed.
- [x] Confirm the test instance remains healthy and its log has no unexpected errors.
- [x] Record the drill result, non-goals, and remaining production requirements.
- [x] Run focused regression, full `pytest -q`, `git diff --check`, `doc-lookup`, and `scan-stale-docs`.

## Verification Record

The first pxed run exposed `artifact_root_missing` when decrypting a backup with zero artifacts. Root cause: encrypted file restoration recreated files but not the required empty `artifacts/` directory. `_decrypt_to_plain` now creates that bounded directory before validation, and `tests/test_platform_backup_encrypted.py` covers the zero-artifact round trip. The repaired drill reported `created_verified=true`, `public_verified=false`, `decrypted_verified=true`, `restored_schema=1`, `file_count=2`, `second_restore=destination_exists`, `key_material_in_manifest=false`, `plaintext_content_in_manifest=false`, `source_db_unchanged=true`, and cleaned all temporary material.
