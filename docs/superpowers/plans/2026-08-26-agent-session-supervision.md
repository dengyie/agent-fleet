# Agent-Fleet 受管会话监督 Implementation Plan

> **Status (2026-09-08): historical / implemented.** Open checkboxes below are the original plan and were not kept in sync after merge. Do not re-implement from unchecked boxes. Remaining work is recorded in `docs/HANDOFF.md` §六 as `done` or `deferred-with-condition`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保持现有 observation、Runner task control 和前后端独立发布契约不变的前提下，为由 Agent Supervisor 启动的 Hermes/Codex/Claude 会话增加可证明的内容采集、bounded transcript persistence、策略审计和 pull-mediated process control。

**Architecture:** 新增一个与 metadata observation、Runner result 分离的 session data plane。Agent-side Session Bridge 通过经过 capability probe 的 native/structured/PTY source 产生统一事件，写入加密且有界的本地 spool 后主动上传 Hub；Hub 将事件写入独立 transcript repository。Agent-side Supervisor 是受管 CLI 的本地进程权威，Hub 只通过 Agent 主动 polling 返回签名、限时、scope 明确的固定控制命令。

**Tech Stack:** Python 3.12+、现有 Flask/SQLite/WAL/JSONL 组件、原生 `urllib`/`subprocess`/`os`/`signal`、native HTML/CSS/JS frontend、`cryptography>=42`（Ed25519 command signatures 和 AEAD transcript-at-rest encryption）。不引入 Node/npm/webpack/Vite/Electron/Tauri 作为本功能后端依赖，不改变既有前端 release 机制。

**Spec:** `docs/superpowers/specs/2026-08-26-agent-session-supervision-design.md`

## Global Constraints

- Observation remains metadata-only; complete session events use a separate session data plane and must not enter observation snapshots or ordinary `events.jsonl` payloads.
- Existing `queued -> leased -> running -> succeeded|failed|cancelled|expired` task state machine, lease TTL, heartbeat, attempt fencing, retry, and idempotent result contracts remain authoritative.
- Existing Observe, Operator, and Runner authentication domains remain mutually exclusive; Supervisor gets a fourth capability-scoped credential and never inherits Runner control authority.
- Hub never SSHes, opens a reverse connection, executes arbitrary remote shell, writes arbitrary remote files, injects arbitrary stdin, or issues arbitrary tmux commands.
- Supervisor commands are limited to `pause_session`, `resume_session`, `terminate_session`, `quarantine_session`, and `cancel_attempt`.
- Every command has canonical signature, nonce, issued/expiry timestamps, machine/session/attempt scope, idempotency, and an auditable receipt.
- Managed sessions require Supervisor ownership, session/attempt/process-group binding, startup/exit records, capability manifest, bounded spool, command receipt, and visible sequence/cursor/gap state.
- Unmanaged sessions are `managed: false`, `capture_quality: best_effort`, and `control_capability: unavailable`; they are never auto-adopted.
- Hermes has no verified structured spawn/transcript contract in this baseline and is observation-only/best-effort until a runtime capability probe proves otherwise.
- Claude and Codex flags, transcript formats, hooks, and resume behavior are capability-probed at runtime; unverified CLI behavior cannot be implemented as an exact guarantee.
- Redaction runs at Agent capture, before Hub durable ingest, and before Operator response; secret/path/credential values never enter logs, SSE, frontend HTML, metrics labels, or bounded errors.
- Raw transcript is encrypted at rest, access-controlled separately from redacted transcript, bounded by byte/event limits, rotated, retained for 14 days, and never dumped through an API.
- Initial hard limits are 64 KiB per redacted event, 100 events or 256 KiB per upload batch, 32 MiB per-session spool, 128 MiB per-machine spool, and 256 MiB per Hub session raw store.
- `/api/*` remains authoritative, `/api/v1/*` is adapter-only, and there is no `/api/v1/commands/*` Runner surface unless a later approved spec explicitly adds it.
- Existing `/api/stream` SSE MIME, no-cache, buffering, keepalive, replay, and proxy timeout behavior remains unchanged; new SSE payloads are summaries/cursors, not raw transcript.
- Backend release is deployed before a frontend release that consumes new fields; rollback preserves old JSONL, SQLite schema, API paths, and in-flight leases.
- Each task is implemented and committed by a fresh subagent, then reviewed for spec compliance and code quality before the next dependent task starts.

