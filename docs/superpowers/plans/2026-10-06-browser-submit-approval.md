# Browser Submit Approval Implementation Plan

> **For agentic workers:** Execute this plan task-by-task in the current isolated worktree with evidence-driven regression tests. The optional subagent-driven-development skill is not installed; inline execution is selected by the user's request to complete development.

**Goal:** Implement every offline acceptance case of the browser submit approval specification, including owner admission, atomic durable dispatch, signed Node binding, and bounded form POST.

**Architecture:** Keep the existing synchronous Flask/SQLite/Node transport architecture. The Hub owns approval state; SQLite holds approval consumption, command insertion and outbox insertion in one transaction. The Node validates signed approval metadata and reuses the injected browser driver. No real-browser adapter is introduced.

**Tech Stack:** Python 3.10+, Flask 3+, stdlib sqlite3/http.client, existing cryptography signing, pytest. No new dependencies.

## Global constraints

- Source: `docs/superpowers/specs/2026-10-05-browser-submit-approval-contract.md`, T3.6 plan and T3.7 network spec.
- User authorization on 2026-10-06 approves implementation of the draft's fixed acceptance values, not gate activation or deployment.
- All gates default off. Submit requires the browser and network parent gates and an available submit-capable driver.
- TTL 300 s (expiry inclusive), active session/run quotas 4/16, workspace rolling-hour grants 64.
- Selector <=512 UTF-8 bytes; exact matching, no semantic rewriting.
- POST <=64 fields, names <=128 B, values <=4096 B, encoded body <=16 KiB. GET/HEAD general request only; no POST redirect.
- Preserve 20-second absolute deadline, existing pinned transport/TLS/result/artifact budgets and manual-only retry.
- SQLite point lookups use indexed keys (O(log n)); quota lookups stop at 4/16/64 rows. Form validation is O(field bytes), bounded memory.
- Preserve causes internally; wire errors contain stable codes only. No new logging of input/driver data.
- Existing sync persistence is used deliberately: converting the entire Flask/Node architecture to async is outside this spec and would require unrelated dependencies and lifecycle changes.

## Task 1: Durable approval invariants

**Files:** `hub/domain/browser_submit.py`, `hub/infrastructure/browser_repository.py`, `tests/test_platform_submit_approvals.py`.
**Interfaces:** immutable `SubmitApprovalRequest.parse(body)` and public approval DTO; `grant_submit_approval`, `revoke_submit_approval`, `get_submit_approval`, `expire_submit_approvals`; transaction-only `consume_submit_approval(connection, ...)`.

- [x] Turn review reproductions into pytest regressions using a real temporary SQLite platform database and injectable time.
- [x] Prove inclusive TTL, stale quota, scope mismatch, revoked/consumed error cases fail on the imported snapshot.
- [x] Add bounded indexed matching and quota queries; grants verify the persistent run/session tuple inside the write transaction.
- [x] Add scoped lifecycle triggers for closed sessions and terminating runs, covering all existing Run write paths.
- [x] Validate with `pytest tests/test_platform_submit_approvals.py -q`.

Acceptance assertions include:
```python
with pytest.raises(BrowserRepositoryError, match='approval_expired'):
    consume(now=approval['expires_at'])
assert repo.get_submit_approval(owner, approval_id)['state'] == 'expired'
assert repo.get_submit_approval(owner, unrelated_id)['state'] == 'active'
```

## Task 2: Atomic command admission

**Files:** `hub/infrastructure/command_repository.py`, `hub/application/command_delivery_service.py`, `tools/platform/remote_tool_broker.py`, `tests/test_platform_submit_approvals.py`, `tests/test_platform_remote_delivery.py`.
**Interfaces:** `CommandRepository.enqueue(..., prepare=None)` transaction preparation hook; `CommandDeliveryService.enqueue_submit(command, *, approvals, idempotency_key)` returns the final signed persisted command. The callback consumes exactly one approval and signs after adding its ID.

- [x] Add rollback tests injecting command/outbox insertion failure and concurrent consumers.
- [x] Prepare submit command inside the existing command transaction; verify replay identity before consuming another approval.
- [x] Broker uses the atomic delivery operation and includes the opaque approval ID in receipt/events, never selector or form fields.
- [x] Verify rollback, restart persistence, duplicate command idempotency, approval single-use and signing failures.

```python
assert command_repo.get(command_id) is None
assert approvals.get_submit_approval(owner, approval_id)['state'] == 'active'
assert driver.submits == []
```

## Task 3: Node and local execution boundary

**Files:** `tools/platform/browser_policy.py`, `tools/platform/browser_backend.py`, `tools/platform/node_executor.py`, `tools/platform/node_client.py`, `tools/platform/node_runtime.py`, `tools/platform/tool_broker.py`; corresponding platform tests.

- [x] Reproduce signed approval_id rejection and local approval bypass as failing tests.
- [x] Separate model arguments from signed approval metadata at Node dispatch; validate opaque ID and propagate it through success/failure/unknown receipts and journal.
- [x] Keep direct local ToolBroker submit disabled because it has no Hub approval authority.
- [x] Gate submit on browser/network config and driver availability; driver.submit remains the sole driver seam.
- [x] Check pre-dispatch bounded failure, after-dispatch unknown, run binding, replay and result overflow.

```python
assert receipt['result']['approval_id'] == approval_id
assert journal.get(command_id)['state'] == 'unknown'
assert driver.submits == ['#go']
```

## Task 4: Owner HTTP and runtime wiring

**Files:** `hub/application/conversation_service.py`, `hub/http/conversation_routes.py`, `hub/config.py`, `hub/web.py`, `hub/bootstrap.py`, `hub/application/run_worker_service.py`, `tests/test_platform_submit_http.py`.

