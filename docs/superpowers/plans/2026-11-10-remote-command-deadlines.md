# Remote command deadlines implementation plan

> **For agentic workers:** Execute this isolated slice inline, using evidence-driven-bugfix and verification-before-completion. Preserve the existing Node durable receipt retry changes.

**Goal:** Normal remote execution must retain its queue, execution and receipt budgets; a local wait must not prematurely make the durable command unknown.

**Architecture:** An optional timing row is committed in the same transaction as the command and outbox. The existing signed `expires_at` remains the total hard deadline. Admission stops at the queue deadline; claim freezes the execution and receipt deadlines. A sequential Node claims one command at a time, for at most the caller's batch limit. Legacy commands retain their existing envelope and caller-selected lease.

**Tech stack:** Python, SQLite, pytest; no new dependencies.

## Constraints

- Work only in `codex/functional-root-fixes`; no bootstrap, composition, Run worker or deployment changes.
- Default budgets: queue 30 seconds, execution 30 seconds, receipt 300 seconds; total at most 3600 seconds.
- Receipt budget covers three sequential screenshot/result requests, each with the existing Transport nominal budget of 15 seconds × 3 attempts × 2 routes + 4 × 0.4 seconds of backoff = 91.6 seconds. Socket timeouts do not guarantee a network wall-clock ceiling; the Hub deadline still settles uncertainty.
- All timing values must be finite, positive, non-boolean numbers. Signed wire fields remain unchanged.
- Never rewrite terminal results or automatically replay dispatched work.
- Queued expiry is `expired`; leased, accepted or running expiry is `unknown`.
- Cancellation and durable model/tool checkpoints remain separate work. Waiting still occupies the current worker.

## Task 1: Reproduce the actual failures

**File:** `tests/test_platform_command_timing.py`

- [x] Add the real SQLite regression: queue at 100, claim at 102, succeed at 131; the default broker must return success.
- [x] Add two 40-second commands with a 60-second lease and `limit=2`; both must succeed, and each poll must request one command.
- [x] Add exact-boundary expiry tests for leased, accepted and running commands, plus intermediate receipt lease expiry.
- [x] Run `python3 -m pytest -q tests/test_platform_command_timing.py`; original implementation fails all seven cases.

## Task 2: Persist and consume a shared deadline contract

**Create:** `fleet_core/command_timing.py`
**Modify:** `hub/infrastructure/command_repository.py`, `hub/application/command_delivery_service.py`, `hub/platform/remote_tool_broker.py`

**Interfaces:**

```python
CommandTimingPolicy(queue_timeout_s=30, execution_timeout_s=30, receipt_timeout_s=120)
CommandTimingPolicy.start(now: float) -> CommandTiming
CommandTiming(queue_deadline: float, execution_timeout_s: float, receipt_timeout_s: float)
CommandTiming.hard_deadline: float
CommandRepository.enqueue(command, *, timing: CommandTiming | None = None, ...)
CommandRepository.get_timing(command_id: str) -> dict | None
CommandRepository.expire_command(command_id: str) -> dict
CommandDeliveryService.enqueue(command, *, timing=None, ...)
CommandDeliveryService.enqueue_submit(command, *, timing=None, ...)
RemoteToolBroker(..., timing_policy: CommandTimingPolicy | None = None)
```

- [x] Create the bounded neutral timing values and additive `platform_command_timing` table with command foreign key and cascading retention cleanup.
- [x] Commit timing and original command atomically; idempotency returns the original timing and deadline.
- [x] In the claim transaction expire overdue queued work, settle every expired active lease as unknown, and freeze timed execution/receipt deadlines.
- [x] A timed claim's lease covers the original hard deadline and remains bounded by the 3600-second contract.
- [x] Waiters settle authoritative expiry; a timed local wait timeout returns the pending row without mutating its outcome. Legacy wait timeout semantics remain compatible.
- [x] Broker constructs timing and uses the returned original command deadline when calculating its wait.
- [x] Extract the existing pure `remote_retry_class` helper for Workflow capability dispatch.

## Task 3: Execute only immediately claimed commands

**Modify:** `tools/platform/node_client.py`, `tools/platform/node_runtime.py`

- [x] Retry durable receipts first, retaining the original worker identity.
- [x] Poll at most `limit` times, requesting `limit=1` each time; stop when empty or on transport failure, and preserve bounded result counts.
- [x] Preserve legacy direct `handle()` behavior and the wire envelope. Reject malformed responses containing more than the single claimed capacity.

## Task 4: Verify persisted boundaries and integrations

**Tests:** `tests/test_platform_command_timing.py` and existing remote delivery, node HTTP, journal admission, node runtime, browser and Muse integration suites.

- [x] Validate queue deadline equality, restart persistence, bounded leases, receipt grace beyond 60 seconds, idempotency without deadline extension, local wait without durable mutation, legacy command compatibility, immutable terminal results, signature compatibility and invalid/non-finite timing values.
- [x] Update explicit delivery stubs to accept and forward `timing`; no `hasattr` or silent fallback.
- [x] Run the seven original regressions, then the related suites fresh and inspect output.

## Verification evidence

- Initial original-code regression run: 7 failed.
- Corrected owner-scoped queue/execution regression rechecked against the original HEAD broker in memory: failed with `unknown` versus expected `succeeded`; no shared source files were reverted.
- Fresh related verification: 243 passed in 11.50 seconds. Command:

```sh
python3 -m pytest -q tests/test_platform_command_timing.py tests/test_platform_delivery.py tests/test_platform_remote_delivery.py tests/test_platform_node_http_e2e.py tests/test_platform_node_runtime.py tests/test_platform_journal_admission.py tests/test_platform_run_worker.py tests/test_muse_fleet_integration.py tests/test_plugin_node.py tests/test_platform_browser_policy_backend.py tests/test_platform_submit_approvals.py tests/test_platform_command_reconciliation.py tests/test_boundaries.py
```

- No deployment or real remote-node process cancellation exercised. Execution budget is persisted and composes with the existing Sandbox 30-second ceiling; it is not a generic process cancellation facility. Timed synchronous waits still occupy the worker until a receipt/deadline; releasing capacity requires durable runtime checkpoints.