## Worktree and Task Order

All implementation agents work in isolated worktrees. The controller integrates one reviewed task at a time in dependency order. No two implementation agents modify the shared integration branch concurrently.

Dependency order:

```text
1 schema -> 2 probe -> 3 redaction -> 4 spool
1 -> 5 transcript repository -> 6 ingest/query API
2 + 3 + 4 + 6 -> 7 Session Bridge
1 + 2 + 3 -> 8 Agent Supervisor
6 + 8 -> 9 Supervisor command queue and receipts
8 + 9 -> 10 task cancel integration
6 + 7 + 9 + 10 -> 11 SSE/frontend
11 + all -> 12 migration, rollback, chaos and production gates
```

## File Map

### Shared contracts and Agent-side files

- `session_schema.py`: shared, dependency-light event envelope, allowlists, quality values, event/batch bounds.
- `tools/session/probe.py`: bounded CLI capability probe and manifest serialization.
- `tools/session/probes/claude.py`, `codex.py`, `hermes.py`: per-agent probe adapters; no raw command output leaves the probe.
- `tools/session/redact.py`: deterministic redaction and uncertainty reporting.
- `tools/session/crypto.py`: local AEAD wrapper and key loading; never logs key material.
- `tools/session/spool.py`: encrypted append-only segments, checkpoints, quota, rotation and replay.
- `tools/session/uploader.py`: bounded HTTPS upload and ack/cursor handling.
- `tools/session/bridge.py`: source adapter orchestration and event normalization.
- `tools/supervisor/model.py`: local managed-session manifest and capability model.
- `tools/supervisor/supervisor.py`: process group lifecycle, local policy and fixed actions.
- `tools/supervisor/control_client.py`: Supervisor poll, signature verification and receipt upload.

### Hub-side files

- `hub/domain/session.py`: session lifecycle, public DTO and managed/unmanaged state transitions.
- `hub/domain/control.py`: fixed action/state enums, canonical command payload and conflict precedence.
- `hub/domain/supervisor.py`: Supervisor identity, capability and receipt DTOs.
- `hub/infrastructure/session_repository.py`: durable session metadata, cursor and dedupe index, separate from task DB.
- `hub/infrastructure/transcript_repository.py`: bounded redacted stream and encrypted restricted raw store.
- `hub/application/session_service.py`: ingest, dedupe, ack, query and repository failure isolation.
- `hub/application/supervisor_service.py`: command queue, signatures, expiry, receipt idempotency and audit.
- `hub/application/control_router.py`: task cancel to `cancel_attempt` bridge.
- `hub/http/session_routes.py`: Observe ingest and Operator query adapters.
- `hub/http/supervisor_routes.py`: Supervisor poll/receipt adapters.
- `hub/auth.py`, `hub/config.py`, `hub/bootstrap.py`, `hub/web.py`: additive Supervisor auth/config/wiring.

### Tests and deployment

- `tests/test_session_schema.py`
- `tests/test_session_probe.py`
- `tests/test_session_redaction.py`
- `tests/test_session_spool.py`
- `tests/test_transcript_repository.py`
- `tests/test_session_ingest_api.py`
- `tests/test_session_bridge.py`
- `tests/test_supervisor_process.py`
- `tests/test_supervisor_control.py`
- `tests/test_task_cancel_supervisor.py`
- `tests/test_session_frontend_contracts.py`
- `tests/test_session_chaos.py`
- `deploy/e2e-smoke.sh`
- `deploy/test-release-routing.sh`
- `docs/HANDOFF.md`

---

### Task 1: Freeze Session Event Contract

**Files:**
- Create: `session_schema.py`
- Create: `hub/domain/session.py`
- Create: `tests/test_session_schema.py`
- Modify: `deploy/package-release.sh` only if the shared module is not already included by the existing root-file list

**Consumes:** Existing `report_schema.py` allowlist conventions and the spec sections on event envelope, quality, bounds, and public DTOs.

