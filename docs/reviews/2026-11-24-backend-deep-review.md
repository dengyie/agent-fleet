# Backend Deep Review — 2026-11-24

## Scope and baseline

This review used the latest GitHub `main` SHA `8a94dd180ec438756ec9bf0c4b51cad65a063004` and focused on the backend operator session path: `SessionService`, `SupervisorService`, `SupervisorCommandRepository`, session event pagination, operator authorization, SQLite transaction boundaries, and their real HTTP/SQLite regression suites. The event-tail, directory snapshot, command response-loss, duplicate-message and interaction-answer paths were checked against the public contract and existing tests.

The review did not change frontend product code, run a real Agent CLI or external model, inspect production data, or claim Windows/macOS runtime validation for this database migration. The session and Supervisor integration suites passed before implementation (274 passed, 7 subtests); this established the starting behavior but did not cover SQL NULL primary-key semantics.

## Confirmed finding

### P2 — Ordinary message token digest was nullable in SQLite

`session_message_operations.token_digest` was declared as `TEXT PRIMARY KEY CHECK(length(token_digest)=64)`. In SQLite, a non-integer primary key in an ordinary rowid table does not imply `NOT NULL`; a `CHECK` expression that evaluates to `NULL` also passes. A direct in-memory SQLite probe confirmed the existing DDL accepts a `NULL` token digest. Application code hashes validated non-empty browser tokens, so the normal HTTP route did not submit NULL, but the persistence schema failed to enforce its own idempotency identity invariant.

The same migration checked only the column-name set, primary-key position and unique command index. A same-name table could therefore omit `NOT NULL`, operation/digest checks and the command foreign key while still being marked schema version 1 and accepted at startup.

## Change made

PR #122 initially advanced the operation schema marker to version 2 with `token_digest TEXT NOT NULL PRIMARY KEY` and stricter checks. After merge, review of the receiver rollback path showed the pre-PR122 reader rejects marker 2, while the receiver does not snapshot this separate outbox database. The follow-up returns the marker to v1 and uses insert/update triggers to reject NULL token digests while retaining the row shape accepted by the pre-PR122 reader. It validates and atomically converts the already-shipped v2 shape back to v1 while retaining valid rows. If rollback restores the PR122 reader, it can migrate the v1 shape forward again. Existing command rows remain untouched, and a failed migration rolls back table/schema changes.

The fix stays in the SQLite repository boundary. It does not change HTTP request/response shapes, message idempotency semantics, operator identity checks, command delivery, or the separate interactive-question `request_id` protocol.

### Follow-up migration edge cases

Review of the merged startup path found an unversioned legacy branch calling `.get()` on `sqlite3.Row`, which has no such method. A valid unversioned table could therefore prevent the Supervisor repository from starting. The migration now indexes the row and rejects a missing token column using the existing bounded repository error. Regression cases exercise both nullable v1 and already-strict v2 pre-versioned tables, repeat initialization against persisted rows, and ensure malformed pre-versioned tables are left unchanged.

The CHECK probes previously used constant digest values. In a populated table, a duplicate-value constraint could reject the probe even when the intended CHECK was absent, hiding a malformed schema. Validation now first inserts one valid row inside a savepoint and uses fresh random digests for each invalid probe. Tests populate v1 and v2 lookalike schemas with invalid existing values to prove absent operation/content checks are detected without rewriting rows. All probes roll back before startup completes.

The deployment receiver snapshots only the configured five databases in `deploy/auto-deploy.example.json`; the Supervisor outbox at the default `var/supervisor/commands.db` is outside that list. Its generic rollback fingerprint does not inspect `supervisor_meta`. Therefore a version bump in that separate database is not protected by the current receiver. The follow-up removes the v2 marker and returns persisted v2 stores to reader-compatible schema version 1. The already-completed PR #122 deployment succeeded, so no rollback was triggered; however, the deployment job did not prove rollback safety for that schema transition. Future outbox changes that cannot remain compatible with the prior reader require the installed receiver to snapshot, fingerprint, preserve aside, and restore this database; changing the repo alone does not update the preinstalled receiver.

