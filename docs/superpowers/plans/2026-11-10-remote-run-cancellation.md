# Remote Run Cancellation Implementation Plan

> **For agentic workers:** Execute this slice inline with evidence-driven-bugfix and verification-before-completion. The user has authorized continued implementation; preserve all earlier functional-root-fixes work.

**Goal:** A cancellation request must reach an ordinary remote Run's receipt wait, prevent an unclaimed command from executing, and release the waiting worker without asserting that a dispatched process has stopped.

**Architecture:** Reuse the existing Run cancellation callback, RemoteToolBroker, CommandDeliveryService and SQLite command transaction. Serialize cancellation against Node claim, preserve terminal receipts, and retain unknown for dispatched execution. No new queue, command wire state, schema or runtime checkpoint.

**Tech Stack:** Python 3.10+, Flask, SQLite WAL, pytest, existing NodeToolExecutor.

## Global constraints

- Work in `codex/functional-root-fixes`; protect the primary main worktree.
- Keep `hub -> fleet_core <- tools`; no new dependency or external service calls.
- A queued command cancelled before claim uses the existing `expired` terminal state with `execution_cancelled` reason. A leased/accepted/running command becomes `unknown`; terminal receipts remain unchanged.
- General remote force-stop and releasing workers during uncancelled waits require separate lifecycle/checkpoint work and remain open.

## Task 1: Close the existing cancellation chain

**Files:**

- Create `tests/test_platform_remote_cancellation.py` for actual SQLite, HTTP cancellation, worker/scheduler and backend execution evidence.
- Modify `hub/application/run_worker_service.py` to pass its existing `should_cancel` closure to the remote broker.
- Modify `hub/application/run_lease_heartbeat.py` to expose `check()` for observing failure without renewing, and use a read-only cancellation closure in the remote broker; background renewal cadence remains unchanged.
- Modify `hub/platform/remote_tool_broker.py` to check cancellation before enqueue and pass the same callback into the receipt waiter. Callers without a callback retain the existing wait signature.
- Modify `hub/application/command_delivery_service.py` to use an atomic cancellation settlement when its existing callback returns true.
- Modify `hub/platform/runtime/native.py` to observe cancellation after a known tool result before reporting tool failure, while unknown still takes precedence.
- Modify `hub/infrastructure/command_repository.py` to expose `cancel_wait(command_id)` and reuse the existing transactional terminal fence.
- Adjust narrowly scoped old waiter fixtures and the remote fixed-test queued cancellation assertion for the new contract.
- Register the new regressions in the existing journey matrix and record results in `docs/platform/development-completion.md`.

**Interfaces:**

```python
# RemoteToolBroker constructor: add optional callback, store on self.
should_cancel = None

# Before enqueue, after validating the tool and command scope:
if self.should_cancel is not None and self.should_cancel():
    return ToolReceipt(command_id, "failed", {}, "execution_cancelled")

# Preserve custom waiters' old signature when cancellation is not configured.
wait_options = {"timeout_s": max(0.0, admitted["expires_at"] - float(self.clock()))}
if self.should_cancel is not None:
    wait_options["should_cancel"] = self.should_cancel
row = waiter.wait_for_receipt(command_id, **wait_options)

# CommandDeliveryService existing cancellation branch:
return self.repository.cancel_wait(command_id)
```

The repository fence reads the current command under `BEGIN IMMEDIATE`, returns existing terminal rows unchanged, and chooses `expired` only when the current row is still `queued`; all other nonterminal rows become `unknown`. Status/result/lease cleanup commit together. Original command identity, attempt, arguments and deadlines never change. `mark_unknown` retains its previous unconditional unknown semantics by sharing the fence with queued expiry disabled.

- [x] Add failing evidence: cancellation during a real remote worker wait must prevent a subsequent Node poll from writing a fixture file; an already dispatched command stays unknown, and another owner's Run can use the scheduler slot.
- [x] Cover cancellation before enqueue, terminal receipt precedence, callback/claim race, queued cancellation after restart, and concurrent SQLite claim/cancel.
- [x] Run the new module before implementation and save the red JUnit/log.
- [x] Implement the callback chain and the single atomic command fence; rerun the exact red module.
- [x] Run associated Run/Node/deadline/reconciliation/Workflow tests; 275 passed after the polling-write correction. Complete available local gate follows on frozen source. Preserve platform skips and original strict release rules.
- [x] Update the existing development ledger, required selectors and canonical development route. Preserve original evidence scope; no development commit, push or deployment in this slice.

## Verification commands

```bash
PYTHONPATH=. /usr/local/bin/python3 -m pytest tests/test_platform_remote_cancellation.py -q
PYTHONPATH=. /usr/local/bin/python3 -m pytest tests/test_platform_remote_delivery.py tests/test_platform_run_worker.py tests/test_platform_run_scheduler.py tests/test_platform_command_timing.py tests/test_platform_command_reconciliation.py tests/test_remote_workflow_test_execution.py tests/test_platform_node_http_e2e.py tests/test_platform_node_runtime.py tests/test_boundaries.py -q
/opt/homebrew/bin/ruff check --select E9,F63,F7,F82,F401 hub tools connectors fleet_core
git diff --check
```

The pre-fix worker test must observe the post-cancel Node side effect or fail the immediate cancellation/fencing assertions. The post-fix test must show one model request, no command replay, exact queued/dispatched state distinctions and a released worker slot. Neither result proves remote process termination.

## Evidence so far

- `/tmp/agent-fleet-remote-cancel-red-valid-20261110.xml`: 10 failed / 4 passed, including an actual post-HTTP-cancel Node file write and 3301 logical receipt sleeps for the dispatched case. The clock is injected; this is not a 330-second wall-clock measurement.
- `/tmp/agent-fleet-remote-cancel-message-red-20261110.xml`: 1 failed / 13 passed; known cancellation incorrectly displayed tool failure. Unknown keeps priority over cancellation; known results now observe cancellation before reporting tool failure.
- `/tmp/agent-fleet-remote-cancel-poll-write-red-20261110.xml`: 1 failed; direct cancellation callback reuse caused a lease write on each of 20 polls. The remote wait now checks heartbeat failure and reads cancellation without synchronous renewal.
- `/tmp/agent-fleet-remote-cancel-poll-closure-20261110.xml`: 275 passed / 47.06s across 18 associated modules. Includes the 15 new parameter cases; local/remote unchanged receipts, lost-receipt write unknown, browser submit, Node HTTP, fixed Workflow tests and shared scheduler limits remain covered.
- Remote fixed-test cancellation before Node claim now leaves the command expired; its Workflow Run and reservation conservatively remain unknown/held because that adapter still lacks a durable completed test receipt. No wider recovery assertion is added.

## Frozen full verification

Snapshot `69028deb2cf7b2908e3a4d415aba95e819dd1fa6`: **6903 passed, 5 platform skips, 204 passing subtests / 900.15s**, zero failures/errors; **43/44 journeys**, all **1984 selectors** collected, **46 complete PNGs**. All **1124 source hashes** remained identical, checkout clean. Original release verifier returned exit 1 / `journey_incomplete`; mandatory Linux cases remain unexecuted on macOS. Five external checks stay `not_run`.

Evidence: `/Users/mango/.cache/agent-fleet/verification/functional-root-remote-cancel-20261110/`. Original packagers produced 1048 backend and 67 frontend files, byte-identical to the frozen manifest. Post-run documentation recording is outside this frozen test version.