**Produces:**
- `EVENT_KINDS` containing exactly the fifteen event kinds in the spec;
- `CAPTURE_QUALITIES = ("exact", "structured", "best_effort")`;
- `MAX_EVENT_BYTES = 65536`, `MAX_BATCH_EVENTS = 100`, `MAX_BATCH_BYTES = 262144`;
- `validate_event(event: Mapping[str, Any]) -> dict`;
- `validate_batch(events: Sequence[Mapping[str, Any]]) -> list[dict]`;
- `public_session_dto(session: Mapping[str, Any]) -> dict`;
- `managed/unmanaged` and quality transition validation.

- [ ] **Step 1: Write failing tests.** Cover unknown event kind, unknown top-level field removal, invalid quality, missing identity fields, oversized payload, batch count/byte limits, and DTO omission of path/PID/credential/raw collector fields.
- [ ] **Step 2: Run `PYTHONPATH=. .venv/bin/python -m unittest tests.test_session_schema -v`.** Confirm failure because the shared contract is absent.
- [ ] **Step 3: Implement pure validation.** Keep it independent of Flask, SQLite, CLI packages, filesystem paths, and network clients so both Hub and Agent tools can import it.
- [ ] **Step 4: Add golden JSON fixtures.** Fixtures must contain user/assistant/tool/capture-gap events and redaction metadata without real credentials or production paths.
- [ ] **Step 5: Run the focused test and the existing schema/regression tests.**
- [ ] **Step 6: Commit `feat(session): freeze session event contract`.**

**Rollback:** Delete only the new shared module and tests; existing observation schema and APIs remain untouched.

---

### Task 2: Implement Runtime Capability Probes

**Files:**
- Create: `tools/session/probe.py`
- Create: `tools/session/probes/claude.py`
- Create: `tools/session/probes/codex.py`
- Create: `tools/session/probes/hermes.py`
- Create: `tests/test_session_probe.py`

**Consumes:** Task 1 event/quality constants. Existing connector naming is reference-only; probe code must not call Hub or import Flask.

**Produces:**
- `CapabilityManifest` dataclass with `agent`, `version`, `installed`, `spawn`, `resume`, `native_transcript`, `structured_stream`, `hooks`, `pty`, `supported_event_kinds`, `quality_by_kind`, and bounded `diagnostics`;
- `probe_agent(agent: str, command: Sequence[str], timeout_s: float = 8.0) -> CapabilityManifest`;
- `probe_all(config) -> dict[str, CapabilityManifest]`;
- deterministic JSON serialization with no raw stdout/stderr.

- [ ] **Step 1: Write fixture-driven failing tests.** Feed fake `--version`/`--help` outputs for Claude, Codex and Hermes; assert absent flags become false and diagnostics are bounded summaries.
- [ ] **Step 2: Run `PYTHONPATH=. .venv/bin/python -m unittest tests.test_session_probe -v`.** Confirm missing probe module failure.
- [ ] **Step 3: Implement bounded subprocess probing.** Catch missing executable, timeout and non-zero exit. Never construct an invocation from Hub data. Only allow configured executable families.
- [ ] **Step 4: Encode honest capabilities.** Claude/Codex may report structured/native capabilities only when observed. Hermes defaults to observation-only best-effort and never reports structured spawn/resume from metadata alone.
- [ ] **Step 5: Add capability downgrade tests.** A failed resume probe must make `resume=False`; no code path may append an unverified resume flag.
- [ ] **Step 6: Commit `feat(session): add CLI capability probes`.**

**Rollback:** Disable probe invocation; existing adapters continue unchanged.

---

### Task 3: Add Redaction and Session Crypto Primitives

**Files:**
- Create: `tools/session/redact.py`
- Create: `tools/session/crypto.py`
- Create: `hub/domain/redaction.py`
- Modify: `requirements.txt` to add `cryptography>=42`
- Create: `tests/test_session_redaction.py`
- Create: `tests/test_session_crypto.py`

**Consumes:** Task 1 payload allowlists. No external provider token or raw key may be added to the repository.

**Produces:**
- `Redactor.redact_event(event: Mapping[str, Any]) -> tuple[dict, RedactionReport]`;
- deterministic rules for credential/token/password/private-key/cookie/db-URL/env/path/internal-address patterns;
- `uncertain=True` when context cannot safely classify a value;
- `AeadBox.encrypt(plaintext, associated_data) -> bytes` and `AeadBox.decrypt(ciphertext, associated_data) -> bytes`;
- key loader that reads only an explicitly configured local credential source and fails closed if encryption is requested without a key.

