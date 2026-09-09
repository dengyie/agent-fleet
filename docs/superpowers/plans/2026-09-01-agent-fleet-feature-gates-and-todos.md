# Agent Fleet Feature-Gate Rollout and TODO Closure Plan

> **Status (2026-09-08): historical / implemented.** Open checkboxes below are the original plan and were not kept in sync after merge. Do not re-implement from unchecked boxes. Remaining work is recorded in `docs/HANDOFF.md` §六 as `done` or `deferred-with-condition`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Safely publish the merged agent discovery, session, supervisor, adoption, and exact-capture capabilities in independently reversible stages, then close or explicitly defer the remaining long-term TODOs.

**Architecture:** Ship code with every new route default-off. Enable only the smallest dependency-complete slice through `<FLEET_HOME>/fleet-gates.conf`, with environment values taking precedence and explicit false always winning. Each stage has a bounded smoke test, a monitoring window, and a rollback that disables only that stage before any database or task-lease migration.

**Tech Stack:** Python/Flask, SQLite/WAL, Ed25519 supervisor channel, bash release symlinks, curl, pytest, SSE, native ES modules.

**Spec:** `docs/superpowers/specs/2026-08-29-agent-instance-discovery-adoption-design.md`; rollout constraints in `docs/HANDOFF.md` and `docs/superpowers/plans/2026-08-30-agent-discovery-adoption-plan.md`.

## Global Constraints

- Feature gates are default-off, independently resolvable, and explicit false wins over env/file truthy values.
- Never enable a dependent gate before its required store, route, credential source, and rollback target are verified.
- Do not print or export ingest tokens, runner credentials, supervisor credentials, signing keys, cookies, raw transcripts, or full process identities.
- Discovery is metadata-only; no session content is collected before explicit adoption.
- Adoption is operator-explicit; no automatic adoption or control command is allowed.
- `detach` never signals; `terminate` only acts after current `(pid, started_at, exe_path)` identity validation and adopted-state validation.
- Hub remains push-only toward machines; all machine-bound commands use the signed supervisor poll/receipt channel.
- Raw exact capture is disabled unless encryption, quota, retention, audit, and operator authorization checks all pass.
- Every stage is stopped on a 5xx, unexpected 404/405, auth-domain mismatch, unbounded response, process signal, data-store cross-contamination, or failed rollback.
- Preserve observation JSONL, task SQLite, session metadata, adoption SQLite, transcript root, and task leases across code rollback.

---

## File Structure

Runtime gate resolution:

- `hub/web.py`: resolves env/file/kwarg precedence and records app gate state.
- `hub/config.py`, `hub/bootstrap.py`: derives isolated stores and conditionally registers services/routes.
- `tests/test_feature_gate_runtime.py`: default-off, precedence, and route registration contracts.

Discovery and observation:

- `tools/probe/discovery.py`, `report_schema.py`, `tools/probe_collectors.py`: local discovery and bounded payload.
- `hub/application/observe_service.py`, `hub/http/observe_routes.py`, `frontend/api/contracts.js`, `frontend/views/machine.js`: ingest and render metadata.

Session and supervisor:

- `hub/infrastructure/session_repository.py`, `hub/infrastructure/transcript_repository.py`, `tools/session/*`: isolated session data path.
- `hub/http/session_routes.py`: session query/event endpoints.
- `hub/http/supervisor_routes.py`, `hub/application/supervisor_service.py`, `tools/supervisor/*`: signed poll/receipt control path.

Adoption and exact capture:

- `hub/infrastructure/adoption_repository.py`, `hub/application/adoption_service.py`, `hub/http/adoption_routes.py`: explicit adoption lifecycle.
- `tests/test_adoption_e2e.py`, `tests/test_adoption_lifecycle.py`, `tests/test_exact_capture_upgrade.py`: safety contracts.

Operational validation:

- `deploy/package-release.sh`, `deploy/hk-container-adopt-release.sh`, `deploy/hk-container-inspect.sh`, `deploy/e2e-smoke.sh`.
- Obsidian canonical notes and incident record after each confirmed stage.

---

## Phase 0: Release readiness and safe baseline

### Task 1: Build and verify a gate-off release

