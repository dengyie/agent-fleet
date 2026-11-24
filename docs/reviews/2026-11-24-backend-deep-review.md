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

### Continued review — backup publication and SQLite rollback detection

Review continued from public main `978a0a489913ee99cc9860d1cad2174633d14886` and narrowed to the platform backup publication helper and the deployment receiver's database rollback decision.

Two reachable persistence ambiguities were reproduced with temporary filesystem/SQLite state:

- **P2 — Backup publication error hid an already visible destination.** `_publish_new_directory()` performs an atomic no-replace rename and then flushes the destination's parent directory. If that final `fsync` failed, it returned the same `publish_failed` as a pre-rename failure. The destination actually existed and was complete, so an operator could treat the failure as unpublished, retry, and receive `destination_exists`. The helper now raises `publish_durability_unconfirmed` for this exact post-rename state. It retains the destination, exposes no path/OS error, and callers must inspect/validate the exact target before retrying. Pre-rename failure still leaves the destination absent.
- **P2 — Deployment rollback missed SQLite DDL without a version bump.** `database_schema_fingerprint()` compared `user_version`, platform `meta.schema_version`, and workflow-domain version markers only. A failed release could add a column while leaving those markers unchanged; rollback restored old code but retained the incompatible live database. A real deployment fixture reproduced this: old code returned `rolled_back` with the added column still in the live database. The fingerprint now also compares persistent `sqlite_master` schema objects. When DDL differs, rollback moves the failed database and sidecars to `failed-databases/`, restores the pre-deploy snapshot, and records `restored_databases`; schema-stable data writes continue to remain online.

These changes do not alter application API DTOs or application database schemas. Updating the repository receiver does not update the separately installed host receiver; that deployment boundary remains explicit in `deploy/auto-deploy.md` and must be checked independently before claiming the receiver change is live.

Regression evidence:

- Both new regressions failed before their fixes: the post-rename flush returned `publish_failed`; an unversioned DDL migration survived old-code rollback.
- `PYTHONPATH=. python3 -m pytest tests/test_platform_backup.py tests/test_platform_backup_encrypted.py tests/test_review_store_backup_fsync.py tests/test_auto_deploy.py tests/test_review_rollback.py tests/test_deploy_pidfd.py tests/test_guardian_process_control.py tests/test_release_layout.py -q` — **129 passed, 3 skipped, 2 subtests passed**. The three skips are expected Darwin-only platform boundaries. Coverage includes plain/encrypted create and restore, post-rename flush fault injection, pre-rename failure, no-overwrite publication, DDL rollback with failed-database preservation, schema-stable data-write retention, deployment rollback, pidfd control, Guardian and release layout.
- `ruff check --select E9,F63,F7,F82,F401 deploy/auto_release.py hub/infrastructure/platform_backup.py tests/test_auto_deploy.py tests/test_platform_backup.py tests/test_platform_backup_encrypted.py` — **passed**.

This is a focused follow-up, not a claim that every Runner, provider, deployment host, or filesystem failure mode has been exhaustively audited. Production evidence for this candidate is pending its public CI and receiver-specific rollout; no real Agent CLI or external model was run.

The next review boundary remains platform-specific Runner lifecycle and production backup/restore behavior on the separately installed receiver.

### Continued review — Supervisor outbox deployment snapshot coverage

The deployment transaction snapshots only SQLite paths listed in the root-owned receiver configuration. `SupervisorCommandRepository` defaults to `var/supervisor/commands.db`, but the checked-in `deploy/auto-deploy.example.json` listed only five other databases. This is a reachable configuration omission: if a future release migrates the outbox schema and then fails health verification, the receiver cannot compare or restore that database because it is absent from the configured path set. Existing deployment rollback tests exercise restoration only for configured database paths; the missing sample entry was reproduced by a regression that failed against the prior example.

The example now includes the default Supervisor outbox path. Deployment documentation and the test entry require existing `/etc/agent-fleet-deploy.json` receiver configurations to be checked independently because application deploys do not update the separately installed receiver or its root configuration. The test asserts the checked-in template includes the path; the existing real SQLite deployment tests verify configured DDL changes are preserved aside and restored on rollback.

