# Agent Fleet production review and hardening

## Objective

Review the current Hub, control-plane, runner, Probe discovery, session/transcript, platform execution, Windows sandbox, and browser-facing contracts against their real call chains and tests. Fix confirmed production defects at their root and leave a fresh, reproducible verification record.

## Review order

1. Establish the current branch, baseline diff, runtime dependencies, startup/configuration path, and existing CI gates.
2. Trace request authentication and authorization from routes through application services and repositories; check failure propagation, idempotency, and tenant/workspace scoping.
3. Trace task/run scheduling, lease heartbeat, retries, reconciliation, and result persistence; check duplicate execution, stale ownership, and cancellation/resource cleanup.
4. Trace Probe discovery and transcript/session identity from filesystem input through ingestion and API output; check fail-closed behavior and collision handling.
5. Trace local/remote process launch and Windows workspace sandbox boundaries; check path, environment, handle, descendant, and cleanup invariants.
6. Review frontend/API contracts and browser journeys only where they exercise the above server invariants.
7. For every confirmed issue, add a focused regression test that fails before the fix, implement the smallest root-cause correction, and rerun the original failure plus affected suites.
8. Run compile, syntax, diff, focused tests, full tests, and platform-specific gates that are available; record external/platform limitations explicitly.

## Evidence and acceptance

- No production claim without fresh command output.
- No fix without a reproducible failing test or equivalent deterministic failure evidence.
- Do not change the protected primary worktree or its unrelated dirty files.
- Do not add compatibility wrappers or speculative abstractions; remove unreachable duplicate code only when its lack of callers is proven.
- Final report must distinguish confirmed defects, fixed defects, external validation still required, and test skips that are platform limitations rather than passes.

## Review evidence and fixes

### Confirmed and fixed

1. **Native transcript identity confusion (P1)**
   `tools/probe/discovery.py` selected the newest JSONL below the Codex, Pi,
   or ZCode session root when the process command line carried no stable
   session identity. Concurrent sessions could therefore be adopted with a
   different transcript and later resumed as the wrong session. Discovery now
   fails closed without an explicit selector. Codex accepts only an explicit
   resume id, Claude keeps exact session-id matching, and Pi accepts an
   explicit session path/id constrained to its configured root. The unused
   mtime-based scanner was removed.

2. **Scheduler cleanup masked worker outcomes (P1)**
   `RunSchedulerService._execute` allowed `release_worker_slot()` failures in
   `finally` to replace a successful worker result with `worker_error`. Cleanup
   now emits a bounded diagnostic and preserves the worker result or original
   exception. The slot remains reclaimable by normal expiry/reconciliation.

3. **Reconciliation exceptions were silent (P2)**
   The generic background callback loop and requeue event fan-out discarded
   exceptions. Both paths now use `log_failure` with fixed event codes and
   bounded exception-chain/location metadata.

4. **Task-store initialization degraded without evidence (P2)**
   `create_app` disabled the task store on initialization failure without a
   diagnostic. It now records `task_store_init_failed` through the same bounded
   logging boundary; the observation API remains available as designed.

### Verification

- Focused review slice: `147 passed`.
- Full gate: `3499 passed, 24 skipped, 165 subtests passed`.
- `python3 -m compileall -q .`: passed.
- `git diff --check`: passed.
- The 24 skips are existing platform/browser constraints; no new skip was
  introduced by this review.

### Remaining review conclusions

- Task records are intentionally global operator resources in the legacy task
  store; the table has `requested_by` audit attribution but no owner scope.
  No cross-tenant defect was changed without a documented owner-scoping
  contract.
- Cleanup-only `pass` blocks in repository rollback and process teardown were
  inspected and left unchanged where they are bounded best-effort cleanup;
  business failures are propagated or recorded at their owning boundary.