**Files:**
- Inspect: `deploy/package-release.sh`, `hub/web.py`, `hub/config.py`, `hub/bootstrap.py`
- Test: `tests/test_feature_gate_runtime.py`, full test suite
- Runtime: a new release directory and a preserved known-good symlink target

**Interfaces:**
- Consumes: merged PR #1 and the recovery plan’s stable supervisor path.
- Produces: a release that starts with all new gates off and retains all legacy observation/task/runner routes.

- [ ] **Step 1: Assert required release members without credentials**

```bash
bash deploy/package-release.sh /tmp/agent-fleet-rollout.tgz
for path in agent_profiles.py hub/web.py tools/session/__init__.py tools/supervisor/supervisor.py; do
  tar -tzf /tmp/agent-fleet-rollout.tgz | grep -qx "$path" || {
    printf 'missing release member: %s\n' "$path" >&2
    exit 1
  }
done
! tar -tzf /tmp/agent-fleet-rollout.tgz | grep -E '(^|/)(credentials|state|var)(/|$)|(^|/)(.*token|.*credential|.*\.key)$'
```

Expected: runtime code is present and credential/state/runtime stores are absent.

- [ ] **Step 2: Verify all gates are off on a temporary app**

```bash
python3 -m pytest -q tests/test_feature_gate_runtime.py -k 'default or explicit_false'
```

Expected: no session, supervisor, or adoption route is registered when unset or explicitly false.

- [ ] **Step 3: Verify legacy compatibility**

```bash
python3 -m pytest -q tests/test_observability.py tests/test_runner.py tests/test_task_http.py
python3 -m compileall -q hub tools connectors tests
```

Expected: existing ingest, task, runner, and SSE behavior remains green.

- [ ] **Step 4: Record the release and rollback IDs**

Record only release identifiers and symlink targets, never credential paths’ contents. Do not switch production `live` until recovery Task 4 has passed.

- [ ] **Step 5: Commit or publish the release preparation**

```bash
git diff --check
git status --short
```

Expected: no unintended source changes.

**Rollback:** retain the current known-good release; no gate changes have occurred.

---

## Phase 1: Discovery metadata only

### Task 2: Enable and verify additive discovery

**Files:**
- Runtime: `fleet-gates.conf` only if a dedicated discovery gate exists; otherwise the merged discovery code is additive and no control gate is enabled.
- Test: `tests/test_discovery.py`, `tests/test_instance_schema.py`, `tests/test_instance_ingest.py`, `tests/test_frontend_adoption_contracts.py`.
- Docs: `README.md`, `docs/HANDOFF.md`, canonical rollout note.

**Interfaces:**
- Consumes: `discover_instances()` and `sanitize_instances()`.
- Produces: bounded `instances[]` metadata in snapshots; no session events, adoption records, supervisor commands, or transcript writes.

- [ ] **Step 1: Verify metadata allowlist locally**

```bash
python3 -m pytest -q tests/test_discovery.py tests/test_instance_schema.py tests/test_instance_ingest.py tests/test_frontend_adoption_contracts.py
```

Expected: only the documented bounded fields are accepted; unknown fields and oversized values are dropped or truncated.

- [ ] **Step 2: Verify no content/control side effects**

Run the probe against synthetic process rows and assert it does not open transcript files, emit supervisor commands, or send signals. Use test doubles only; do not attach to a real process.

- [ ] **Step 3: Enable the additive surface**

Publish the backend release first. If the implementation provides a discovery-specific runtime switch, set only that switch; otherwise leave all three control-plane gates off. Never enable adoption merely because instances appear.

- [ ] **Step 4: Verify public bounded responses**

```bash
for path in /api/status /api/machines/<bounded-machine>; do
  curl -sS -o /dev/null -w "$path=%{http_code}\n" "https://hub.example.com$path" || true
done
```

Expected: status and machine APIs are successful according to their auth contract; no response body is saved or printed.

- [ ] **Step 5: Monitor one observation interval**

Confirm repeated snapshots remain bounded, no raw process identity leaks into public projections/audit, and legacy ingest/task/SSE probes remain healthy.

- [ ] **Step 6: Record stage evidence**

Update the canonical operational note with release id, bounded status codes, gate values, and rollback target. Do not include raw instance rows.

**Rollback:** disable only the discovery-specific switch if one exists, or roll back the backend release; keep all control-plane gates off.

---

## Phase 2: Session repository and redacted event path

