# Workflow Historical Budget Link Scan Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification checkpoints. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make workflow initialization validate and repair every historical dispatch row, including explicit zero and negative SQLite rowids, without changing admission or budget semantics.

**Architecture:** Keep the repair inside `WorkflowRepository._init_budget_links`, the existing SQLite initialization transaction. Replace the positive-rowid lower bound with a nullable keyset cursor: the first page has no predicate, later pages use indexed keyset traversal, and each page holds at most 128 rows. Existing per-row indexed attribution checks and the all-or-nothing initialization transaction remain unchanged.

**Tech Stack:** Python 3.10+, stdlib `sqlite3`, pytest, existing WorkflowRepository schema.

## Global Constraints

- Preserve the primary worktree and all unrelated dirty files.
- Keep the existing `binding_run_id`/budget reservation consistency checks unchanged.
- Read and repair at most 128 dispatch rows per page; never load the full history.
- Use UTF-8 without BOM and retain original exception causes.
- No schema, dependency, admission, or API changes.

---

### Task 1: Capture the historical-row regression

**Files:**
- Modify: `tests/test_workflow_repository.py`
- Modify: `hub/infrastructure/workflow_repository.py`

**Interfaces:**
- `WorkflowRepository.init()` continues to be the public repair entry point.
- `_init_budget_links(connection)` keeps its existing side effects for rows with `binding_run_id`.

- [x] Add a regression that creates a valid bound dispatch, clears its budget child link, moves its SQLite rowid to `-1` and `0`, and asserts a fresh `repository.init()` restores the link and creates one budget check for both cases.
- [x] Run the focused regression before changing production code and record the failure.
- [x] Change only the initial cursor predicate: first page uses no `WHERE`, subsequent pages use `rowid>?` from the last returned row.
- [x] Re-run the regression and the workflow repository/budget suites.
- [x] Exercise signed 64-bit rowid extremes, empty history, exact 128-row boundaries and multiple pages; repeated initialization preserves completed budget-check progress.
- [x] Verify missing/mismatched budget attribution at negative, zero and positive rowids fails closed and rolls back all repairs from earlier pages.
- [x] Register all three regression selectors in the existing required WF-08 journey.

### Task 2: Verification and scope record

- [x] Run Python compilation and `git diff --check` for the changed files.
- [x] Record the focused result in the implementation progress document without changing unrelated acceptance counts.
- [x] Confirm no external I/O, schema migration, or deletion occurred.
- [x] Run serial Python 3.10/3.13 related suites with existing browser dependencies and retain JUnit evidence: **836 passed + 30 passing subtests** on each interpreter, no skips (`/tmp/agent-fleet-workflow-history-related-python310-20261025.xml`, `/tmp/agent-fleet-workflow-history-related-python313-20261025.xml`).
- [x] Run the unchanged strict full journey gate: **5268 passed, 5 Linux `/proc`/pidfd skips, 195 passing subtests; 41/42 journeys**. `WF-06` is **302/681** and `WF-08` is **165/333**; `RELEASE-01` remains incomplete solely for Linux-only selectors. Seven external checks remain `not_run`. Evidence: `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-workflow-history-full-20261025-nv114p_9`.
- [x] Check all touched files, including untracked ones, for UTF-8/no-BOM, trailing whitespace and broken local Markdown links. Preflight reports **854 source files, 852 UTF-8 text files, 600 Python AST files, 223 local links, 22 CJS checks, 8 modified JS module checks, compileall on Python 3.10/3.13, and diff check passed** (`/tmp/agent-fleet-workflow-history-preflight-20261025.json`).