- [x] POST accepts only session_id and selector; Idempotency-Key header supplies optional grant identity.
- [x] Derive node/workspace from the durable session; authenticate owner and validate run before grant, revoke or get.
- [x] Public DTO exposes approval metadata without selector or internal owner identifiers; wrong run never mutates a row.
- [x] Wire default-off submit gate and approval repository through Hub configuration and remote worker.
- [x] Verify anonymous/wrong-owner/wrong-run/gate-off behavior and model visibility using the in-process Flask client.

```python
assert response.status_code == 404
assert repo.get_submit_approval(owner, approval_id)['state'] == 'active'
```

## Task 5: Form transport and end-to-end acceptance

**Files:** `tools/platform/browser_transport.py`, `tests/test_platform_browser_transport.py`, `tests/test_platform_submit_http.py`, `tests/test_platform_node_http_e2e.py`.

- [x] Reuse pinned exchange; validate UTF-8 and all exact field/body boundaries before I/O.
- [x] Inject a fixture driver resolving the current enclosing form, rejecting password/file controls and invalid selectors before POST.
- [x] Prove valid owner grant -> worker -> signed Node -> driver -> fake socket -> durable receipt, followed by rejected reuse.
- [x] Prove destination rejection, all redirect statuses, timeout/TLS errors, password-type screening, and no metadata leakage with synthetic markers.

```python
assert len(transport.test_requests) == 1
assert transport.test_requests[0][0] == 'POST'
assert marker not in json.dumps(metadata_planes)
```

## Task 6: Verification and documentation

**Files:** submit specification, `docs/platform/agent-fleet-self-hosted-platform.md`, `docs/testing/README.md`, and `docs/testing/journeys.json`.

- [x] Run focused submit tests and the entire repository suite (including all platform tests) with available browser-test prerequisites.
- [x] Run Python 3.10 AST checks, compileall with cache outside the source checkout, and `git diff --check`.
- [x] Review the final diff against every numbered acceptance case; record exact test evidence and remaining real-browser non-goals.
- [x] Rebase onto latest main and verify the integrated changes before committing and the requested local main merge; preserve the source dirty worktree.

### Verification evidence

- Rebased consolidated development onto `6e547a2`; retained main's account, provider request-observer, worker lease and acceptance-policy changes. Pre-integration snapshot remains on `codex/browser-submit-pre-integration`.
- Updated provider fixtures to the current keyword-only `request_observer` interface. Node HTTP and submit E2E regression: **26 passed**.
- Final review reproduced two command-idempotency failures (`1` versus `true` / `1.0`); restored canonical argument-hash comparison. Expanded signed cross-Run and file-control tests. Related regression: **51 passed**.
- Latest approval/HTTP/E2E/transport regression after flattening quota/scope validation: **189 passed**.
- Follow-up coverage added for the revocation race: the `revoked_in_flight` E2E case waits until the single POST has completed, revokes the already-consumed approval before the driver returns, and verifies the original command result is preserved; the pre-dispatch `revoked` case still proves no driver dispatch. Focused approval/HTTP/E2E/transport regression: **202 passed**.
- Full local journey gate with installed Playwright and Chrome: **2632 passed, 2 skipped, 154 subtests passed; 29/29 journeys passed**, in 330.35 seconds. The two skips are Linux `/proc` checks in `tests/test_supervisor_process.py`; Linux CI must supply those results. The existing `tools/probe/discovery.py` docstring escape emits one SyntaxWarning.
- Python 3.10 syntax accepted all **26 changed Python files**; compileall succeeded for `hub`, `tools`, and `tests`; whitespace check passed. No new dependency or gate activation.
- Machine-readable local evidence: `/tmp/agent-fleet-submit-evidence/journeys.json` and `/tmp/agent-fleet-submit-evidence/results.xml`. This was a local working-tree verification, not CI/release or external-service acceptance; the report records the pre-commit revision and dirty state truthfully.

### Post-integration wiring repair (2026-10-07)

The integrated production assembly exposed three submit-path mismatches that
the earlier fixture-only evidence did not cover: `SubmitApprovalService` was
constructed without its Run lookup, the worker passed duplicate browser-submit
arguments, and `RemoteToolBroker` consumed an approval before calling the
transactional `enqueue_submit` path. The latter could burn an approval and then
fail command admission, violating the contract's single transaction boundary.

The repair wires the service through the existing Hub assembly, adds its
transaction-aware consume and terminal-expiry ports, and leaves approval
consumption to `CommandDeliveryService.enqueue_submit`, where the approval ID,
final signature and durable command are committed together. The in-process Node
HTTP fixture now enables the browser/network/submit parent gates, declares the
submit capability, verifies the Hub signature, and grants through the public
owner service before dispatch.

Fresh focused verification: **139 passed** across submit HTTP, approval store,
remote delivery, Node HTTP E2E, Run worker and conversation regressions. The check used the
working tree and did not establish a clean committed full-suite/package gate;
real-browser adapter, process-level egress and external acceptance remain
outside this offline contract.

The clean committed revision `3d4502222555704e933c12a311ea71e4ff9063dc` then
passed the browser-enabled required gate: **3027 passed, 2 Darwin `/proc`
skips, 156 subtests and 29/29 journeys** in 328.54 seconds. The release,
layout and auto-deploy regression slice passed **88 tests and 2 subtests**.
Evidence: `/tmp/agent-fleet-submit-wiring-final-20261007/results.xml` and
`journeys.json`; the report records `working_tree_dirty=false`,
`ci_status=passed`, and matrix SHA-256
`d45464805d50dffea1ec39fc5faeaef07b2c7708a7f90e4457b841eb00698795`.
MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE remain `not_run`.