This change cannot establish the contents of the production host's root-owned config or update it. The next production deployment must separately confirm `var/supervisor/commands.db` is listed and the installed receiver contains the DDL fingerprint/restore behavior before treating outbox rollback as protected.

### Continued review — Windows orphaned process recovery

Windows Supervisor starts children inside a Job Object configured to survive Supervisor death. After restart, however, the durable owner record contains only the root PID/start identity and the new Supervisor has no handle to the unnamed Job Object. When that root had exited, `_rebind_owner()` immediately returned `no_live_process`; the control client maps that result to `already_finished`, and Hub receipt projection closes the managed session. Windows descendants may outlive their root, so this could mark the session finished while an Agent child remains running and no longer controllable through the recovered session. A focused regression reproduced the incorrect `no_live_process` outcome with a dead owner and a live descendant in the Windows process table.

Recovery now checks the Windows process tree when the root is gone. It returns `liveness_unknown` if descendants remain, a child liveness query is uncertain, or process enumeration fails. It returns `no_live_process` only when enumeration succeeds and no descendant is alive. This preserves the existing fail-closed receipt behavior without signaling a process whose original Job Object ownership cannot be re-established.

The bounded fake-process regression covers live orphan, no remaining descendant, and failed enumeration on all CI hosts; the dedicated Windows CI remains the runtime authority for Win32 APIs. A safe cross-restart kill capability would require a durable/reopenable Job Object ownership design and is outside this bounded repair.

### Continued review — concurrent Supervisor manifest persistence

Runner and the standalone control client can each restore a Supervisor over the same `manifest_dir`. `_persist()` previously wrote every session to the fixed `<session_id>.json.tmp` path before `os.replace()`. Two concurrent state updates could truncate or replace that shared temporary file; a deterministic two-instance regression synchronized both replacements and reproduced one `manifest_write_failed` after one writer consumed the temp path. The adjacent `HandlesMixin._write_owner()` used the same fixed temporary name for the owner identity needed by cross-instance process rebinding; a second synchronized regression reproduced one lost `os.replace` after the shared temp was consumed.

Manifest and owner persistence now share one private Supervisor file-writing helper. It exclusively creates a uniquely named temporary file beside the target, writes UTF-8, closes it, and atomically replaces the target. It best-effort removes a temporary file after write/replace failure; manifest writes still expose only the bounded `manifest_write_failed` error while owner writes remain best-effort. This protects atomic publication from filename collisions; it does not merge stale in-memory manifests, so last successful atomic replacement remains the winner if independent instances write different states concurrently.

- Both concurrency regressions failed against their fixed-temp implementations (manifest: one `manifest_write_failed`; owner: one swallowed `FileNotFoundError`), then passed after the fix. A separate injected replace failure verifies the original manifest bytes remain intact and no temporary file leaks.
- The three focused manifest/owner persistence regressions — **3 passed**.
- `PYTHONPATH=. python3 -m pytest tests/test_review_runner_control.py tests/test_round5_windows_process.py tests/test_supervisor_process.py tests/test_supervisor_control.py tests/test_native_continuation.py tests/test_agy.py tests/test_acp_lifecycle_races.py -q` — **229 passed, 2 skipped, 2 subtests passed**.
- The manifest and owner concurrency regressions, replacement-failure regression, and Windows orphan-liveness regression are explicitly listed in the Windows/macOS CI jobs in addition to the Linux full suite. The first Windows CI attempt exposed an over-synchronized test that made simultaneous destination replacement part of the assertion; the test now synchronizes concurrent writes but serializes the actual replace call, isolating temp-name collisions from platform-specific destination replacement behavior.
- Targeted Ruff, compileall, `git diff --check`, and UTF-8/no-BOM inspection — **passed**. The test uses controlled process data; no real Agent CLI or external model was run.

## Review boundary and follow-up

This is a focused, evidence-based backend review, not a claim that every provider, platform adapter, workflow, or production integration was exhaustively audited. The broader service layering and message/event contracts have meaningful integration coverage; no speculative module-size-driven refactor was made. Continue deeper review by risk surface with concrete failing evidence, especially node/runner cross-platform lifecycle, workflow recovery and backup/restore interactions.
