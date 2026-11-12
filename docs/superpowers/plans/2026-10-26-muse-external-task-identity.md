# Muse External Task Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification checkpoints. Steps use checkbox (`- [x]`) syntax.

**Goal:** Prevent a Muse submit receipt or historical task reference from selecting a different journal task than the original dispatch token.

**Architecture:** Enforce the controlled Muse bridge's existing stable identity at its adapter boundary. Submission must return the exact dispatch token as task_id; subsequent task operations require that same external_id and no unsupported session identity. Keep the generic external Agent task/session model and original Workflow recovery state machine.

**Tech Stack:** Python 3.10+, stdlib, pytest, existing Muse bridge and SQLite Workflow fixtures. No new dependencies.

## Global Constraints

- Continue in the existing isolated feature worktree and preserve unrelated changes.
- Preserve token/payload, unknown, held/isolated reservations and exact retry rules.
- Never query a foreign task or turn a malformed receipt into absence/replay proof.
- Muse cancel/export remain unsupported; all gates remain off by default.
- No schema, reader, API, generic ExternalTaskRef or configuration-digest changes.
- UTF-8 without BOM; no commits, push, merge, deployment or live external calls.

### Task 1: Document and reproduce the mismatch

**Files:**
- Modify: `docs/superpowers/specs/2026-10-20-artifact-capability-integration-contract.md`, §7.6.
- Create: `tests/test_muse_external_identity.py`.

**Interfaces:** `MuseExternalAgentAdapter.submit(token, contract)` returns a reference only for task_id=token. `inspect`, `request_cancel` and `export_outputs` reject references outside Muse's journal identity before bridge I/O. Generic adapters retain independent task/session IDs.

- [x] Clarify the existing journal identity invariant and uncertain-submit behavior in §7.6.
- [x] Test foreign submit receipts for sent/running/waiting_input/completed/failed/dispatching, missing identities and unknown state. Assert exact error code, uncertain=True, one send and no read.
- [x] Test inspect/cancel/export against foreign external_id, missing external_id, unsupported session and foreign agent; assert rejection before bridge I/O. Retain valid token roundtrip with a real journal.
- [x] Test a foreign receipt after one real Workflow send: no saved task or execution binding, persistent unknown and held reservation. Explicit recovery looks up the original journal token, creates no second send and does not auto-reconcile the original unknown.
- [x] Run `PYTHONPATH=. python3.13 -m pytest tests/test_muse_external_identity.py -q --tb=short --junitxml=/tmp/agent-fleet-muse-identity-red-20261026.xml` before production changes; record actual failures.

### Task 2: Enforce the existing adapter identity

**Files:**
- Modify: `tools/platform/external_agents/muse.py`.
- Modify: `tests/test_external_agent_adapter.py`.

**Interfaces:** Keep existing signatures and causes. Foreign submit raises `ExternalAgentError("external_task_mismatch", uncertain=True)`; malformed existing reference raises `external_task_mismatch` before I/O. Missing identity remains `dispatch_identity_missing`; unknown remains `dispatch_unknown`.

- [x] After the existing missing-identity guard, require the exact token:

```python
if external_id != token:
    raise ExternalAgentError("external_task_mismatch", uncertain=True)
return ExternalTaskRef(self.agent_id, token, external_id=external_id)
```

- [x] Require Muse's exact journal identity on subsequent calls:

```python
if (not isinstance(task, ExternalTaskRef) or task.agent_id != self.agent_id
        or task.external_id != task.dispatch_token or task.session_id is not None):
    raise ExternalAgentError("external_task_mismatch")
```

- [x] Align the original fake bridge's valid task references with the real bridge invariant; retain foreign-receipt rejection tests.
- [x] Run `PYTHONPATH=. python3.13 -m pytest tests/test_muse_external_identity.py tests/test_external_agent_adapter.py tests/test_external_agent_workflow.py -q --tb=short --junitxml=/tmp/agent-fleet-muse-identity-green-20261026.xml`.

