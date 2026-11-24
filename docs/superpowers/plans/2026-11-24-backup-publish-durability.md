# Backup Publication Durability Implementation Plan

> **For agentic workers:** Execute this plan inline in the current isolated worktree. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden two backend persistence boundaries: published backup durability reporting and deployment rollback detection of SQLite DDL changes whose version marker was not advanced.

**Architecture:** Keep backup publication in `hub.infrastructure.platform_backup._publish_new_directory`. Preserve the no-overwrite rename and staged file/tree flush; return one bounded `BackupError` code for a post-rename parent-directory flush failure so callers inspect the destination before retrying. Extend the deployment receiver's SQLite schema fingerprint with canonical `sqlite_master` rows so rollback detects actual structural changes even if an application version marker is unchanged.

**Tech Stack:** Python 3.10+, pathlib, SQLite fixtures, pytest.

## Global Constraints

- Do not modify `frontend/` product code.
- Keep destination publication atomic and no-overwrite on supported platforms.
- Never include filesystem paths or raw OS exception text in `BackupError`.
- Keep all changed files UTF-8 without BOM.

---

### Task 1: Reproduce the post-rename durability ambiguity

**Files:**
- Test: `tests/test_platform_backup.py`

**Interfaces:**
- Exercise `_publish_new_directory(stage, target)` and `PlatformBackupService.create_backup()` using the existing temporary filesystem/SQLite fixtures.
- Inject failure only when flushing the destination parent after rename; keep staged file and directory flushes functional.

- [x] Add a regression asserting the current implementation reports generic `publish_failed` even though the complete target has already appeared.
- [x] Run the targeted test and record the baseline behavior.

### Task 2: Expose a bounded published-but-unconfirmed result

**Files:**
- Modify: `hub/infrastructure/platform_backup.py`
- Test: `tests/test_platform_backup.py`
- Test: `tests/test_platform_backup_encrypted.py`

**Interfaces:**
- `_publish_new_directory(stage: Path, target: Path) -> None` raises `BackupError("publish_durability_unconfirmed")` only when rename succeeded but parent-directory fsync failed.
- The code contains no path, OS message, credential, or database detail; the destination is retained for inspection.

- [x] Change only the post-rename parent flush failure classification.
- [x] Verify plain backup, plain restore, encrypted backup, and encrypted restore keep the complete destination and report the same bounded result under injected parent-fsync failure.
- [x] Verify pre-rename flush and no-overwrite races retain their existing behavior.
- [x] Reproduce same-version SQLite DDL escaping rollback detection, compare persistent schema objects in the receiver fingerprint, and preserve the failed database alongside the restored pre-deploy database.

### Task 3: Record and verify the contract

**Files:**
- Modify: `docs/testing/README.md`
- Modify: `docs/reviews/2026-11-24-backend-deep-review.md`
- Modify: `deploy/auto-deploy.md`
- Modify: canonical Obsidian `Note/Infra/agent-fleet 部署与运维.md` on vault `master`.

- [x] Document that `publish_durability_unconfirmed` means the destination exists and must be inspected before retry.
- [x] Document that automatic rollback detects both version markers and SQLite schema-object changes, while preserving same-schema data writes.
- [x] Run the backup/deployment focused suites, `git diff --check`, and UTF-8/no-BOM checks.
- [x] Run Obsidian `doc-check` and confirm both worktrees are clean or contain only this task's changes. Final combined backup/deployment slice: **129 passed, 3 expected Darwin skips, 2 subtests passed**.

## Acceptance

- A pre-rename failure leaves the destination absent.
- A post-rename parent-directory flush failure keeps a fully valid destination and returns `publish_durability_unconfirmed`.
- Retries do not overwrite an existing destination.
- Error responses remain bounded and do not disclose paths or OS exception text.
- A DDL change without a version bump selects the database for rollback; a data-only write with the same schema does not.