## Existing architecture and coverage observed

- Ordinary message command creation and the token binding already share the Supervisor repository's `BEGIN IMMEDIATE` transaction, which gives concurrent identical submissions one durable operation and command.
- Token values are stored as digests; message bodies are represented by a content digest in the operation binding.
- The API retains a single replay-based recovery path and returns the original command's current receipt status.
- Event pagination uses the Hub ingestion cursor as identity and bounds each tail/before/after read.
- Session-directory pagination uses an immutable directory cursor and a snapshot ceiling, so updates and new sessions do not reorder an in-progress traversal.
- Adoption revocation is rechecked on first admission; exact same-operator retries can recover a command admitted before revocation without dispatching a new one.
- Interactive question answers continue using their independent request/token reservation and reconciliation path.

## Verification

The focused regression first failed against the baseline because the lookalike schema was accepted; a separate SQLite probe also observed NULL accepted by the old DDL.

- `PYTHONPATH=. python3 -m pytest tests/test_agent_evidence.py tests/test_session_access_consistency.py tests/test_native_session_api.py tests/test_session_interactions.py tests/test_supervisor_control.py tests/test_session_repository.py tests/test_transcript_repository.py -q` — **277 passed, 7 subtests passed**.
- `PYTHONPATH=. python3 -m pytest tests/test_boundaries.py tests/test_frontend_separation.py -q` — **21 passed**.
- `PYTHONPATH=. python3 -m pytest tests/test_assistant_vm_sessions.py tests/test_assistant_session_capabilities.py tests/test_native_chat_frontend.py -q` — **48 passed, 29 skipped** because this host has no Playwright/Chromium runtime.
- `ruff check --select E9,F63,F7,F82,F401 hub tools connectors fleet_core` — **passed**.
- `python3 -m compileall -q hub/infrastructure/supervisor_command_repository.py tests/test_native_session_api.py`, `git diff --check`, and UTF-8/no-BOM inspection — **passed**.
- `PYTHONPATH=. python3 -m pytest tests -q` — **8593 passed, 235 skipped, 204 subtests passed, 11 failed**. Nine `test_mcp_catalog_admin_browser` cases fail before browser startup because `FLEET_PLAYWRIGHT_MODULE` is unset and the Node fixture calls `require(undefined)` (local Node is v26.9.0; CI pins Node 22 and installs Playwright). Two `test_runtime_dependencies` cases fail because the isolated local Python environment lacks `websocket-client`. These failures are outside the changed code and need the configured CI dependency/browser environment for release evidence.
- Follow-up regressions for pre-versioned schema startup, collision-resistant SQL constraint probes, v2-to-v1 reader-compatible migration, atomic rollback on migration failure, and reserved trigger-name collision — **14 passed** after first reproducing 6 failures (two unversioned table starts raised `AttributeError`; four populated missing-CHECK schemas were incorrectly accepted).
- Re-run of the seven session/Supervisor suites after follow-up — **287 passed, 7 subtests passed**.
- Post-PR122 production check: main run [45306957639](https://github.com/dengyie/agent-fleet/actions/runs/45306957639) completed with Linux, Windows, macOS, package, and deploy jobs successful; `/healthz` returned `{"status":"ok"}` and `/manifest.json` reported `bcea9c37e1998c2cf073dfae8055d0da77c745d3`. This confirms deployment success, not rollback rehearsal.
- `shellcheck --shell=bash --severity=error deploy/*.sh` was unavailable (`shellcheck: command not found`); this change does not modify shell scripts.

Local verification covers SQLite semantics, HTTP contracts and schema migration on this host. It does not by itself prove all production OS or filesystem behavior.

## Review boundary and follow-up

This is a focused, evidence-based backend review, not a claim that every provider, platform adapter, workflow, or production integration was exhaustively audited. The broader service layering and message/event contracts have meaningful integration coverage; no speculative module-size-driven refactor was made. The review surfaced a deployment receiver gap that needs its own compatible receiver update and regression before future database schema migrations can be considered rollback-safe. Continue deeper review by risk surface with concrete failing evidence, especially node/runner cross-platform lifecycle and workflow recovery.