- [ ] **Step 1: Write failing tests.** Cover high-confidence token replacement, private-key replacement, path replacement, nested tool arguments/results, uncertainty marking, unchanged non-sensitive text, AEAD round trip, wrong associated data rejection, and missing-key fail-closed behavior.
- [ ] **Step 2: Run focused tests and confirm failure.**
- [ ] **Step 3: Implement redaction as pure deterministic code.** Redaction output must contain placeholders such as `[REDACTED:credential]`; original text must never be included in `RedactionReport`.
- [ ] **Step 4: Implement AEAD wrapper.** Use `cryptography` primitives; do not implement cryptography manually. Key values never enter repr, exception text, metrics or logs.
- [ ] **Step 5: Run focused tests plus `tests/test_http_contracts.py` secret/path error assertions.**
- [ ] **Step 6: Commit `feat(session): add redaction and encrypted storage primitives`.**

**Rollback:** Remove the optional dependency and keep session capture disabled; no existing authentication path depends on it.

---

### Task 4: Build the Bounded Local Spool and Uploader

**Files:**
- Create: `tools/session/spool.py`
- Create: `tools/session/uploader.py`
- Create: `tests/test_session_spool.py`
- Create: `tests/test_session_uploader.py`

**Consumes:** Tasks 1 and 3. The uploader must not reuse Runner result files or heartbeat log buffers.

**Produces:**
- `LocalSpool(root, machine_id, session_id, max_session_bytes=33554432, max_machine_bytes=134217728)`;
- `append(event) -> int`, `read_after(sequence, limit, max_bytes)`, `ack(sequence)`, `checkpoint()`, `rotate()`, `status()`;
- encrypted bounded segments with checksum/header and atomic segment completion;
- `SessionUploader(post_json, batch_events=100, batch_bytes=262144)` with `flush_once()` and `replay_from_ack()`;
- explicit `capture_blocked` result for exact-event quota exhaustion.

- [ ] **Step 1: Write failing tests.** Cover append/read, monotonic sequence, restart checkpoint recovery, checksum failure isolation, segment rotation, session quota, machine quota, ack deletion, duplicate replay, exact overflow pause signal, and best-effort gap event.
- [ ] **Step 2: Run `PYTHONPATH=. .venv/bin/python -m unittest tests.test_session_spool tests.test_session_uploader -v`.**
- [ ] **Step 3: Implement spool writes.** Assign sequence before persistence; fsync checkpoint before reporting an event durable; never silently discard exact events.
- [ ] **Step 4: Implement uploader.** POST only bounded batches; honor `accepted_through`/`next_cursor`; retain unacknowledged segments; treat transport failures as retryable and API rejects as per-event results.
- [ ] **Step 5: Run focused tests and crash-injection tests.**
- [ ] **Step 6: Commit `feat(session): add bounded spool and resumable uploader`.**

**Rollback:** Disable uploader and leave spool files untouched for later replay; Runner pending result behavior remains unchanged.

---

### Task 5: Add Independent Hub Session and Transcript Repositories

**Files:**
- Create: `hub/infrastructure/session_repository.py`
- Create: `hub/infrastructure/transcript_repository.py`
- Create: `tests/test_transcript_repository.py`
- Create: `tests/test_session_repository.py`
- Modify: `hub/config.py` only with additive session repository configuration

**Consumes:** Tasks 1 and 3. Existing observation repository, event repository and task repository must not be modified for session raw storage.

**Produces:**
- `SessionRepository` for session metadata, stream cursor, dedupe key and capability state;
- `TranscriptRepository.ingest(event) -> IngestResult` with accepted/duplicate/rejected/gap states;
- bounded redacted event stream;
- encrypted restricted raw stream;
- metadata/raw retention and quota rotation;
- audit append/read for restricted reads and control receipts;
- repository errors classified without exposing paths or exception text.

- [ ] **Step 1: Write failing tests.** Assert repository root is separate from state/task paths, event dedupe is idempotent, redacted data is queryable, raw data requires restricted key, quota/retention deletes only eligible raw data, and malformed records do not poison later records.
- [ ] **Step 2: Run focused repository tests and confirm failure.**
- [ ] **Step 3: Implement separate durable stores.** Use additive SQLite/file structures with explicit schema version; do not add transcript payloads to `events.jsonl` or ordinary task rows.
- [ ] **Step 4: Implement encryption and retention.** Fail closed when raw encryption key is absent; metadata queries remain available when raw storage is unavailable.
- [ ] **Step 5: Add failure-isolation tests.** Simulate raw write failure and assert observation repository calls remain successful.
- [ ] **Step 6: Commit `feat(session): add isolated transcript repositories`.**