### Task 3: Enable session repositories without supervisor/adoption

**Files:**
- Runtime: session gate setting only.
- Test: `tests/test_session_chaos.py`, session route and repository tests, `deploy/e2e-smoke.sh` route assembly section.
- Docs: `docs/HANDOFF.md`, `README.md`.

**Interfaces:**
- Consumes: isolated session DB/transcript root, encryption key source if raw storage exists.
- Produces: `/api/session-events`, `/api/sessions`, and bounded session queries; default event quality is redacted/best-effort; no adoption or process control.

- [ ] **Step 1: Verify prerequisites without exposing key material**

Check that the session metadata DB path and transcript root are distinct from `state/` and task DB. Verify key source existence and permissions only; do not print key bytes or file content.

- [ ] **Step 2: Run session chaos and route tests**

```bash
python3 -m pytest -q tests/test_session_chaos.py tests/test_session_repository.py tests/test_transcript_repository.py tests/test_session_http.py
```

Expected: store failures are fail-closed and isolated; spool quota produces structured bounded failure; raw output is never returned without the existing audit/crypto contract.

- [ ] **Step 3: Enable only the session gate**

Set `AGENT_FLEET_SESSION_REPOSITORIES_ENABLED=1` in the approved runtime gate file. Do not set supervisor or adoption gates. Restart only through the documented supervisor owner if the application must reload configuration.

- [ ] **Step 4: Verify routes and auth by status code**

```bash
for path in /api/session-events /api/sessions; do
  curl -sS -o /dev/null -w "$path=%{http_code}\n" "https://hub.example.com$path" || true
done
```

Expected: routes are present and protected according to their documented operator/machine auth; no route returns an unexpected 5xx.

- [ ] **Step 5: Verify legacy isolation**

Probe `/api/status`, `/api/stream`, task list, and runner status. Confirm observation JSONL and task DB modification rates are unchanged by a session repository failure.

- [ ] **Step 6: Monitor and record**

Observe at least one full reconciliation interval and one session upload/ack cycle using bounded counters only. Record failures by fixed code, not natural-language exception text.

**Rollback:** set the session gate explicitly to `0`, restart through the same owner, and verify legacy routes. Keep session DB/transcript files for forensic rollback; do not delete them.

---

## Phase 3: Supervisor poll/receipt in fail-closed mode

### Task 4: Enable supervisor transport before any adoption

**Files:**
- Runtime: supervisor gate, credential source, signing key source (existence/permissions only).
- Test: `tests/test_supervisor_control.py`, `tests/test_supervisor_http.py`, `tests/test_session_chaos.py`.
- Docs: supervisor deployment and rollback sections.

**Interfaces:**
- Consumes: machine-bound supervisor credential, Ed25519 signing key, nonce/TTL/clock validation.
- Produces: signed poll/receipt route surface; no commands are issued until credentials, key, machine scope, and test receipt path all pass.

- [ ] **Step 1: Verify key/credential readiness without reading values**

Check source paths, ownership, mode, and non-empty status through a privileged operator procedure. Never print key or credential bytes.

- [ ] **Step 2: Run the negative auth matrix**

```bash
python3 -m pytest -q tests/test_supervisor_control.py tests/test_supervisor_http.py -k 'auth or signature or nonce or scope or clock'
```

Expected: invalid/missing credentials and signatures return fixed bounded codes; no natural-language secret/path data is returned.

- [ ] **Step 3: Enable only supervisor**

Set `AGENT_FLEET_SUPERVISOR_ENABLED=1`; keep adoption off. The service must start even without an issuance key in fail-closed mode, but production rollout requires the key source to be independently verified before testing a signed command.

- [ ] **Step 4: Verify no command issuance**

Poll with a synthetic machine credential in a temporary app and assert bounded rejection. Verify the command queue remains empty and no target process receives a signal.

- [ ] **Step 5: Verify receipt idempotency**

Use synthetic signed fixtures to submit one receipt twice; assert the second receipt is idempotent and bounded. Do not use production machine ids or credentials in local fixtures.

- [ ] **Step 6: Monitor and record**

Monitor route status, rejected-poll codes, nonce replay counters, and process signal count. Any unexpected command, signal, or 5xx stops the rollout.

**Rollback:** set supervisor gate to `0`, restart through the documented owner, and verify session/observation routes remain unaffected.

