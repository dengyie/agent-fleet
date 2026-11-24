# Backend Deep Review — 2026-11-24

## Scope and baseline

This review used the latest GitHub `main` SHA `8a94dd180ec438756ec9bf0c4b51cad65a063004` and focused on the backend operator session path: `SessionService`, `SupervisorService`, `SupervisorCommandRepository`, session event pagination, operator authorization, SQLite transaction boundaries, and their real HTTP/SQLite regression suites. The event-tail, directory snapshot, command response-loss, duplicate-message and interaction-answer paths were checked against the public contract and existing tests.

The review did not change frontend product code, run a real Agent CLI or external model, inspect production data, or claim Windows/macOS runtime validation for this database migration. The session and Supervisor integration suites passed before implementation (274 passed, 7 subtests); this established the starting behavior but did not cover SQL NULL primary-key semantics.

## Confirmed finding

### P2 — Ordinary message token digest was nullable in SQLite

`session_message_operations.token_digest` was declared as `TEXT PRIMARY KEY CHECK(length(token_digest)=64)`. In SQLite, a non-integer primary key in an ordinary rowid table does not imply `NOT NULL`; a `CHECK` expression that evaluates to `NULL` also passes. A direct in-memory SQLite probe confirmed the existing DDL accepts a `NULL` token digest. Application code hashes validated non-empty browser tokens, so the normal HTTP route did not submit NULL, but the persistence schema failed to enforce its own idempotency identity invariant.

The same migration checked only the column-name set, primary-key position and unique command index. A same-name table could therefore omit `NOT NULL`, operation/digest checks and the command foreign key while still being marked schema version 1 and accepted at startup.

## Change made

The Supervisor message-operation schema now advances atomically from version 1 to version 2. Version 2 declares `token_digest TEXT NOT NULL PRIMARY KEY`; validates text declarations, nullability, primary-key and unique-index shape, the command foreign key, and the observable operation/digest check behavior; and uses rollback-only savepoints for temporary constraint probes. A valid v1 table is validated and rebuilt with its rows preserved. A malformed lookalike is rejected before rewrite. Existing command rows remain untouched, and a failed migration rolls back table/schema changes.

The fix stays in the SQLite repository boundary. It does not change HTTP request/response shapes, message idempotency semantics, operator identity checks, command delivery, or the separate interactive-question `request_id` protocol.

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
- `shellcheck --shell=bash --severity=error deploy/*.sh` was unavailable (`shellcheck: command not found`); this change does not modify shell scripts.

Local verification covers SQLite semantics, HTTP contracts and schema migration on this host. It does not by itself prove all production OS or filesystem behavior.

### Follow-up migration edge cases

Review of the merged startup path found an unversioned legacy branch calling `.get()` on `sqlite3.Row`, which has no such method. A valid unversioned table could therefore prevent the Supervisor repository from starting. The migration now indexes the row and rejects a missing token column using the existing bounded repository error. Regression cases exercise nullable v1 and already-strict pre-versioned tables, repeat initialization against persisted rows, and ensure malformed pre-versioned tables are left unchanged.

The SQL CHECK probes previously used constant digest values. In a populated table, a duplicate-value constraint could reject a probe even when the intended CHECK was absent, hiding a malformed schema. Validation now first inserts a valid row inside a savepoint and uses fresh random digests for each invalid probe. Tests populate v1/v2 lookalike schemas with invalid existing values to prove absent operation/content checks are detected without rewriting rows. Probes roll back before startup completes.