### Task 3: Register and verify

**Files:**
- Modify: `docs/testing/journeys.json`, `docs/testing/README.md`, `docs/testing/test-chains.md`.
- Modify: `docs/platform/workflow-platform/implementation-progress.md`.

- [x] Add all new selectors to required WF-06 without removing existing selectors.
- [x] Run serial Python 3.10/3.13 related suites covering dispatch/recovery, external inputs, Muse integration and backups; preserve logs/JUnit.
- [x] Run UTF-8/BOM, AST/compilation, local-link and diff checks. Compilation is not static type proof.
- [x] Run the strict full journey gate against frozen sources; record actual exit/skips and external not_run checks.
- [x] Record red/green evidence, remaining platform/live requirements and the pending structured-output design choice. Correct the stale F5 summary listing implemented explicit Memory as entirely open.

## Recorded evidence

- Red before the production fix: 16 failed / 10 passed, exit 1; `/tmp/agent-fleet-muse-identity-red-20261026.xml`.
- Initial green across the new regression, adapter and Workflow suites: 98 passed, exit 0; `/tmp/agent-fleet-muse-identity-green-20261026.xml`.
- Fresh serial related verification: Python 3.10.21 and 3.13.15 each **676 passed, 0 failures/errors/skips**, 21 modules; `/tmp/agent-fleet-muse-identity-related-python310-20261026.xml` and `...-python313-20261026.xml`.
- Initial Python 3.10 collection failed because its former temporary environment no longer contained Flask/cryptography; the former Python 3.13 environment was also incomplete. New task-local virtual environments install only existing `requirements-test.txt`; each resolved wheel was checked below 10 MB. No global packages or repository dependencies changed. Resolve/install/size logs are under `/tmp/agent-fleet-muse-identity-*20261026*`.
- Preflight: 856 source files, 854 UTF-8/no-BOM text files, 601 Python AST files, 225 local links, 22 CJS and 8 modified JS module checks, Python 3.10/3.13 compileall and diff check passed. No static type checker configured; `/tmp/agent-fleet-muse-identity-preflight-20261026.json`.
- Full frozen verification: **5294 passed, 0 failures/errors, 5 platform skips, 195 passing subtests**, 666.065 seconds; Python 3.13.15/macOS with existing Chrome/Playwright. WF-06 **307 selectors / 707 cases** and WF-08 **165 / 333** all passed; **41/42 journeys** passed. Strict exit **1**, `ci_status=failed`: RELEASE-01 lacks three required Linux pidfd/proc cases; two other Supervisor proc cases also skipped. Seven external checks remain `not_run`.
- Evidence: `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-muse-identity-full-20261026-r1_9bsz4`. Configuration/command, original and final source digests, JUnit, journey report, exit and verification records retained. All 856 source files remained unchanged; matrix SHA-256 `523291b4fd6ac704f0ee8029612367617f1d63ef9a0839ca2240313f446c19f2`. JUnit has 5299 actual testcase elements; its 5494 suite total includes 195 subtests and is not an extra independent test count.
- All 44 PNG files passed signature/CRC/dimension/IEND checks; representative existing unknown and waiting_external Workflow screenshots were visually reviewed. No UI implementation changed in this slice.
- Full platform work remains open: Native/summary context, binary/code-package inputs, capability requirements, other configuration and real Linux/Windows/two-node/model/CLI/isolation/MCP/Muse acceptance. Structured external output still requires a decision between existing Hub ArtifactRefs and remote-file import; neither was inferred or implemented. Gates stay default-off; Muse cancel/export unsupported; no release approval is claimed.
- Current progress and test-entry summaries distinguish implemented explicit Memory from open Native/summary support. Post-document checks and frozen-source comparison are stored as `post-doc-preflight.json` and `post-doc-source-comparison.json` in the evidence directory; runtime code, tests and the matrix remain unchanged after the full run.