---

## Phase 4: Explicit adoption and guarded control pilot

### Task 5: Pilot one synthetic or explicitly approved instance

**Files:**
- Runtime: adoption gate plus the already-enabled session/supervisor dependencies.
- Test: `tests/test_adoption_http.py`, `tests/test_adoption_control_http.py`, `tests/test_adoption_control_guard.py`, `tests/test_adoption_e2e.py`, `tests/test_adoption_lifecycle.py`.
- Docs: operator adoption SOP and audit record.

**Interfaces:**
- Consumes: a bounded discovered candidate, operator auth, signed pull transport, and isolated adoption store.
- Produces: pending → adopted lifecycle for one approved candidate; control actions remain fixed and guarded; detach is signal-free.

- [ ] **Step 1: Verify adoption prerequisites**

Confirm session and supervisor gates are on, operator auth is available, the adoption DB is separate, and the candidate row is fresh/attachable. Do not copy candidate raw identity fields into chat or notes.

- [ ] **Step 2: Run the full adoption safety tests**

```bash
python3 -m pytest -q tests/test_adoption_http.py tests/test_adoption_control_http.py tests/test_adoption_control_guard.py tests/test_adoption_e2e.py tests/test_adoption_lifecycle.py
```

Expected: unauthenticated adoption fails, stale/pid-reused/exe-drift candidates fail closed, duplicate adopt is idempotent, detach emits no signal, and only guarded terminate can signal.

- [ ] **Step 3: Enable adoption only after explicit operator approval**

Set `AGENT_FLEET_ADOPTION_REPOSITORIES_ENABLED=1` in the gate file. Do not enable exact capture. Select one approved candidate via the operator UI/API; never auto-adopt all discovered rows.

- [ ] **Step 4: Verify pending and receipt transitions**

Observe only bounded status projection: pending, accepted/adopted, rejected, retryable, or revoked. If the pending-recovery fix is not merged, do not proceed; use the explicit revoke/retry path and stop.

- [ ] **Step 5: Exercise detach and drift**

Use the approved pilot to verify detach and drift revocation. Assert no process signal and one bounded audit action for each lifecycle transition. Do not test terminate on a production-critical agent during the first window.

- [ ] **Step 6: Monitor and record**

Monitor adoption counts, fixed rejection codes, duplicate command count, signal count, DB errors, and route 5xx. Keep raw identity and transcript data out of monitoring logs.

**Rollback:** set adoption gate to `0`, restart through the same owner, and leave session/supervisor gates on only if their independent routes remain healthy; otherwise disable them in reverse dependency order.

---

## Phase 5: Exact capture and operator UI/policy

### Task 6: Enable exact capture only after a bounded upgrade review

**Files:**
- Runtime: exact-capture operator action; no new global gate unless one is explicitly implemented.
- Test: `tests/test_exact_capture_upgrade.py`, `tests/test_adoption_e2e.py`, frontend contract tests.
- Docs: raw-read authorization, encryption, quota, retention, and audit policy.

**Interfaces:**
- Consumes: adopted session, explicit operator identity, encryption key, quota/retention settings, and raw-read audit mechanism.
- Produces: `capture_quality=exact` only for the selected session; default remains `best_effort` for all others.

- [ ] **Step 1: Verify crypto/quota/retention prerequisites**

Check key source, directory permissions, quota, retention job, and audit sink existence without reading secrets or raw transcript content. If any prerequisite is unavailable, remain best-effort.

- [ ] **Step 2: Run exact-capture tests**

```bash
python3 -m pytest -q tests/test_exact_capture_upgrade.py tests/test_adoption_e2e.py
```

Expected: unauthenticated/unknown/pending/revoked requests fail with bounded codes; repeated exact upgrade is idempotent; raw-read audit remains required.

- [ ] **Step 3: Upgrade one non-critical adopted session**

Use the operator-authenticated state-transition endpoint with an empty body. Do not send raw pid/path/command fields. Verify only bounded quality/status output.

- [ ] **Step 4: Verify bounded UI rendering**

```bash
python3 -m pytest -q tests/test_frontend_adoption_contracts.py
bash deploy/test-static-frontend.sh
```

Expected: UI renders quality/status and fixed error codes; no raw transcript or credential is embedded in frontend assets.

