# External Agent Workflow Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification checkpoints.

**Goal:** Connect explicitly registered external Agents to the existing Workflow coordinator with immutable admission pins, durable original-task recovery, bounded owned I/O, and truthful evidence/cancellation semantics.

**Architecture:** Extend the existing admission transaction with a small private external-binding table; keep execution states in workflow_dispatches/workflow_steps. A concrete ExternalStepExecutor translates the existing synchronous external I/O port into StepExecutor. The controlled Muse bridge gains journal-only lookup; it retains its original account, approval, single-writer and uncertain-send behavior.

**Tech Stack:** Python 3.10+, stdlib dataclasses/hashlib/json/sqlite3/asyncio, existing Flask/bootstrap, existing MuseBridge, run_owned_io and pytest. No new dependencies.

## Global Constraints

- SSOT: integration contract §7.2/§7.6 and workflow execution contract §5.2.
- All F5 gates remain default-off; construction performs no external I/O or worker start.
- UTF-8 without BOM; preserve primary worktree and every existing uncommitted change.
- No deletion, commit, push, deployment, real Muse/model/node calls, downloads, or Obsidian changes.
- No new scheduler, queue, execution-state machine, model tools, approval endpoint, or generic Facade.
- Initial slice rejects files/context/capability requirements/output specs before sending; full output export and real pilots remain open.
- Stable token and exact payload/configuration pins are mandatory. Query failure never means task absence.
- Native execution/validation/acceptance remain independent; external self-report cannot create validation or acceptance.
- Short transactions never span external I/O; synchronous operations have their own bounds and are joined on cancellation.
- O(1) trusted target lookup; indexed token lookup; ≤8 Workflow steps; backup scans keyset batches of 128.

---

### Task 1: Explicit lookup and deterministic external registration

**Files:**
- Create `hub/domain/external_agent.py` for the framework-free I/O contract; preserve existing tools imports for compatibility.
- Create `hub/domain/workflow_external.py` for immutable RegisteredExternalTarget and strict snapshot parsing.
- Modify `tools/platform/external_agents/base.py`, `muse.py` and `tools/browser_bridge/muse.py` for explicit lookup and non-secret configuration identity. Provision persistent journal identity through explicit `MuseBridge.initialize()` before registering a Workflow target; fingerprint reads never provision a missing journal.
- Test `tests/test_external_agent_adapter.py`, `tests/test_muse_browser_bridge.py`.

**Interfaces:**
`ExternalAgentAdapter.lookup(dispatch_token: str, resolved_contract: Mapping[str, Any]) -> ExternalTaskRef | None`. The concrete Muse implementation delegates to journal-only `MuseBridge.lookup(task_id, prompt, scope=workspace_id)` and validates the original prompt hash and scope. No additional lookup service is needed.

- [x] Add lookup tests before implementation and capture missing-interface failures with JUnit.
- [x] Implement journal-only lookup; absent record returns None, changed scope/prompt conflicts, bridge errors preserve causes.
- [x] Implement strict registered-target snapshot/digest and adapter configuration digest; exact booleans and positive safe revisions, no raw config/secret storage.
- [x] Verify actual Muse journal with injected transport:
```python
assert bridge.lookup("task-1", "Find facts", scope="workspace") is None
original = bridge.submit("task-1", "Find facts", scope="workspace")
sent = len(transport.sends)
assert bridge.lookup("task-1", "Find facts", scope="workspace") == original
assert len(transport.sends) == sent
```
Run `PYTHONPATH=. python3 -m pytest tests/test_external_agent_adapter.py tests/test_muse_browser_bridge.py -q` and require no failures.

### Task 2: Atomic admission pins and compatible backup reader

**Files:**
- Create `hub/infrastructure/workflow_external_schema.py` and `workflow_external_repository.py`.
- Modify `hub/infrastructure/workflow_schema.py`, `workflow_repository.py`, `platform_backup.py`.
- Test `tests/test_external_agent_workflow.py` and `tests/test_external_agent_persistence.py`.