**Rollback:** Disable session repository wiring; leave old observation/event/task stores and leases untouched. Preserve new files for a later compatible release.

---

### Task 6: Expose Session Ingest and Operator Query APIs

**Files:**
- Create: `hub/application/session_service.py`
- Create: `hub/http/session_routes.py`
- Create: `hub/http/v1/session_routes.py`
- Modify: `hub/bootstrap.py` for optional service/repository wiring
- Modify: `hub/config.py` for additive configuration
- Create: `tests/test_session_ingest_api.py`
- Create: `tests/test_session_query_api.py`
- Modify: `tests/test_http_contracts.py` for v1 alias and auth matrix

**Consumes:** Tasks 1 and 5. Existing `require_ingest_token`, `require_operator`, request IDs, bounded errors and public DTO conventions are authoritative.

**Produces:**
- `POST /api/session-events` using Observe credential and bounded event batches;
- `GET /api/sessions`, `GET /api/sessions/<id>`, `GET /api/sessions/<id>/events`, `GET /api/sessions/<id>/policy-signals` using Operator credential;
- `/api/v1/session-events` adapter to the same service;
- no `/api/v1/supervisor/*` route;
- `accepted_through`, `next_cursor`, per-event rejects and opaque request_id;
- failure isolation: transcript repository failure returns a bounded session error without breaking `/api/ingest`.

- [ ] **Step 1: Write failing HTTP contract tests.** Cover Observe success/403, Operator success/401, Runner denial, foreign-header no-dev-fallback, batch bounds, duplicate sequence, malformed event, bounded error body, public DTO allowlist, no raw transcript in SSE/status, v1 equivalence.
- [ ] **Step 2: Run focused tests and confirm route/service absence.**
- [ ] **Step 3: Implement service and thin blueprints.** Use `current_app.extensions["fleet"]["services"]`; no module-level credential or path lookup in new routes.
- [ ] **Step 4: Wire optional services without changing app startup when session feature is disabled.** Existing observation/task routes must register and behave exactly as before.
- [ ] **Step 5: Run all auth/HTTP/regression tests.**
- [ ] **Step 6: Commit `feat(session): add ingest and operator query APIs`.**

**Rollback:** Remove session blueprints from registration or disable the feature flag; old `/api/*`, `/api/v1/*`, observation and Runner routes remain available.

---

### Task 7: Implement Session Bridge Capture Adapters

**Files:**
- Create: `tools/session/bridge.py`
- Create: `tools/session/capture/native_jsonl.py`
- Create: `tools/session/capture/structured_stream.py`
- Create: `tools/session/capture/hooks.py`
- Create: `tools/session/capture/pty.py`
- Create: `tests/test_session_bridge.py`
- Create: `tests/test_session_capture_sources.py`

**Consumes:** Tasks 1-4 and Task 6 HTTP contract. Capability manifests are the only source of truth for which adapter path is allowed.

**Produces:**
- `SessionBridge.open(manifest, session_config) -> BridgeHandle`;
- source adapters mapping verified events to `session_schema`;
- byte-offset native JSONL tailing with checkpoint; structured stream parsing that rejects non-JSON noise; hook adapter that never forwards hook environment; PTY adapter with explicit best-effort quality;
- redaction before spool append;
- uploader integration with ack/replay;
- capture quality downgrade and `capture_gap` events.

- [ ] **Step 1: Write fixtures and failing tests.** Include Claude/Codex native records, malformed lines, structured stream noise, hook absence, PTY byte stream, source disconnect, quality downgrade and no Hermes structured spawn assumption.
- [ ] **Step 2: Run focused capture tests and confirm failure.**
- [ ] **Step 3: Implement source adapters.** Use only capability manifest flags; do not add hard-coded unsupported flags or arbitrary command construction.
- [ ] **Step 4: Implement Bridge orchestration.** Order is source read -> normalize -> redact -> append -> upload; a source failure creates bounded diagnostic/gap, not raw exception output.
- [ ] **Step 5: Run fake-source integration tests with Hub HTTP test client.**
- [ ] **Step 6: Commit `feat(session): bridge verified transcript sources into spool`.**