- [ ] **Step 5: Monitor retention and audit**

Confirm exact sessions remain within quota, raw-read access creates one audit event, and disabled/revoked sessions stop new capture. Do not export transcript content.

**Rollback:** revoke exact upgrade or disable exact-capture action; preserve encrypted data for retention-compliant handling. If a code regression exists, disable adoption before backend rollback.

---

## Long-term TODO closure

### Task 7: Decide and record the remaining capabilities

**Files:**
- Modify: `README.md`, `docs/HANDOFF.md`, `docs/architecture-v4-control-plane.md`, `docs/architecture-review.md`
- Modify: canonical Obsidian agent-fleet notes and project index after router reread
- Test/doc: no new runtime code unless a separate approved plan is created

- [ ] **Step 1: Close or explicitly defer file-by-file task reads**

Keep `GET /api/tasks/<id>/files/<path>` deferred unless a runner-side bounded file-return protocol exists. If retained, specify path allowlist, symlink rejection, byte/line limits, redaction, rate limit, and audit event before implementation.

- [ ] **Step 2: Close or explicitly defer WebSocket**

Retain SSE plus bounded polling as the supported realtime contract unless measured requirements exceed it. If WebSocket becomes necessary, create a separate architecture/spec plan covering auth, proxy, reconnect, origin, and downgrade behavior.

- [ ] **Step 3: Prioritize additional collectors**

Add more agent-specific collectors only when a concrete machine/use case exists; each collector must publish bounded metadata, have isolated failure handling, and add fixture tests. Do not add collectors for speculative coverage.

- [ ] **Step 4: Define operational follow-ups**

Create separate tracked work for credential rotation after the historical plaintext SSH password exposure, guardian auto-restart hardening, container permission policy, and production backup/restore drills. These are operational security tasks, not feature-gate tasks.

- [ ] **Step 5: Update the authoritative TODO list**

For every item record exactly one state: `done`, `deferred-with-condition`, or `blocked-by-operator-access`, with owner, evidence, next trigger, and rollback. Do not leave vague “later” or “TBD” entries.

### Task 8: Final release and incident closure

**Files:**
- Modify: canonical Obsidian incident note
- Modify: `docs/HANDOFF.md`, `README.md`
- Test: complete local and production bounded verification matrix

- [ ] **Step 1: Run the full local suite**

```bash
python3 -m pytest -q
python3 -m compileall -q hub tools connectors tests
bash deploy/test-static-frontend.sh
bash deploy/test-release-routing.sh
```

Expected: all pass with recorded counts.

- [ ] **Step 2: Verify production status and route matrix**

```bash
for path in /api/status /api/sessions /api/adoptions /api/supervisor/poll; do
  curl -sS -o /dev/null -w "$path=%{http_code}\n" "https://hub.example.com$path" || true
done
```

Expected: status is 200; gated routes return the documented auth/status code for their current stage and never an unexplained 404/5xx.

- [ ] **Step 3: Verify rollback readiness**

Confirm the previous release, current release, live symlink, guardian owner, and isolated store paths are known. Do not execute rollback merely as a test against production; perform a dry-run metadata check.

- [ ] **Step 4: Close the incident only with evidence**

Update the incident status to closed only after the public route and guardian stability criteria pass. Record actual PR #1, merged commit, release IDs, gate stages, and unresolved operator-access blockers without secrets.

- [ ] **Step 5: Run Obsidian health check**

```bash
cd "$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/obsidian-note"
python3 .local/bin/scan-stale-docs
```

Expected: no broken links or stale canonical deployment claims.

**Rollback:** reverse the gate order—exact capture, adoption, supervisor, session—then switch backend release only if legacy compatibility checks remain green.

---

## Completion Gate

- Discovery metadata is observable without enabling any control-plane gate.
- Session, supervisor, adoption, and exact capture are enabled only after their prerequisites and tests pass.
- Every stage has bounded status/error evidence, monitoring, and an independent rollback.
- No process receives a signal from discovery, adopt, detach, drift, or revoke; only guarded terminate may signal.
- No secrets or raw transcripts appear in logs, docs, responses, or frontend assets.
- Remaining TODOs are either closed or recorded as condition-bound deferrals with an owner and trigger.
- Canonical Obsidian notes and repository docs agree on topology, gates, release IDs, and rollback order.
