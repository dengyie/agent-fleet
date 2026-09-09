# Task 16 / Task 17 — Release-Gate Ledger Report

**Date:** 2026-08-24
**Branch:** `arch-v2-primary`
**HEAD verified:** `de85dde` (`test: validate standalone frontend release`)
**Scope:** Documentation-only reconciliation of the Task 16 and Task 17 release gates. No repository files outside the two ledger files were modified; only read-only git commands were run.

## Commit existence (verification)

Confirmed present in git history via `git log` / `git show --oneline -s`:

| Commit | Subject |
|---|---|
| `3e35e7b` | `fix: Task 16 — task view fencing, terminal class allowlist, cutover shell` |
| `25c79bc` | `fix: serve static assets during frontend cutover` |
| `de85dde` | `test: validate standalone frontend release` (Task 17 HEAD commit) |

Also confirmed: Task 16 initial implementation `c4bebd9` (`feat: add standalone task view and shell handoff`), on top of Task 15 final fix `a92f8d3`.

## Task 16 — complete

- Brief: `task-16-brief.md`. Counted deliverables: `frontend/views/task.js` with `mountTask(root, taskId, store, client)`, compatibility shell for Flask task pages during cutover, and same-process static asset serving under `FRONTEND_CUTOVER`.
- Commits: implementation `c4bebd9`, review fix `3e35e7b`, asset fix `25c79bc` (all verified in history).
- Asset fix detail (`task-16-asset-fix-report.md`, 2026-08-23): `hub/http/pages.py` gains a catch-all route active only when `FRONTEND_CUTOVER` is enabled, with an explicit release-layout allowlist and traversal rejection; focused `tests.test_task_view_review` + `tests.test_release_layout` = `33/33 OK`; `compileall hub tests` exit 0; `node --check frontend/views/task.js` exit 0; `git diff --check` clean.
- Reviewer verdict: none recorded in the SDD artifacts. `task-16-review.diff` exists but no `task-16-report.md` and no PASS/FAIL verdict was captured; completion is recorded from commits and test evidence only, without inventing a verdict.
- **Task 16: complete.**

## Task 17 — complete

- Task: `task-17-brief.md` Phase 3 gate. Created `deploy/test-static-frontend.sh`; added `test_frontend_has_required_modules` to `tests/test_release_layout.py`; documented the gate in `docs/HANDOFF.md`.
- Commit: `de85dde` (`test: validate frontend release`) — verified; it is the current HEAD.
- Recorded evidence (`task-17-report.md`, 2026-08-23): focused release-layout suite `7/7 OK`; `bash deploy/test-static-frontend.sh` → `STATIC OK (all 11 required paths 200)`, re-run green, no leftover `http.server` processes; `compileall -q ...` OK; `bash -n` and `git diff --check` OK.
- Full suite `unittest discover -s tests` recorded as 384 tests with **2 pre-existing failures**, both reproduced with the Task 17 test change stashed (so pre-dating the task, both in hub routing, untouched by Task 17):
  1. `test_http_contracts.ApiEdgeErrorContractTests.test_unknown_api_route_404_is_bounded_json` — 405 != 404 (router route-availability behavior)
  2. `test_regressions.IngestApiSecurityTests.test_scan_endpoint_requires_post_and_token` — 404 != 405 (GET on POST-only route)
- `deploy/e2e-smoke.sh` deliberately not run as part of Task 17.
- **Task 17: complete.**

## Working-tree ruling — uncommitted four-file regression

Read-only `git status` at this ledger update shows, on top of committed HEAD `de85dde`, uncommitted changes to exactly four files:

- `docs/HANDOFF.md`
- `frontend/views/task.js`
- `hub/bootstrap.py`
- `hub/config.py`

Ruling recorded in `progress.md`: this uncommitted four-file regression is **not** part of the committed Task 16/17 release; it remains preserved in the working tree pending explicit cleanup. This ledger pass did not stage, revert, or modify those four files.

`.playwright-mcp/` is pre-existing (untracked) and was not touched.

## Verification performed (read-only)

- `git show --oneline -s 3e35e7b 25c79bc de85dde` — all three exist as documented.
- `git log --oneline -8` — confirms the commit ordering (a92f8d3 → c4bebd9 → 3e35e7b → 25c79bc → de85dde).
- `git status --short` — confirms the four uncommitted files and untracked `.playwright-mcp/`; used only to record the ruling.
- No commands that write to the repository were run.

## Concerns

1. No Task 16 reviewer verdict exists in the artifacts; completion status relies on verified commits plus the asset-fix report's recorded test evidence.
2. The two pre-existing full-suite failures are open questions for the follow-on controller work, not part of Task 16/17.
3. The uncommitted four-file regression (docs/HANDOFF.md, frontend/views/task.js, hub/bootstrap.py, hub/config.py) is preserved as ruled, pending explicit cleanup — it is still present in the working tree.