**Rollback:** Keep existing Runner adapter path; disable managed session capture and retain non-managed metadata behavior.

---

### Task 8: Implement Agent Supervisor Process Lifecycle

**Files:**
- Create: `tools/supervisor/model.py`
- Create: `tools/supervisor/supervisor.py`
- Create: `tests/test_supervisor_process.py`
- Modify: `tools/runner_config.py` with additive managed-session settings
- Modify: `tools/agent-runner.py` only to add an opt-in managed path, preserving default `--once` behavior

**Consumes:** Tasks 2, 3 and 7. Existing `BaseAdapter` process and watchdog behavior remains the compatibility path until managed mode is explicitly enabled.

**Produces:**
- `ManagedSessionManifest` containing opaque session/attempt IDs, process group capability, agent family, capability manifest and state;
- `Supervisor.launch(allowlisted_command, cwd, env_allowlist, ...)`;
- `pause_session`, `resume_session`, `terminate_session`, `quarantine_session`, `cancel_attempt` local methods;
- Linux process-group/cgroup capability detection and macOS process-group capability detection;
- graceful terminate then forced group terminate with explicit degraded outcome on escape;
- crash recovery from durable manifest;
- no automatic adoption of existing processes.

- [ ] **Step 1: Write failing process tests using fake child processes.** Cover group creation, manifest persistence, pause/resume, graceful/forced terminate, missing cgroup downgrade, escaped child detection, unknown target rejection and Supervisor restart.
- [ ] **Step 2: Run focused tests and confirm failure.**
- [ ] **Step 3: Implement platform abstraction.** Keep process-group operations behind a small interface so Linux and macOS tests can use fakes; do not invoke shell strings.
- [ ] **Step 4: Implement managed runner mode behind an explicit config flag.** Default Runner execution remains byte-for-byte compatible with current adapter path and heartbeat limits.
- [ ] **Step 5: Run existing Runner tests plus managed supervisor tests.**
- [ ] **Step 6: Commit `feat(supervisor): manage bounded CLI process groups`.**

**Rollback:** Set managed mode off; current Runner continues using existing adapters and task result contract.

---

### Task 9: Add Supervisor Credential, Signed Command Queue and Receipts

**Files:**
- Create: `hub/domain/control.py`
- Create: `hub/domain/supervisor.py`
- Create: `hub/application/supervisor_service.py`
- Create: `hub/http/supervisor_routes.py`
- Modify: `hub/auth.py` with `extract_supervisor_identity`, `require_supervisor`, and foreign-header isolation
- Modify: `hub/config.py`, `hub/bootstrap.py`, `hub/web.py` for additive credential/key loading
- Create: `tools/supervisor/control_client.py`
- Create: `tests/test_supervisor_control.py`
- Modify: `tests/test_auth_matrix.py`

**Consumes:** Tasks 1, 3, 5 and 8. Supervisor credential is not Runner credential; Hub signing public/private keys and raw encryption keys are separate capabilities.

**Produces:**
- fixed `CONTROL_ACTIONS` and `CONTROL_STATES` enums;
- canonical JSON command serialization;
- Ed25519 command signature generation/verification;
- Supervisor poll/receipt APIs;
- nonce, TTL, machine/session/attempt scope and idempotent receipt handling;
- audit records for enqueue, delivery, reject, execution, expiry and restricted reads;
- auth matrix proving Observe/Operator/Runner/Supervisor boundaries.

- [ ] **Step 1: Write failing tests.** Cover valid/invalid signature, canonical serialization changes, nonce replay, expiry, clock skew, machine mismatch, session/attempt mismatch, unsupported action, duplicate command/receipt, foreign credential headers, Runner denial, and bounded errors.
- [ ] **Step 2: Run focused control/auth tests and confirm failure.**
- [ ] **Step 3: Implement control domain and queue.** Conflict precedence is terminate/quarantine over resume; command status never implies execution before receipt.
- [ ] **Step 4: Implement auth and key loading.** Key values never appear in config responses, logs, exceptions or test output. Missing signing key disables command issuance rather than falling back to unsigned commands.
- [ ] **Step 5: Implement Agent control client.** It validates signature/scope/TTL locally, persists used nonces, executes only fixed actions through Supervisor, and uploads bounded receipts.
- [ ] **Step 6: Run all auth, HTTP contract, repository and control tests.**
- [ ] **Step 7: Commit `feat(supervisor): add signed pull-mediated control`.**

