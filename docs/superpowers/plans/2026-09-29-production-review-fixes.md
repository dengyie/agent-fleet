# Production Review Root-Cause Fixes

**Goal:** Resolve the six confirmed production review defects on `codex/platform-review-fixes`.

**Architecture:** Use the existing sandbox for both Hub and Node execution. SQLite owns message ordering across supported code rollbacks. Persist Run results once and use bounded lifecycle events. SSR and SPA share the existing SSE client.

**Tech stack:** Python, SQLite, Flask, browser ES modules, Bash, pytest and Node fixtures.

## Constraints

- Work only in the isolated review worktree; preserve original worktrees.
- No remote deployment, feature-gate enablement, or push.
- Use disposable databases, keys, files and loopback servers for verification.
- Preserve rollback to the pre-sequence writer: omitted `turn_sequence` must allocate an ordered value.
- Root cause and failing evidence already exist in the production review; promote the reproductions into portable repository tests before implementation.

## Execution

### 1. Node isolation

- [x] Add a signed NodeRuntime regression for an outside-workspace read without a launcher; run it and confirm the sentinel leaks before the fix.
- [x] In `tools/platform/node_runtime.py`, replace `DirectoryBackend(config.workspace_root)` with `SandboxBackend(config.workspace_root, launcher=config.sandbox_launcher)` and add a validated immutable argv field to `NodeRuntimeConfig`.
- [x] Verify missing-launcher denial, configured-launcher wiring, capability/signature tests and confined file operations.

### 2. Release dependency closure

- [x] Add a test that executes `deploy/package-release.sh`, extracts the archive and starts an independent app process with platform enabled and disabled; confirm both fail before the fix.
- [x] Include `platform_schema.py` in `deploy/package-release.sh` and the local copy in `deploy/e2e-smoke.sh`; include shared frontend assets in the smoke copy when SSR imports them.
- [x] Verify archive startup and the self-contained local smoke script.

### 3. Database-owned turn sequence

- [x] Reproduce old-writer inserts after migration, including two new turns and another upgrade, without depending on private Git history in the portable test.
- [x] In `hub/infrastructure/platform_db.py`, add schema version 2 and reject future schemas before mutation. In the migration transaction, repair zero sequences after existing positive values and install an `AFTER INSERT ... WHEN NEW.turn_sequence=0` trigger assigning `MAX(turn_sequence)+1` within the conversation.
- [x] Remove application-side sequence allocation from `append_turn`; all ordinary inserts use the same database rule. Keep the unique order index.
- [x] Verify upgrade/rollback/re-upgrade, repaired zero sequence, independent conversations, concurrent writers and future-schema rejection.

### 4. Unknown execution versus cancellation

- [x] Promote the real signed Node write/lost receipt/cancel reproduction.
- [x] In `finish_run`, cancellation must not replace `unknown`: `if row['cancel_requested'] and state != 'unknown': state = 'cancelled'`.
- [x] Verify ordinary cancellation still works and interrupted operations retain unknown outcomes in durable state and events.

### 5. Bounded completion events

- [x] Promote the real provider-adapter test returning 22,000 CJK characters; add emoji and JSON-escape boundaries.
- [x] Remove full result text from both worker adapters' `run_finished` payloads. Keep the full answer in `runs.result_text`, already used by conversation projection. Render event status in the assistant event list.
- [x] Verify completion, persisted answer, next-turn context and bounded events, including the nonleased worker adapter.

### 6. Shared SSE lifecycle

- [x] Promote a browser-module fixture exercising default SSR disconnect/reconnect, replay, deduplication and shutdown; confirm the current implementation keeps the stale source.
- [x] Convert `hub/static/app.js` to a module importing existing `SseClient` and `FleetStore`. Keep SSR rendering/task handlers, replace its transport and polling with the shared client, and stop it on page unload.
- [x] In `hub/http/pages.py`, expose only the four shared dependency modules under `/static/frontend/` with exact allowlisting and path confinement. Set the template script to `type=module`.
- [x] Remove the old SSR EventSource, timestamp cursor and unbounded polling implementation. Verify asset routes with cutover disabled, module imports, resume from the last sequence, log/terminal replay, stale-frame rejection and cleanup.

## Completion evidence

- [x] Run targeted regression suites after each attributable fix.
- [x] Run the entire pytest suite and the local deployment smoke test.
- [x] Check changed Python/JavaScript/Bash syntax and `git diff --check`.
- [x] Review final diff, update the platform contract and this evidence record, and commit locally after verification.


## Verification record — 2026-09-29

- Before implementation, the seven automated reproductions failed (the release defect has two gate cases); the earlier broad suite had passed, confirming that these failures needed behavior-level regression coverage.
- `tests/test_production_review_regressions.py` adds 19 portable cases covering all six findings. They use disposable signing keys, databases, workspaces, provider responses and a Node VM loading the real frontend modules.
- Full suite: `python -m pytest -q -p no:cacheprovider` with `PYTHONDONTWRITEBYTECODE=1`: **2074 passed, 2 skipped, 154 subtests passed** in 70.65 seconds. One existing invalid-escape docstring warning remains in `tools/probe/discovery.py`; it is unrelated to these fixes.
- Final focused regression after strengthening terminal-event and SSR restoration assertions: **158 passed, 2 subtests passed**. This includes Node runtime/HTTP, Run workers, conversation ordering, defaults, scheduling, SSE, rollback, backups, XSS and release layout.
- An additional local reproduction loaded the actual pre-sequence repository implementation from commit `17ad21a`, performed upgrade / two rollback writes, and passed. Checked-in tests model that insert contract without requiring private Git history.
- `deploy/e2e-smoke.sh`: **SMOKE OK**. It exercised a local Hub, signed ingest, idempotent task creation, real local runner subprocess/result diff, SSE, frontend release assets and independent route assembly.
- Changed Python files parse successfully; changed JavaScript/MJS pass `node --check`; deployment scripts pass `bash -n`; `git diff --check` passes.
- The first broad focused run exposed two stale test fixtures: the simulated old schema must drop its new trigger before dropping the column, and the smoke copy assertion must include the new dependency closure. Both fixtures were corrected and included in the passing final run.

The release script archives committed `HEAD`. After the local fix commit, repeat clean archive extraction/startup with platform off and on, and verify the default SSR page plus all four shared module responses before handoff.

## Result and limits

All six reproduced code defects are fixed. Removed the duplicate SSR EventSource/timestamp-cursor/polling implementation, application-side message sequence allocation and full-answer completion-event payload. No speculative compatibility layer or new transport abstraction was added.

No production environment, feature gate, provider, Node or service was changed. The denying launcher fixture proves configuration wiring only; a real administrator-owned isolation launcher still needs deployment validation. SSR verification uses actual modules in a Node VM and real Flask asset routes, not a real-browser end-to-end run.