**Interfaces:** `WorkflowRepository.admit_step(..., external_target: RegisteredExternalTarget | None = None)` saves the exact target with the original intent/slot/budget in one transaction. `ExternalWorkflowRepository.init()`, `.prepare_send(...)`, `.lookup(...)`, `.bind_task(...)`, `.validate_snapshot(connection)` own only private metadata, not dispatch state.

- [x] Add failing tests for absent pins, mismatched target/payload, rollback, restart/config drift, malformed/future metadata, gate-off recognition, and plain/encrypted restore.
- [x] Implement reader 3 without weakening the existing requirement that reader 2 requires valid capability metadata.
- [x] Store send_started_at before I/O and immutable task_snapshot afterwards; exact target digest, scope, payload and original generation are checked on every read/write.
- [x] Validate all historical rows with indexed joins and keyset batches of 128. A task snapshot requires a send intent; original stage may remain sending/unknown after a crash.
- [x] Verify pinned target is part of original transaction:
```python
with repository.transaction(write=False) as connection:
    row = connection.execute("SELECT target_snapshot,target_digest FROM workflow_external_bindings WHERE token=?", (token,)).fetchone()
    assert bytes(row[0]) == target.snapshot
    assert row[1] == target.digest
```
Run the persistence module plus workflow repository, capability reservation and platform backup suites; require no regressions.

### Task 3: Concrete executor, coordinator and passive assembly

**Files:**
- Create `hub/application/workflow_external_execution.py`.
- Modify `hub/application/workflow_coordinator.py`, `hub/domain/workflow_execution.py`, `hub/infrastructure/workflow_repository.py`, `workflow_operator_repository.py`, `workflow_bootstrap.py`, `workflow_dispatch.py`, and `tools/async_io.py`.
- Modify `frontend/assets/api/contracts.js` and `frontend/assets/views/assistant/workflow.js` for the evidence_source enum and clear external-state copy.
- Test `tests/test_async_io.py` and `tests/test_frontend_workflow_contracts.py` through actual HTTP payloads.
- Test `tests/test_external_agent_workflow.py` and `tests/test_external_agent_persistence.py`.

**Interfaces:** `ExternalStepExecutor(repository, target, adapter, *, clock, enabled=False)` implements existing create_or_lookup/lookup/inspect/request_cancel. Administrator bootstrap dependency `workflow_external_agents` is a tuple of `(RegisteredExternalTarget, ExternalAgentAdapter)`; RegisteredWorkflowStep/Plan accepts an optional external_target admission pin. Gate-off adapters retain original lookup/inspect, while new admission/submit are deferred.

- [x] Add failing tests for initial submit, duplicate payload conflict, all three crash windows, lookup errors, non-idempotent replay denial, active lease/control/deadline/held-slot guards, owned-I/O cancellation and two-process races.
- [x] Implement target/configuration/capability checks before external I/O, full canonical prompt with an 8192-character limit, and no DB transaction during I/O. Recheck the original admission after lookup and immediately before submit. `wait_owned` retains the child task through Python 3.10 repeated cancellation/timeout; worker.result preserves the cancellation cause lost by shield wrappers.
- [x] Persist evidence_source on observations/events; map waiting_input to waiting_external with external_input_required. Status and cancel receipts must match both original external_id/session_id fields, including session-only tasks. Only confirmed cancellation for the original task can stop it.
- [x] Connect admission pins and external gate to the original coordinator; build adapters passively from explicit admin dependencies, preserving default-off behavior.
- [x] Verify provenance and separation:
```python
assert observed.state == "succeeded"
assert observed.evidence_source == "agent_reported"
assert repository.get(OWNER, "workflow-a").acceptance_state == "pending"
with repository.transaction(write=False) as connection:
    assert connection.execute("SELECT count(*) FROM workflow_evidence").fetchone()[0] == 0
```
Run both external modules and coordinator/control/gate/Muse regressions on Python 3.10.21 and 3.13.7; require no failures.

### Task 4: Acceptance registration and current evidence