**Rollback:** Stop issuing Supervisor commands and leave existing Runner command polling active; unsigned or unknown commands are always rejected by the Agent.

---

### Task 10: Connect Task Cancel to Managed Attempt Termination

**Files:**
- Create: `hub/application/control_router.py`
- Modify: `hub/application/task_service.py` at the existing cancel transition
- Modify: `hub/http/task_routes.py` only if the service contract requires no route change
- Modify: `hub/bootstrap.py`
- Create: `tests/test_task_cancel_supervisor.py`
- Create: `tests/test_task_cancel_races.py`

**Consumes:** Existing task state machine plus Tasks 6, 8 and 9.

**Produces:**
- `enqueue_cancel_attempt(task_id, attempt_id, operator, reason_code) -> command_id | None`;
- cancellation that preserves existing state transitions and emits a control command only for a managed active attempt;
- receipt-driven finalization with `terminated`, `already_finished`, `failed`, `expired`, and `rejected` outcomes;
- attempt fencing that rejects stale Runner result/heartbeat after termination;
- no change to cancel behavior for unmanaged or already-terminal tasks.

- [ ] **Step 1: Write failing tests.** Cover queued/leased/running/terminal cancel rules, managed running cancel, unmanaged cancel compatibility, natural-completion race, stale heartbeat 409, stale result rejection, receipt retry and lease reconciliation.
- [ ] **Step 2: Run existing task API tests plus new focused tests and confirm the new integration failure.**
- [ ] **Step 3: Implement control routing after the existing task transition is accepted.** Do not let command queue failure mutate the old task state incorrectly; record bounded `control_pending`/`control_failed` audit state.
- [ ] **Step 4: Implement receipt callback with attempt fencing.** A late success cannot overwrite a cancelled/terminated attempt.
- [ ] **Step 5: Run full task/runner/lease tests.**
- [ ] **Step 6: Commit `feat(task): route managed cancel through supervisor`.**

**Rollback:** Disable cancel command enqueue while preserving existing task cancel and lease behavior; no task schema destructive migration is permitted.

---

### Task 11: Add Session Timeline, SSE Summary Events and Compatibility Contracts

**Files:**
- Create: `frontend/views/session.js`
- Modify: `frontend/api/contracts.js`
- Modify: `frontend/api/client.js`
- Modify: `frontend/realtime/sse.js` only for allowlisted summary event handling
- Modify: `frontend/state/store.js` if the existing store owns session state
- Modify: `frontend/index.html`/route manifest according to the current static frontend layout
- Create: `tests/test_session_frontend_contracts.py`
- Modify: `tests/test_release_layout.py`
- Modify: `hub/application/event_publisher.py` or session service only if required to emit summary events through the existing SSE publisher

**Consumes:** Tasks 6, 7, 9 and 10. Existing frontend route and source layout is authoritative; do not introduce a second build system.

**Produces:**
- `/session/<session_id>` timeline route;
- bounded DTO parser for session metadata/events/policy/control receipts;
- quality and managed-state badges;
- explicit gap, queued, executing, expired, failed and already-finished rendering;
- SSE summary events containing only session ID, cursor, sequence, kind, quality and safe metadata;
- no raw transcript, token, path, SQL or exception in frontend assets or rendered HTML.

- [ ] **Step 1: Write source-contract tests.** Assert no credentials/raw payloads, allowlisted fields only, bounded rendering, no dynamic class concatenation from server state, and exact/best-effort labels.
- [ ] **Step 2: Run focused frontend contract tests and confirm missing route/parser failure.**
- [ ] **Step 3: Implement API contracts and timeline view using existing native frontend conventions.** Full text is fetched through bounded Operator API pagination, never embedded in SSE.
- [ ] **Step 4: Add SSE summary handling without changing existing event names or reconnect semantics.**
- [ ] **Step 5: Run static release layout, XSS, routing, SSE and full frontend contract tests.**
- [ ] **Step 6: Commit `feat(frontend): add bounded managed-session timeline`.**

