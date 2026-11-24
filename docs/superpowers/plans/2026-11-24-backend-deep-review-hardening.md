# Backend Deep Review Hardening Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task, inline. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the confirmed nullable-token and migration-validation defects in the Supervisor ordinary-session-message idempotency store and record the backend review evidence.

**Architecture:** Preserve the existing `hub -> application/domain/repository` dependency direction and the Supervisor repository's atomic `BEGIN IMMEDIATE` migration. Validate the persisted schema at the repository boundary so malformed legacy databases fail closed before command admission or receipt recovery.

**Tech Stack:** Python 3.10+, SQLite, Flask HTTP integration tests, pytest.

## Global Constraints

- Modify backend and backend documentation only; do not edit `frontend/` product code.
- Preserve existing command, idempotency, authorization, and public HTTP contracts.
- Keep all SQLite migration checks inside the existing write transaction and leave no committed probe rows.
- Use UTF-8 without BOM and do not delete user or production data.
- Run targeted migration and session API tests, relevant Supervisor tests, `git diff --check`, and encoding checks.

---

## Review Scope

The reviewed production path is ordinary operator session-message recovery through `SessionService.send_message`, `SupervisorService.find_session_message`, and `SupervisorCommandRepository`. Existing real HTTP/SQLite tests cover exact replay, terminal receipt recovery, concurrent duplicate submission, token conflicts, revocation, response loss, and rollback. Two repository defects are confirmed: `TEXT PRIMARY KEY` plus `CHECK(length(token_digest)=64)` still accepts SQL `NULL`, and migration validation checks column names, the token primary key, and the unique command index without checking all required `NOT NULL`, `CHECK`, and foreign-key invariants.

## Task 1: Reproduce and Reject a Lookalike Corrupt Schema

**Files:**
- Modify: `tests/test_native_session_api.py`
- Modify: `hub/infrastructure/supervisor_command_repository.py`

**Interfaces:**
- Keep `SupervisorCommandRepository.init()` as the sole migration entry point.
- Preserve `SupervisorCommandRepositoryError("command_unavailable")` as the bounded failure contract.
- Upgrade the independent operation schema from version 1 to version 2 atomically; preserve valid version-1 rows and reject malformed rows without data loss.

- [x] **Step 1: Add regressions for the nullable key and a lookalike corrupt version-1 table.**

Create a valid pre-fix version-1 table with a valid persisted message operation and assert the upgrade preserves the row, makes `token_digest` explicitly `NOT NULL`, and rejects a subsequent NULL key. Separately, replace a version-1 table with a lookalike table that has all six expected column names, a token primary key, and a unique command index but lacks the other constraints. Assert a fresh `init()` raises `SupervisorCommandRepositoryError` and does not rewrite the version or existing command rows.

- [x] **Step 2: Run the new test and confirm the current implementation accepts the corrupt schema.**

Run: `PYTHONPATH=. python3 -m pytest tests/test_native_session_api.py::test_supervisor_rejects_lookalike_message_schema_missing_constraints -q`

Expected before the fix: FAIL because the version-1 migration accepts the lookalike schema, does not require a non-NULL key, and does not upgrade the schema version.

- [x] **Step 3: Validate column declarations, uniqueness, and command foreign-key shape.**

Set the operation schema version to 2 and create `token_digest TEXT NOT NULL PRIMARY KEY`. Validate every field declaration, `command_id` single-column uniqueness, and the exact `command_id -> supervisor_commands.command_id` foreign key. Accept only a valid version-1 schema with the known nullable key defect; atomically rebuild it and copy its rows into the strict version-2 table. Reject any invalid version-1 lookalike before rebuilding.

- [x] **Step 4: Verify SQL CHECK behavior without persisting probe rows.**

Within a savepoint in the existing migration transaction, insert a temporary valid parent command and attempt operation rows that independently violate token-digest length, operation value, and content-digest length. Require NULL token acceptance only while validating a valid v1 table, then require NULL rejection for v2. Every other invalid insert must raise `sqlite3.IntegrityError`; roll back the savepoint whether each attempted row is rejected or accepted. Reject the migration if any constraint probe has the wrong result. Keep command/message/audit data untouched after savepoint rollback.

- [x] **Step 5: Run migration regressions and session API tests.**

Run: `PYTHONPATH=. python3 -m pytest tests/test_native_session_api.py tests/test_session_repository.py tests/test_supervisor_control.py -q`

Expected: all tests pass, including old-schema atomic migration, malformed schema rejection, concurrent message replay, response-loss recovery, revoked-session behavior, and SQLite rollback.

## Task 2: Record Review Result and Verification

**Files:**
- Create: `docs/reviews/2026-11-24-backend-deep-review.md`
- Modify: `docs/native-session-api.md`
- Modify: `docs/testing/README.md`

- [x] **Step 1: Document the finding and exact repository-boundary remedy.**

Record the confirmed migration-validation finding, why it can poison future message writes, the minimal fix, test evidence, review scope, and limits. Distinguish confirmed code facts from areas not externally exercised.

- [x] **Step 2: Extend the authoritative session API migration note.**

State that the Supervisor message-operation schema checks its column constraints, digest/operation CHECK behavior, unique command index, and command foreign key; malformed schema fails closed atomically.

- [x] **Step 3: Link the regression entry from the testing index.**

Keep the existing native-session and Supervisor suite command as the runnable entry point; add the migration-hardening regression to the described coverage.

- [x] **Step 4: Run final verification.**

Run the scoped test command from Task 1, `git diff --check`, and a Python script that checks every modified UTF-8 file has no UTF-8 BOM. Inspect `git status --short` and `git diff --stat` to confirm no frontend or unrelated product changes.

## Acceptance

- A corrupt version-1 lookalike operation table is rejected during `init()` with the existing bounded repository error.
- A valid version-1 operation table upgrades atomically to version 2 and preserves existing operations and commands; older pre-feature databases still initialize atomically.
- The version-2 table rejects a NULL idempotency digest at SQLite level.
- Migration validation leaves no probe command or message-operation rows after success or failure.
- Existing ordinary-message idempotency, interactive-answer protocol, and authorization behavior remain unchanged.
- The focused backend test suites pass and the diff contains no frontend product files.
