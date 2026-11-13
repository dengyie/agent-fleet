# M3 Service Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification checkpoints.

**Goal:** Add a default-off, owner-scoped `inspect`/`restart` service action slice with explicit approval grants, immutable service-version binding, durable command delivery, node-side fixed argv validation, and no automatic Incident closure.

**Architecture:** Service definitions declare an explicit action allowlist. Hub policy creates approval grants for production mutations, consumes each grant at most once, and enqueues a signed platform command bound to service version, target alias, adapter, and argument hash. Nodes execute only adapter-owned fixed argv through an optional action executor; command receipts remain the source of execution truth while health evidence performs post-checks separately.

**Tech Stack:** Existing Python/Flask application layers, SQLite repositories, `PlatformCommand`/`CommandRepository`, static operator API, pytest. No new runtime dependency.

## Global Constraints

- `service_actions_enabled` defaults to false and exposes no new action/approval routes when disabled.
- Operator approval is a structured grant; chat text, logs, model output, and Incident state cannot authorize a production mutation.
- Every grant is owner-scoped, service-version-scoped, action-scoped, argument-hash-scoped, expiring, and single-use.
- Only fixed adapter-owned argv is allowed; no shell string, arbitrary executable, or model-provided command is accepted.
- `unknown` remains unknown; action receipt success never closes an Incident without independent health evidence.
- Credentials, raw command output, lease internals, and secret refs never appear in public responses.

## Task 1: Service action policy data

**Files:** `hub/domain/service.py`, `hub/infrastructure/service_repository.py`, `tests/test_platform_service_health.py`.

- [x] Add `allowed_actions` validation limited to `inspect` and `restart`, defaulting to an empty list.
- [x] Add/migrate the `allowed_actions` JSON column and include it in service DTOs and upsert.
- [x] Add tests proving unsupported actions are rejected, default is empty, and service version increments invalidate prior definitions.
- [x] Run `.venv/bin/python -m pytest -q tests/test_platform_service_health.py`.

## Task 2: Approval grant repository and policy service

**Files:** `hub/infrastructure/approval_repository.py`, `hub/application/service_action_service.py`, `tests/test_platform_service_actions.py`.

- [x] Create durable `platform_approval_grants` with pending/approved/rejected/consuming/consumed/expired states, bounded fields, expiry, remaining uses, service version, and args hash.
- [x] Implement owner-scoped create, decision, execution claim, and finalization with `BEGIN IMMEDIATE`; reject expired, revoked, already-consumed, version-mismatched, or scope-mismatched grants.
- [x] Implement `ServiceActionService.request` to validate fixed actions and service policy; queue `inspect` directly and return a pending grant for `restart`.
- [x] Implement `decide` to approve/reject grants and, on approval, enqueue a deterministic `PlatformCommand` with `grant_id`, `service.<action>`, fixed adapter metadata, and `manual_only` retry for restart.
- [x] Make grant consumption idempotent through deterministic `command_id`/idempotency key and bounded public DTOs.
- [x] Run the focused policy/action tests.

## Task 3: HTTP wiring and gates

**Files:** `hub/config.py`, `hub/web.py`, `hub/bootstrap.py`, `hub/http/service_action_routes.py`, `tests/test_platform_service_actions.py`, `tests/test_feature_gate_runtime.py`.

- [x] Add `service_actions_enabled` to config and environment/file gate resolution.
- [x] Initialize approval repository and action service only when platform, service monitoring, and action gates are enabled.
- [x] Add operator-only `POST /services/<service_id>/actions`, `GET /approvals/<grant_id>`, and `POST /approvals/<grant_id>/decisions`; disabled gate returns 404.
- [x] Return only action/grant/command metadata; never expose secret refs, lease fields, or raw command output.
- [x] Run focused HTTP and gate tests.

## Task 4: Node fixed action executor

**Files:** `tools/platform/services/actions.py`, `tests/test_platform_service_actions.py`.

- [x] Implement adapter/action allowlists for systemd, Supervisor, and Docker with fixed tuple argv and `shell=False`.
- [x] Validate command metadata, bounded alias text, supported action, timeout, and output size; reject arbitrary executable/argv fields.
- [x] Return bounded structured result and preserve executor exceptions as `unknown` through existing `NodeClient` journal semantics.
- [x] Run action executor tests for argv, shell rejection, unsupported adapter/action, timeout, and output bounds.

## Task 5: Documentation and full verification

**Files:** Obsidian canonical agent-fleet index, roadmap, API contract, `00.MOC/AI-DOC-ROUTER.md`, `00.MOC/项目索引.md`.

- [x] Record T3.1 implementation status, default-off gate, grant lifecycle, fixed argv boundary, and explicit non-goals (real VPS, production restart, auto-remediation).
- [x] Run `python3 .local/bin/scan-stale-docs`.
- [x] Run focused tests, full pytest, compileall, Node syntax checks, and `git diff --check`.

## T3.2 Execution Window Control Plane

**Goal:** Add a default-off, owner/run-scoped control plane for short-lived execution-window attachment and exclusive writer leases without claiming a PTY, browser, or host-process takeover.

**Architecture:** An `execution_windows` repository persists the window lifecycle and one-time attach-ticket hashes in the existing platform SQLite database. A service creates/reconnects/leases windows through owner-scoped application methods; HTTP routes expose only bounded metadata and opaque ticket/lease values. The lease protects future window input endpoints while the current slice deliberately exposes no endpoint that executes host commands or attaches to a real process.

**Global constraints:**