**Rollback:** Frontend-only rollback removes the session route while backend session ingest/control data remains intact; old pages and APIs continue working.

---

### Task 12: Migration, Rollback, Chaos and Production Acceptance Gates

**Files:**
- Create: `tests/test_session_chaos.py`
- Modify: `deploy/e2e-smoke.sh`
- Modify: `deploy/test-release-routing.sh` only for additive session route/SSE assertions
- Modify: `docs/HANDOFF.md` with the new data plane, managed/unmanaged boundary, release and rollback rules
- Modify: `docs/architecture-v4-control-plane.md` with an explicit extension/supersession note if needed
- Modify: `docs/superpowers/specs/2026-08-26-agent-session-supervision-design.md` only for verified implementation deviations

**Consumes:** Tasks 1-11 and the existing smoke/test/release scripts.

**Produces:**
- P0 capability-probe runbook;
- P1 shadow-mode session capture gate;
- P2 Supervisor-managed new task gate;
- P3 dry-run/pause/resume/terminate/quarantine control gates;
- P4 Operator timeline and policy gate;
- rollback switch and backend/frontend release verification;
- chaos tests for Hub unreachable, repository read-only, spool full, Agent crash before ack, Supervisor crash during terminate, stale/expired command, clock skew, signing key rotation, source disconnect, escaped child and frontend/backend rollback;
- documented production acceptance evidence with no credentials or raw transcript.

- [ ] **Step 1: Write failing chaos/acceptance tests.** Each scenario must assert both success and terminal failure behavior; silence is not a pass.
- [ ] **Step 2: Run local baseline before modifying smoke scripts.** Required commands: `.venv/bin/python -m unittest discover -s tests -v`, `python3 -m compileall -q connectors hub tools tests`, `bash deploy/e2e-smoke.sh`, `bash deploy/test-static-frontend.sh`, `bash deploy/test-release-routing.sh`.
- [ ] **Step 3: Implement local chaos fakes and smoke assertions.** Keep production credentials out of fixtures and output.
- [ ] **Step 4: Verify release packaging.** Backend package excludes frontend raw state/credentials; frontend package excludes backend state, transcript and credentials; backend release is tested before frontend release.
- [ ] **Step 5: Execute staged rollout in shadow mode first.** Observe upload/repository failures without enabling control. Then enable Supervisor for new tasks, then control actions in the order dry-run, pause/resume, terminate, quarantine.
- [ ] **Step 6: Exercise rollback.** Disable session feature, restore previous backend release, restore previous frontend release, and prove old observation/task/runner/SSE/lease contracts remain healthy.
- [ ] **Step 7: Update HANDOFF and architecture docs with verified facts only.** Unverified CLI capabilities and local secret paths must not be recorded.
- [ ] **Step 8: Commit `test(session): add rollout, rollback and chaos gates`.**

**Rollback:** Keep session feature disabled and retain spool/repository data; existing production release remains the fallback until all gates pass.

## Controller Review Gates

After each task, the controller performs two reviews before unblocking the next task:

1. **Spec compliance:** compare the task diff to its exact files, interfaces, bounds, auth and rollback requirements; reject any unverified CLI assumption or contract drift.
2. **Code quality/security:** check exception boundaries, raw secret/path leakage, bounded I/O, concurrency, platform behavior, test assertions and unrelated changes.

A task is not complete when its implementation agent reports success. It is complete only when focused tests, relevant existing regression tests and both reviews pass. The controller never resolves a conflict by weakening the spec silently; any approved deviation is recorded in the spec and plan before the next dependent task.

## Final Acceptance

The feature is accepted only when:

- all existing tests remain green;
- all new session/schema/probe/redaction/spool/repository/API/Supervisor/task/frontend/chaos tests pass;
- at least one real Claude or Codex capability probe produces the same manifest class used by the managed path;
- Hermes remains honestly best-effort unless a real probe proves a stronger source;
- a fake managed session survives crash/replay without duplicate or missing sequence;
- a real or platform-equivalent process-group test proves bounded terminate behavior;
- a signed command cannot be replayed, retargeted or executed after expiry;
- Runner credentials cannot read or control session data;
- observation/task persistence failure isolation is demonstrated;
- frontend and backend can be rolled back independently;
- no acceptance artifact contains a token, secret, password, raw transcript, SQL statement or filesystem path.
