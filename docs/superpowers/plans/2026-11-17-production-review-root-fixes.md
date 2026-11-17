# Production Review Root Fixes Implementation Plan

> **For agentic workers:** Use executing-plans to implement this plan inline, task by task.

**Goal:** Fix the four reproduced production review defects and deploy through the existing verified CI chain.

**Architecture:** Preserve application service boundaries. Serialize Node reference admission and retirement in platform.db; feed only proven pre-effect errors back to the model; paginate transcripts by durable ingestion order; describe activity according to the actual operation.

**Tech Stack:** Python, Flask, SQLite, vanilla JavaScript, pytest, GitHub Actions.

## Global Constraints

- UTF-8 without BOM; preserve production data and historical receipts.
- Reuse this isolated worktree; rebase onto latest origin/main before merge.
- No arbitrary model execution cap and no automatic replay of uncertain writes.
- Use dedicated acceptance data, preserving real users' sessions and settings.

### Task 1: Atomic Node retirement and reference admission

Files: `hub/infrastructure/platform_db_catalog.py`, `service_repository.py`, `plugin_repository.py`; `hub/application/plugin_service.py`; `hub/http/node_routes.py`; `tests/test_node_retirement.py`.

- [x] Add failing tests for service/enabled-plugin bindings, fresh re-enrollment, stale authenticated reports and receipt observations, concurrent reference admission.
- [x] Run `PYTHONPATH=. python -m pytest tests/test_node_retirement.py -q` and capture failures.
- [x] Block bound services/enabled plugin targets; clear disabled plugin targets and derived caches in the removal transaction. Serialize service/target writes; check credential generation when admitting Node reports/observations.
- [x] Rerun Node, service and plugin suites; commit the independently verified fix.

### Task 2: Recoverable management failures

Files: `hub/platform/runtime/native.py`, `hub/domain/assistant_admin.py`, `tests/test_native_tool_recovery.py`, `tests/test_assistant_project_admin.py`.

- [x] Add a real Worker CAS-conflict/re-read/correct test, checkpoint recovery without replay, and terminal forbidden/unknown/cancel cases.
- [x] Capture red tests, then use exact operation/error pairs for proven pre-effect rejection. The model chooses the correction; the runtime never retries the tool itself.
- [x] Run `PYTHONPATH=. python -m pytest tests/test_native_tool_recovery.py tests/test_assistant_project_admin.py -q`; commit.

### Task 3: Durable event cursors

Files: `hub/infrastructure/transcript_repository.py`; session application/HTTP/tool contracts; `frontend/assets/api/{client,contracts}.js`; `frontend/assets/views/session.js`; transcript/session/assistant/frontend regression tests.

- [x] Add red coverage for equal source sequences across streams, late low-sequence ingestion, restart/migration and duplicate ingestion.
- [x] Introduce an additive monotonic ingestion index, transactionally backfill existing records, and paginate by cursor. Source sequence remains event metadata and deep-link identity.
- [x] Update assistant prompt, HTTP/JS DTOs and all in-repository pagination callers together; remove obsolete pagination parameters.
- [x] Run transcript, query, interaction, assistant, frontend contract/browser suites; commit.

### Task 4: Truthful activity feedback

Files: `hub/domain/tool_activity.py`, `tests/test_assistant_project_admin.py`.

- [x] Prove reads falsely claim persistence, then distinguish inspection from successful runtime/hosts mutation. Failed receipts never claim a save.
- [x] Verify read/write/failure activity tests; commit.

### Task 5: Release and acceptance

Files: `docs/testing/journeys.json`, platform guide, README, release notes; existing canonical Obsidian runbook.

- [ ] Register meaningful regressions in journey coverage; run required local checks and inspect diff.
- [ ] Rebase onto latest main, push feature branch, create PR, require matching-head CI success, merge, follow main deployment to completion.
- [ ] Verify exact source/manifest identity, public APIs, original failure scenarios using isolated fixtures, provider/tool streaming, and SQLite integrity.
- [ ] Record actual results and any external limits in existing canonical documentation; report deployed SHA and evidence.

## Verification evidence

- Baseline reproduction: `~/.cache/agent-fleet/verification/review-dfa3040-20261117/`. Four Node tests, seven management cases, three cursor tests, four activity cases and the delayed-summary assertion failed before their corresponding fixes.
- Fresh targeted regression: 179 passed, 3 subtests passed. Browser/session contracts: 28 passed, including live arrival, two-page tied sequences, refresh and source-sequence deep links at mobile/desktop widths.
- Full release gates and exact production acceptance remain mandatory in Task 5; local checks alone do not establish deployment.