- `execution_windows_enabled` defaults to false and requires `platform_enabled`; disabled mode exposes no window routes and creates no window tables.
- Every window is bound to exactly one `owner_id` and `run_id`; all reads and mutations filter both owner and window identity.
- Attach tickets are short-lived, single-use, stored only as SHA-256 hashes, and cannot be used by another owner or closed/expired window.
- A window has at most one active writer lease; lease acquisition, renewal, release, and close use `BEGIN IMMEDIATE` and fail closed after expiry.
- Public DTOs never include raw ticket hashes, lease hashes, filesystem paths, commands, process identifiers, or secret values.
- `pending -> ready -> attached -> closing -> closed` is the normal lifecycle; expiry is terminal and close revokes every outstanding credential.
- This slice does not start a PTY/browser process, accepts no executable/argv/shell input, and performs no real VPS operation.

### Task 6: Durable window, ticket, and lease repository

**Files:**
- Create: `hub/domain/execution_window.py`
- Create: `hub/infrastructure/execution_window_repository.py`
- Modify: `platform_schema.py` only if a bounded token helper is required
- Test: `tests/test_platform_execution_windows.py`

**Interfaces:**
- `ExecutionWindowRepository.init()` creates additive `execution_windows`, `execution_window_tickets`, and `execution_window_leases` tables in `platform.db`.
- `create_window(owner_id, run_id, *, ttl_s, metadata=None) -> dict` returns a window in `pending` state and a one-time raw attach ticket only at creation.
- `get_window(owner_id, window_id, *, now=None) -> dict | None` returns owner-scoped public-safe state.
- `redeem_ticket(owner_id, window_id, ticket, *, now=None) -> dict` atomically consumes a valid ticket and moves `pending|ready` to `attached`.
- `acquire_writer(owner_id, window_id, holder_id, *, ttl_s, now=None) -> dict`, `renew_writer(..., lease_token)`, `release_writer(...)` implement exclusive leases.
- `close_window(owner_id, window_id, *, now=None) -> dict` transitions through `closing` to `closed` and revokes tickets/leases.

- [x] Write tests for default-off storage, owner/run isolation, ticket expiry/single-use, reconnect, exclusive lease, renewal/release, expired lease rejection, and close revocation.
- [x] Run `.venv/bin/python -m pytest -q tests/test_platform_execution_windows.py` and confirm the new tests fail before implementation.
- [x] Implement the state machine and transaction-locked repository with bounded opaque IDs and hashed credentials.
- [x] Run the focused repository tests until green (`8 passed`).

### Task 7: Application service, feature gate, and HTTP surface

**Files:**
- Create: `hub/application/execution_window_service.py`
- Create: `hub/http/execution_window_routes.py`
- Modify: `hub/config.py`, `hub/web.py`, `hub/bootstrap.py`
- Test: `tests/test_platform_execution_windows.py`, `tests/test_feature_gate_runtime.py`

**Interfaces:**
- `ExecutionWindowService.create/get/attach/acquire_writer/renew_writer/release_writer/close` translates repository errors into bounded `ApplicationError` values.
- Operator routes under `/api/platform/v1/execution-windows` return only window metadata and return raw ticket/lease tokens only in the immediate create/redeem/acquire response.
- `execution_windows_enabled` is resolved from `AGENT_FLEET_EXECUTION_WINDOWS_ENABLED`, is false unless the platform gate is active, and registers no blueprint while closed.

- [x] Add failing HTTP tests for gate closure, owner/run checks, reconnect, lease holder checks, close behavior, and response redaction.
- [x] Wire the repository/service only when both platform and execution-window gates are true; register a fail-closed 404 transport while closed.
- [x] Implement bounded request parsing for `run_id`, TTLs, holder IDs, and token fields; reject arbitrary command/process fields recursively.
- [x] Run focused HTTP/gate tests (`8 passed`; related integration regression `46 passed`).

### Task 8: Documentation and verification

**Files:**
- Modify: Obsidian canonical agent-fleet roadmap, API contract, project index, `00.MOC/AI-DOC-ROUTER.md`
- Modify: this plan file to mark T3.2 tasks complete

- [x] Record the window state machine, ticket lifecycle, lease fencing, default gate, and explicit PTY/browser/VPS non-goals in the canonical notes.
- [x] Run compileall, focused tests, full pytest, `git diff --check`, and the Obsidian stale-doc scan.

## T3.3 Window Event Stream

**Goal:** Add a default-off, owner/window-scoped durable event stream that is fenced by the active writer lease and supports bounded cursor recovery without claiming a WebSocket, PTY, browser, or host process.

**Implementation:** `execution_window_events` stores one monotonic sequence per window and a unique `client_event_id`. `append_event` uses `BEGIN IMMEDIATE`, checks attached state plus holder/token lease freshness, returns the original row for an identical retry, and returns `event_conflict` when the same id is reused with different kind/payload. `list_events` filters owner/window and supports bounded `after`/`limit` cursors.

- [x] Add event domain kind/field allowlists and bounded payload validation (size, strings, arrays, nesting, finite numbers).
- [x] Add persistent event table, per-window sequence, owner isolation, lease fencing, idempotency, and cursor listing.
- [x] Add operator-only HTTP POST/GET event routes with stable bounded errors and no lease/token/path/command leakage.
- [x] Add focused tests for gate closure, attach/lease fencing, expiry, owner isolation, sequence/cursor recovery, idempotent retries, event conflicts, payload limits and redaction (`10 passed`).
- [x] Update canonical Obsidian roadmap, contract, project index, router and this plan; stale-doc scan `RESULT=OK`.
- [x] Re-run compileall, `git diff --check`, focused tests and full pytest (`1721 passed, 2 skipped, 18 warnings, 148 subtests passed`).

**Deferred boundary:** No WebSocket, PTY/browser attach, process takeover, VPS operation, or production deployment is included. A future WebSocket change requires the existing HANDOFF auth/proxy/reconnect/origin/downgrade condition to be met and separately specified.
