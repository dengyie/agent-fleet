# Windows Session Control Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make Windows terminate controls converge on a verified process-tree exit and make terminal supervisor receipts converge the corresponding Hub session summary.

**Architecture:** Keep the existing bounded supervisor receipt protocol. Extend the receipt fan-in with an optional session repository callback, and make the Windows backend retain a usable handle until termination is verified while treating taskkill/liveness failures as degraded outcomes.

**Tech Stack:** Python, pytest/unittest, Windows `taskkill.exe`, SQLite session repository.

## Global Constraints

- Work only in the isolated `codex/windows-agent-fleet-fixes` worktree.
- Preserve the primary worktree's existing Muse changes.
- Use only an isolated test PID for Windows online checks; do not stop existing CLI processes or restart the ControlClient service.
- Keep public identifiers and receipt reasons bounded; never expose PID, path, token, credential, or raw exception text.

### Task 1: Reproduce Windows termination failure

**Files:**
- Modify: `tests/test_supervisor_process.py`

- [x] Add a deterministic WindowsGroupOps test double covering a failed graceful `taskkill` followed by a successful forced `taskkill`, including liveness polling after the forced command.
- [x] Run the focused test and capture the current failure before implementation.

### Task 2: Preserve session state convergence on terminal receipts

**Files:**
- Modify: `hub/application/control_router.py`
- Modify: `hub/bootstrap.py`
- Test: `tests/test_task_cancel_supervisor.py`

- [x] Add regression tests for successful receipt projection, failed/unverified receipt behavior, and retry after an atomic projection failure.
- [x] Add an optional `session_repo` dependency to `ControlRouter` and update the terminal receipt path with a bounded close/status projection.
- [x] Verify bootstrap already injects `session_repo` behind the existing feature gates; no bootstrap change was needed.
- [x] Run focused router/session tests.

### Task 3: Fix Windows termination verification

**Files:**
- Modify: `tools/supervisor/supervisor.py`
- Test: `tests/test_supervisor_process.py`

- [x] Make the Windows backend distinguish a missing/invalid PID from a process that remains alive after taskkill, and keep the private handle until the group is verified gone.
- [x] Ensure forced termination returns `terminated_forced` only after a positive gone check; otherwise return `escape_unverified` without pretending cleanup completed.
- [x] Keep unknown process-table, access, and wait results conservative; reject attach/pause/resume when process identity or tree membership cannot be verified.
- [x] Run supervisor lifecycle and Windows-focused tests.

### Task 4: Full verification

- [x] Run the focused regression tests, then the complete relevant supervisor/session test files.
- [x] Run `python3 -m compileall` for changed Python modules and `git diff --check`.
- [x] Re-check the live Hub health/status and Windows ControlClient service read-only; do not deploy or restart.

## Verification Record (2026-10-19)

- Full suite: `3406 passed, 24 skipped, 165 subtests passed`.
- Live Hub: `/healthz` returned HTTP 200; anonymous `/api/status` and `/api/sessions` returned HTTP 401.
- Windows: `AgentFleetControlClient` reported `RUNNING` and `AUTO_START`. Existing Codex/ZCode processes were only enumerated.
- Production was not deployed, and the Windows service was not restarted.
