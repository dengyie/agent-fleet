# Native Task API Hardening Implementation Plan

> **For agentic workers:** Use executing-plans inline. This backend change is independent of the active frontend PR #109. Checkboxes record verified progress, not proposed functionality.

**Goal:** Expose the registered machine launch directory and make retrying an accepted task submission recover its original identity without admitting a different request under the same token.

**Architecture:** HTTP delegates to the existing `TaskService`. The service validates input, tries read-only recovery before current launch policy, and emits an event only for a new task. The SQLite repository compares immutable request fields and internal origins both during recovery and inside the serialized create transaction; task gates retain the original confirmation requirement. No new task database, executor, or frontend policy is introduced.

**Tech Stack:** Existing Python, Flask, SQLite, pytest; no added dependencies.

## Global Constraints

- Reuse the clean linked worktree on `codex/native-task-api-hardening`, based on `origin/main` `d882c5c`.
- Do not edit the active frontend PR #109 worktree or its files.
- All text uses UTF-8 without a BOM; preserve tasks, native sessions, cancellation fences, handoff receipts, and legacy origins.
- Token identity remains globally unique in the existing schema. Another actor or payload gets a generic conflict, never the original task body.
- Original tasks can be recovered after policy/availability changes. Fresh submissions still require online, available, registered targets.
- Use ordinary operator/admin authentication. Ingest, Runner, Supervisor and ordinary account credentials cannot read the launch directory.
- No vendor CLI or production model invocation is required for this Hub-only change; Mac/Windows client bundles stay unchanged.
- Rebase latest main, pass exact-revision CI, merge and follow the existing production deployment and evidence checks.

## File Responsibilities

| File | Responsibility |
|---|---|
| `hub/http/task_routes.py` | Authenticated read-only `GET /api/machines/<machine>/task-targets`; stable application errors |
| `hub/domain/task.py` | Token syntax and pure immutable replay-field validation |
| `hub/application/task_service.py` | Recover original submission before applying new-task policy; map conflicts to 409 |
| `hub/infrastructure/task_repository.py` | Consistent read-only recovery, atomic request binding, original gate/internal origin evidence |
| `tests/test_native_task_api.py` | Real HTTP/SQLite auth, recovery, conflicts, races, missing targets and degraded storage |
| `tests/test_services.py` | Keep the explicit repository test double aligned with the recovery port |
| `docs/native-task-api.md` | Public contract and recovery/error semantics |
| `docs/testing/README.md` | Link the contract and regression command |

### Task 1: Registered Target Directory

Consumes: `TaskService.targets(machine) -> dict` with `machine`, `projects`, `observed_at`, and registry-derived `agents[family].installed` in `true/false/null`.
Produces: authenticated GET of that same DTO. Unknown valid machine names return the existing service's empty/unknown directory. The directory requires observation/configuration, not the task SQLite store.

- [x] Add HTTP regressions against a disposable Hub; require 200 for the public directory and no executable paths, keys or unprojected snapshot fields.
- [x] Run `python3 -m pytest tests/test_native_task_api.py -q` and retain the missing-route failures.
- [x] Add the thin route using the existing operator decorator and `error_response`; do not copy discovery policy into Flask.
- [x] Verify directory auth for anonymous/foreign-domain credentials and ordinary/admin account sessions, invalid-machine errors, no writes and task-store degradation.

### Task 2: Recoverable, Bound Task Submissions

Consumes: `validate_task_input`'s canonical request, the authenticated actor and optional internal handoff/legacy origins.
Produces: `SqliteTaskRepository.find_task_submission(**submission) -> dict | None`; lookup has no writes. Both lookup and create compare `requested_by`, `machine`, `agent_type`, `project`, `instruction`, original `confirm`, and internal origin bindings.

- [x] Add regressions showing replay after lost-response/offline/uninstalled/unregistered changes, field/actor conflicts, invalid token types, original gate decisions, concurrent equal/conflicting submissions and cancellation preservation.
- [x] Re-run the new regression file before changing implementation; retain actual failing output.
- [x] Validate nonempty tokens as strings of at most 128 characters. Omitted/null/empty tokens retain the existing no-token behavior.
- [x] Factor repository recovery and transactional creation through the same matching helper. Use an explicit read transaction for consistent multi-table recovery and the existing `BEGIN IMMEDIATE` writer transaction for creation.
- [x] Invoke recovery before current launch policy. A successful replay returns the original task/state/session projection with `created=false`; conflict maps to `idempotency_conflict`/409 with no request content.
- [x] Verify one task, one creation audit and one queue event for duplicate/racing requests; no policy bypass for new tokens, no cancellation fence removal, no new attempt on replay.
- [x] Verify legacy task bridge, handoff, Workflow recovery, task lifecycle, domain/service contracts and account boundaries.

### Task 3: Release and Production Evidence

- [x] Document the public DTO, replay semantics and retained limits in `docs/native-task-api.md`, linked from `docs/testing/README.md`.
- [x] Run focused regressions, Python lint, compile checks and `git diff --check`. Inspect the complete scoped diff for privacy, transaction and architectural regressions.
- [ ] Rebase latest `origin/main`, push, open a PR and require the exact revision's Linux, Windows and macOS CI results.
- [ ] Merge only after checks pass, follow main packaging/deployment, verify deployed SHA and the same-run artifacts plus authenticated read-only directory behavior.
- [ ] Update the existing canonical operations runbook/project index with deployment, rollback and evidence boundaries. Preserve all prior evidence.

## Baseline Evidence

Before implementation: `tests/test_task_api.py tests/test_task_store.py tests/test_assistant_agent_tasks.py tests/test_task_cancel_races.py tests/test_handoff.py` — **145 passed** on local macOS/Python 3.14. This is a baseline, not verification of the new behavior.

## Implementation Evidence

- Pre-fix new regressions: **39 failed / 3 passed**, preserved in the local verification directory.
- Focused Hub/task/account/lifecycle/Workflow regression batch: **336 passed**.
- Extended task/database/Workflow coordinator/capability batch, including original bound-session recovery: **155 passed**. Batches overlap and are not added into a total.
- Python lint, changed-module compilation, UTF-8/no-BOM and diff checks passed.
- Review preserved specialized handoff conflicts, original gate decisions, internal origin ownership and cancellation fences. No schema migration, new dependencies, frontend changes or node client changes.