**Files:** `docs/testing/journeys.json`, `test-chains.md`, `workflow-platform-acceptance.md`, `docs/platform/workflow-platform/implementation-progress.md`, `README.md`, and F5/roadmap plans.

- [x] Register every new selector in WF-06; retain EXTERNAL-AGENT-LIVE as not_run.
- [x] Run dual-runtime compile checks, UTF-8/no-BOM and local-link checks, frontend CJS syntax checks and git diff --check.
- [x] Run the full strict browser gate with JUnit/journey/screenshots and source digests before/after. Linux-only pidfd/proc evidence remains an explicit macOS limitation; no false all-green claim.
- [x] Record exact commands, failures/repro loops, final counts and genuine limitations. Keep full platform objective active while wider code/real-platform requirements remain.

## Review checkpoint

- [x] Every §7.6 requirement maps to one task above; no new external authority from untrusted bodies.
- [x] No placeholder implementation, secret-bearing snapshot, silent catch, independent validation from self-report, or replay on lookup failure.
- [x] Current code and matrix match the final evidence digest; gate defaults remain unchanged.

## Verified local evidence (2026-10-25)

Python 3.10.21 and 3.13.7 each pass the final 20-module related set with **522 passed / 2 skipped**. The related commands omitted the Playwright path, so two Muse browser fixtures skipped there; both pass in the browser-enabled Python 3.13 full gate. Compileall passes on both interpreters; no static type checker is configured, so compilation is not type proof.

The final uninterrupted full browser gate has **4836 passed, zero failures/errors, 5 platform skips, and 195 passing subtests in 603.31 seconds**. WF-06 has **128 selectors / 261 cases**, all passing. There are 41 passing journeys and one incomplete journey: RELEASE-01 lacks three required Linux pidfd/proc cases on macOS; two additional Supervisor proc cases also skip. Strict exit remains **1**, ci_status remains **failed**, and all seven external checks remain **not_run**.

Evidence: `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-external-workflow-full-20261025-87lg7q77/`. All 814 Git file digests match before/after the run; the matrix SHA-256 is `466d9f58c64d2b83fedeba332ceb666cbd5421e16afd337b37e93291721db27c`. All 28 PNG signatures, chunk CRCs, dimensions and IEND markers verify. Documentation-only changes after the gate are checked separately; runtime, tests and the matrix keep their frozen digests.

The [progress entry](../../platform/workflow-platform/implementation-progress.md#外部-agent-workflow-派发与原任务恢复2026-10-25) preserves failing evidence, the repeated-cancellation and session-identity repairs, exact full-gate command, and the earlier operator-interrupted run. That interrupted run is not a production defect reproduction or final validation. Historical intermittent browser/pipe timing failures still have no proven root-cause fix.

This completes the local dispatch/recovery plan. File/context inputs and output specifications remain explicitly bounded by their dedicated contracts; Node capability requirements are now checked against the immutable registered adapter capability set before admission and never silently ignored. Muse cancel/export are unsupported. Persistent multi-process catalog/identity administration, configuration UI, Linux/Windows, approved real nodes/models/CLI, production isolation and live MCP/Muse pilots remain open under the wider platform plan. All gates stay default-off; no real external calls, commits, push, merge or deployment occurred.

The [text artifact input plan](2026-10-25-external-agent-text-inputs.md) delivers complete bounded UTF-8 artifact data, immutable private requests committed with the original send intent, exact journal prompt recovery, current metadata authorization before permitted sends, and external schema/reader 2 with Workflow reader 5. Legacy no-input prompts and schema-1 gate-off reads remain exact. Node receive/alias checks continue for Node steps and do not block external Hub text. The final dual-Python twenty-module sets each pass 688 cases with no skips; full gate 5172 passed、0 failures/errors、5 skipped、195 subtests passed, WF-06 271/597, strict exit 1 for Linux RELEASE-01, all seven external checks not_run. Binary/code bundles, Memory/summary and structured output export are covered by their dedicated follow-up plans; the external capability requirement admission is now implemented and pinned to the registered adapter capability snapshot. Real pilots remain open.
