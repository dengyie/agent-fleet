# Concurrent Approval Idempotency Hardening Plan

> **For agentic workers:** Execute this plan inline in the current isolated worktree. Session policy prohibits subagent delegation. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure concurrent identical service-approval requests recover one durable grant and that an interrupted approved-grant consume resumes the same deterministic command.

**Architecture:** Keep the behavior in `ApprovalRepository`, where the unique index and SQLite transaction define durable identity. Serialize the existing lookup-and-create sequence with `BEGIN IMMEDIATE`, returning the already persisted grant when the request binding matches and preserving the current bounded conflict behavior otherwise.

**Tech Stack:** Python, SQLite WAL, pytest, thread synchronization for deterministic HTTP-independent repository concurrency coverage.

## Global Constraints

- Do not modify `frontend/` product code.
- Preserve owner scoping, request binding (`service_id`, action, argument digest), approval state, and API error contracts.
- Never weaken the unique `(owner_id, idempotency_key)` invariant.
- Keep all files UTF-8 without BOM and keep storage failures bounded as `approval_store`.

---

### Task 1: Reproduce concurrent grant creation

**Files:**
- Modify: `tests/test_approval_repository.py`

**Interfaces:**
- Use the existing `ApprovalRepository.create()` and `_create()` fixture helper.
- Use two worker threads sharing the same repository and identical `idempotency_key`.
- Force both unprotected lookups to complete before either insert; assert both callers receive the same grant id and only one row exists.
- Create a durable `consuming` row without its command and assert a fresh Hub process can recover the command using the same ID.

- [x] Add a connection wrapper in the test that waits at the owner/idempotency lookup using a two-party barrier. The wrapper delegates normal SQLite operations and ignores a timed-out/broken barrier so it also works after the fix serializes writers before the lookup.
- [x] Run `PYTHONPATH=. python3 -m pytest tests/test_approval_repository.py::test_concurrent_idempotent_grant_creation_recovers_existing -q`; the baseline failed with one `approval_store` error and one persisted grant.

### Task 2: Serialize lookup and insert atomically

**Files:**
- Modify: `hub/infrastructure/approval_repository.py`
- Test: `tests/test_approval_repository.py`

**Interfaces:**
- Keep `ApprovalRepository.create(owner_id, ..., idempotency_key=...)` as the sole write entry point.
- Keep matching retries returning `_row(prior)` and mismatched request bindings returning `ApprovalRepositoryError("idempotency_conflict")`.

- [x] Start `BEGIN IMMEDIATE` before checking the idempotency key. Explicitly commit both the replay-read path and the new insert path; roll back on bounded repository errors and SQLite failures.
- [x] Focused repository regression passed; all 23 approval repository tests passed. A real HTTP race regression passed; the six approval/service-action/delivery suites reported 129 passed.

### Task 3: Record and verify the review result

**Files:**
- Modify: `docs/reviews/2026-11-24-backend-deep-review.md`
- Modify: `docs/testing/README.md`
- Update the canonical Obsidian `agent-fleet` deployment/review note in its `master` worktree with the corrected prior test count and this new result.

- [x] Record the exact reproduction, transaction boundary fix, test command/results, and unchanged approval API/schema in the backend review.
- [x] Final HTTP/service-action suites reported 135 passed; session/Supervisor suites reported 285 passed, 7 subtests; boundary separation reported 21 passed. Ruff, compileall, diff check, and UTF-8/no-BOM checks passed.

## Acceptance

- Two concurrent matching requests with the same owner and key return the same durable grant id.
- Exactly one grant row exists and it remains pending.
- Concurrent approve decisions produce one deterministic command and one outbox row.
- Restart recovery completes the same command when interruption occurs before or after command/outbox persistence.
- A failed concurrent enqueue cannot revert another consumer’s command fence.
- Expired consuming grants cannot enqueue and become durably expired.
- Sequential retries and mismatched action/digest conflicts retain their existing behavior.
- SQLite errors do not expose database paths or exception text.
