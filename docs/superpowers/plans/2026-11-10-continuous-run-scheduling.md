# Continuous Run Scheduling Implementation Plan

> **For agentic workers:** Use executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Keep free Run slots occupied while slow executions continue, and dispatch work from free workspaces when an older workspace is busy.

**Architecture:** Preserve the existing Run leases and durable worker slots. The scheduler maintains a bounded set of in-flight futures; the repository filters busy workspaces while selecting eligible queued Runs. Capacity races retain the existing fenced requeue path. Synchronous run_once remains available for deterministic callers; the background lifecycle performs nonblocking admission and completion collection.

**Tech Stack:** Python 3.10+, Flask composition, SQLite WAL, ThreadPoolExecutor, pytest.

## Global Constraints

- Work in the existing codex/functional-root-fixes linked worktree, preserving previous edits.
- Keep Run attempt fencing, unknown recovery, owner fairness, conversation ordering and durable global/workspace limits.
- Never run model/tool IO inside a database write transaction.
- All text uses UTF-8 without BOM. No dependency download, deployment, merge or push.
- This plan is one component of the [full development goal](../../platform/development-completion.md); its completion does not complete Workflow F0–F6.

## Task 1: Prove the two scheduling failures

**Files:** Modify `tests/test_platform_run_scheduler.py`.

**Interfaces:** Existing `_queued`, `_scheduler`, real `PlatformRepository` and injected `ModelResponse` provider.

- [x] Add a background lifecycle regression with three distinct workspaces: slow A waits on an Event, B returns, C must enter before A is released. Use `try/finally` to release A and stop the scheduler.
- [x] Add a persistent-slot regression: reserve workspace A in SQLite, enqueue older A and newer B for one owner, and assert `tick_once` executes B without increasing A's attempt.
- [x] Add cross-owner fairness when one owner's remaining workspace is busy; the free owner's Run must dispatch within one bounded admission pass.
- [x] Run `PYTHONPATH=. /usr/local/bin/python3 -m pytest tests/test_platform_run_scheduler.py -q`; record the expected failures before production edits.

The background regression observes the behavior boundary:

```python
assert slow_entered.wait(2)
assert next_entered.wait(2), 'free slot was not refilled while slow Run continued'
assert repository.get_run(owner, slow_id)['state'] == 'running'
```

The capacity regression checks persistent state:

```python
result = scheduler.tick_once()
assert result['run_id'] == 'b-free'
assert result['state'] == 'succeeded'
assert repository.get_run(owner, 'a-busy')['attempt'] == 0
```

## Task 2: Filter busy workspaces before claiming a Run

**Files:** Modify `hub/infrastructure/platform_db_runs.py` and `hub/application/run_scheduler_service.py`.

**Interfaces:** Add optional `max_workspace_concurrency: int | None = None` to `claim_run`; unchanged callers retain their current behavior. Scheduler passes its effective workspace limit. Workspace key remains `owner_id + ':' + workspace_id`, with config snapshot taking precedence over the conversation workspace.

- [x] Join the conversation in the candidate query and filter queued candidates against active `platform_worker_slots` using the indexed workspace key and `expires_at > now`.
- [x] Keep expired running/cancelling recovery eligible even if a workspace is occupied, so capacity cannot suppress crash/cancellation reconciliation.
- [x] Preserve final atomic slot admission and fenced requeue if another process acquires capacity after candidate selection.
- [x] Select the workspace key from the existing claim rather than reloading the full conversation just for capacity routing.
- [x] Run the Task 1 persistent-slot tests and existing conversation-order/recovery tests.

The candidate condition is applied only when a scheduling limit is supplied:

```sql
AND (r.state != 'queued' OR (
  SELECT COUNT(*) FROM platform_worker_slots s
  WHERE s.workspace_key = r.owner_id || ':' || COALESCE(
    json_extract(r.config_snapshot, '$.workspace_id'), c.workspace_id)
    AND s.expires_at > ?
) < ?)
```

## Task 3: Continuously admit and collect bounded futures

**Files:** Modify `hub/application/run_scheduler_service.py` and its tests.

**Interfaces:** Private admission/collection helpers own the executor and future registry. `run_once(max_ticks)` preserves finite synchronous behavior; `start` cycles without waiting for unrelated futures.

- [x] Use one bounded future registry for background execution; collect completed futures before admitting available capacity.
- [x] Bound one owner scan, skipping an idle owner within that scan; do not spin repeatedly on a busy owner or schedule duplicate Run IDs.
- [x] Preserve exceptions as worker_error results with diagnostics; keep slot cleanup in `_execute`.
- [x] Stop admission when stop is signaled, join the scheduling loop before shutting down the pool, and allow already admitted executions to finish.
- [x] Add an event-based stop/drain regression and a mixed fast/slow exception regression.
- [x] Run `PYTHONPATH=. /usr/local/bin/python3 -m pytest tests/test_platform_run_scheduler.py tests/test_platform_run_worker.py tests/test_run_lease_heartbeat.py -q`.

The loop keeps admission separate from execution completion:

```python
while not stop.is_set():
    collect_completed()
    admit_available()
    stop.wait(interval_s)
```

## Task 4: Record current evidence

**Files:** Modify this plan, `docs/platform/development-completion.md`, `docs/testing/journeys.json` and `docs/platform/agent-fleet-self-hosted-platform.md`.

- [x] Bind the new real scheduling regressions to RUN-02, preserving existing journey selectors.
- [ ] Record fresh focused result, command, evidence location and remaining workspace-lock/remote-lifecycle work.
- [ ] Run `git diff --check` and Ruff error/import checks over touched production modules.
- [ ] Keep the overall goal active until the full development ledger is satisfied.


## Current integration evidence — 2026-11-10

The original continuous scheduling failures were reproduced before implementation (`/tmp/agent-fleet-scheduler-before.xml`,3failed/6passed). Scheduler/worker/heartbeat/legacy/conversation/journey checks subsequently produced134passed (`/tmp/agent-fleet-scheduler-final.xml`). Same-workspace capacity2 and monotonically increasing lease epochs independently failed before their repair (`/tmp/agent-fleet-workspace-concurrency-before.xml`,2failed), then the affected eight groups produced80passed (`/tmp/agent-fleet-workspace-concurrency-after.xml`). These are focused historical results within this integration; F3 changes require fresh affected tests and the final full gate remains open.