Deployment compatibility was checked against the immediate rollback reader (the PR #122 code on current main): this follow-up keeps the deployed v2 marker and strict column contract, so the previous reader continues to open the database. The separate Supervisor outbox is not among the receiver's default five snapshots, so automatic database rollback is not established for a future incompatible outbox migration. This change leaves the on-disk v2 schema unchanged for databases already migrated by PR #122; future incompatible outbox changes must extend the installed receiver's snapshot/fingerprint/restore handling before release.

- Follow-up pre-versioned-schema and collision-resistant constraint-probe regressions — **7 passed** after first reproducing 6 failures (two unversioned table starts raised `AttributeError`; four populated missing-CHECK schemas were incorrectly accepted).
- Re-run of the seven session/Supervisor suites after follow-up — **284 passed, 7 subtests passed**.
- Main run [45306957639](https://github.com/dengyie/agent-fleet/actions/runs/45306957639) for PR #122 completed Linux, Windows, macOS, package and deploy jobs successfully. `/healthz` returned `{"status":"ok"}` and `/manifest.json` reported `bcea9c37e1998c2cf073dfae8055d0da77c745d3`.

### Continued review — command receipt and approval idempotency

The follow-up review started from current `main` `e0519406cb17837ec1196c1da7fdcb44dd77b82e`. It found no new production defect in the checked Supervisor command-receipt transition, ordinary-message replay, task-submission replay, event cursor, or session-directory pagination paths. A missing test for two competing terminal receipts was closed with a deterministic SQLite regression: two handlers read the queued command before racing `succeeded` against `failed`; exactly one reports `first=true`, both return the committed winner, and one terminal audit event is recorded.

The review also found two reachable P2 approval-admission/recovery failures. First, `ApprovalRepository.create()` performed its owner/idempotency-key lookup and INSERT without one write transaction. Two simultaneous HTTP requests with the same owner, service, action, and key could both observe no grant; one INSERT succeeded and the other hit the unique index, which the repository collapsed into `approval_store` (HTTP 503). The original repository test synchronized both pre-insert reads and reproduced one successful grant plus one `approval_store` error. The fix serializes lookup/replay and insert using `BEGIN IMMEDIATE`, explicitly commits the matching replay and create branches, and rolls back bounded conflict/storage failures.

Second, approval consumption spans two durable SQLite repositories. If the Hub stopped after persisting grant state `consuming` but before `CommandRepository.enqueue()` persisted its command/outbox row, an HTTP retry saw a grant without a command and returned `grant_not_pending`; the same state also made concurrent approval requests race into 409. A test constructed this exact crash point in SQLite and reproduced the 409 after app recreation. Recovery now accepts an already `consuming` grant only with its deterministic command ID, revalidates the registered service version/action/argument digest, and retries the fixed idempotent command enqueue. Approval decisions return the existing approved/consuming/consumed record for repeated approve requests. Same-command consumption remains idempotent, while a consuming grant that has expired is durably marked expired with zero remaining uses and is not dispatched. A failed enqueue handler no longer changes a `consuming` grant back to `approved`; another concurrent handler may have committed the same deterministic command/outbox, so replay observes and finalizes that one operation instead of reopening admission. The owner-scoped key, approval state gate, unique command/outbox identities, fixed arguments, and HTTP DTOs are unchanged.

Real SQLite and HTTP regressions force two identical requests to race through grant creation and two approve decisions to race after both read pending. They assert HTTP 200, one grant ID, one command ID, one durable command/outbox, and consumed terminal state. A separate recovery test creates `consuming` without a command, recreates the Hub application, and proves retry persists the deterministic command. Existing approval/service-action tests continue to cover action binding, owner separation, service-version fencing, expiry, and node receipts.

- Earlier count correction: the seven session/Supervisor suites on `e051940` report **284 passed, 7 subtests passed**, not 287.
- Competing Supervisor terminal receipt regression: **1 passed**; full `tests/test_supervisor_control.py`: **98 passed**.
- Approval/storage and service-action race/crash regressions passed, including both sides of the command/grant commit gap; six related suites: **135 passed**.
- Seven session/Supervisor suites after the receipt regression: **477 passed, 29 skipped, 55 subtests passed** on the current test environment.
- Workflow dispatch/coordinator/control, source-delivery recovery, coding delivery, review execution, and test execution regression suites: **263 passed**. These exercise durable dispatch leases, lost receipts, cancellation, uncertain external execution, task/Workflow split-commit recovery, and replay fencing; they do not establish that every workflow/provider path was exhaustively reviewed.
- The initial PR CI caught a Python 3.10 compatibility regression in the new receipt test (`datetime.UTC`); it was replaced with `timezone.utc` and locally verified against the Python 3.10 suite.
- PR [#124](https://github.com/dengyie/agent-fleet/pull/124) merged as `978a0a489913ee99cc9860d1cad2174633d14886`. Its Linux full suite reported **8854 passed, 2 warnings, 204 subtests**, 47/47 journeys and 2830 selectors; Windows reported **1020 passed, 44 skipped**; macOS reported **630 passed, 2 skipped, 103 subtests**.
- Main run [45353378940](https://github.com/dengyie/agent-fleet/actions/runs/45353378940) completed Linux, Windows, macOS, package, and deploy successfully. Production returned `{"status":"ok"}` from `/healthz`; `/manifest.json` matched the full main SHA; the same-run frontend verifier checked **107 resources**. Receiver receipt was `deployed/succeeded`, rollback anchor `/var/lib/agent-fleet-deploy/45353378940-qwejodsk`. Evidence is retained in `/Users/mango/.cache/agent-fleet/verification/backend-approval-20261124/`.
- No real Agent CLI or external model ran. No frontend product code, database schema, node client package, or deployment logic changed. External model/vendor acceptance remains unrun.

The next review boundary is platform-specific Runner lifecycle and deployment backup/restore interaction. This focused review does not claim exhaustive coverage of all providers, adapters, workflows, or production integrations.

## Review boundary and follow-up

This is a focused, evidence-based backend review, not a claim that every provider, platform adapter, workflow, or production integration was exhaustively audited. The broader service layering and message/event contracts have meaningful integration coverage; no speculative module-size-driven refactor was made. Continue deeper review by risk surface with concrete failing evidence, especially node/runner cross-platform lifecycle, workflow recovery and backup/restore interactions.
