# Remote Fixed Test Capability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add an independently registered remote fixed-test capability bound to the original receive receipt, source revision, workflow generation and dispatch binding, with durable no-replay behavior and Hub-owned evidence publication.

**Architecture:** Extend the existing capability catalog and Run target with a narrow remote validation target. The Node reads the already received source through its journal-protected descriptor and invokes the existing fixed test runner. A bounded result envelope returns complete report/JUnit bytes to Hub, which validates and publishes them through the existing coding-delivery transaction.

**Tech Stack:** Existing Python, SQLite, Flask, cryptography and pytest; no new dependency.

## Global Constraints

- Existing local Hub fixed-test path remains behaviorally unchanged.
- `tool.workflow.test` is not a model tool and cannot invoke arbitrary shell, argv, launcher or path.
- `coding-delivery-tests@1` is the only recipe; command retry class is `manual_only`.
- Source receive must be ready before remote Run admission; source/subject/generation/token/binding are exact matches.
- Result wire stays below the existing 64 KiB command-result limit; report 8 KiB, JUnit 32 KiB, environment 4 KiB decoded caps.
- Unknown never becomes succeeded and never releases held/isolated capacity.
- UTF-8 without BOM, no TODO/pass/placeholders, original exception causes preserved.

---

### Task 1: Add the fixed remote-test domain contract and bounded result codec

**Files:**
- Create: `hub/domain/remote_workflow_test.py`
- Modify: `hub/domain/capability_execution.py`
- Modify: `hub/infrastructure/capability_dispatch.py`
- Modify: `tools/platform/remote_tool_broker.py`
- Test: `tests/test_remote_workflow_test_contract.py`

- [x] Define `REMOTE_TEST_ACTION`, exact request fields, source revision payload and `RemoteTestResult` encode/decode with strict byte limits and base64 validation. Include bounded Node-local `recipe_argv` evidence; accept different absolute Python paths while rejecting changed fixed arguments or mismatched digest.
- [x] Register the action as a capability action while keeping it outside `NODE_TOOLS`; map its retry class to `manual_only`.
- [x] Test malformed fields, oversized report/JUnit, revision mismatch, and stable round-trip bytes.

### Task 2: Expose verified received source through a descriptor-pinned consumer

**Files:**
- Modify: `tools/platform/artifact_transfer.py`
- Create: `tools/platform/workflow_remote_test.py`
- Modify: `tools/platform/workflow_source.py`
- Test: `tests/test_remote_workflow_test_node.py`

- [x] Add a journal-backed verified stream lookup keyed by the original receive grant; validate receipt, root identity, file identity, artifact id/hash/size and alias before opening.
- [x] Add bounded streaming source extraction into a private directory and run the existing fixed recipe with the configured sandbox launcher.
- [x] Return only the strict result codec; never expose a source path or arbitrary command field.
- [x] Test signed request binding, source tampering, missing launcher, fixed argv, report limits and no path escape.

### Task 3: Wire Node registration and lifecycle

**Files:**
- Modify: `tools/platform/node_executor.py`
- Modify: `tools/platform/node_capabilities.py`
- Modify: `tools/platform/node_runtime.py`
- Modify: `tools/platform/node_client.py`
- Test: `tests/test_remote_workflow_test_node.py`

- [x] Add explicit `workflow_test_enabled=False` runtime gate and configured `workflow_test_executor`; advertise `tool.workflow.test` only when the Linux executor is available. Launcher availability is not proof of confinement.
- [x] Route the action before generic tools, preserving capability preflight and Node journal no-replay semantics.
- [x] Verify restart returns the original terminal result without rerunning the runner.

### Task 4: Permit only registered remote validation targets

**Files:**
- Modify: `hub/infrastructure/workflow_run_executor.py`
- Modify: `hub/infrastructure/capability_workflow.py`
- Modify: `hub/infrastructure/workflow_delivery_repository.py`
- Modify: `hub/infrastructure/platform_db.py`
- Modify: `hub/infrastructure/workflow_schema.py`
- Test: `tests/test_remote_workflow_test_target.py`

- [x] Add `remote_test` target identity and validate its exact config/recipe/platform boundary.
- [x] Extend capability admission only for `validation` + `remote_test`; preserve native-run and local test exclusions.
- [x] Include `workflow_remote_test` in binding checks and late completion fencing.
- [x] Pin minimum reader 10 in the remote Run creation transaction; test older-reader rejection and plain/encrypted frozen restore without dispatch.
- [x] Test ordinary tools, local test targets, wrong recipe, wrong generation and unreceived source rejection.

### Task 5: Execute one remote fixed-test command and publish Hub evidence

**Files:**
- Create: `hub/application/remote_workflow_test_execution.py`
- Modify: `hub/application/run_worker_service.py`
- Modify: `hub/infrastructure/workflow_bootstrap.py`
- Test: `tests/test_remote_workflow_test_execution.py`

- [x] Build one fixed command from current target/source context and deterministic receive grant; enqueue through existing capability preparation and wait for the original receipt.
- [x] Decode/validate the complete result and call existing `record_test`; publication retry never reruns the command.
- [x] Map unknown/cancelled/invalid result to existing Run error handling and preserve reservation state.
- [x] Test successful remote report, failed assertion report, lost receipt/restart, and Hub publication conflict.

### Task 6: Focused verification and documentation record

**Files:**
- Modify: `docs/superpowers/specs/2026-10-20-artifact-capability-integration-contract.md`
- Modify: `docs/superpowers/specs/2026-10-20-workflow-execution-contract.md`
- Modify: `docs/platform/workflow-platform/implementation-progress.md`
- Modify: `docs/platform/workflow-platform/README.md`
- Modify: `docs/testing/README.md`, `test-chains.md`, `workflow-platform-acceptance.md`, `journeys.json`
- Modify: `tests/test_workflow_native_text_recovery.py`, `test_external_agent_memory_inputs.py`, `test_external_agent_text_inputs.py`, `test_external_agent_output_recovery.py` (reader 10 is supported; future-reader rejection moves to 11)
- Test: `tests/test_remote_workflow_test_contract.py`, `tests/test_remote_workflow_test_node.py`, `tests/test_remote_workflow_test_target.py`, `tests/test_remote_workflow_test_execution.py`

- [x] Link the new contract and state precisely what is implemented versus still open.
- [x] Run focused tests, compile changed Python modules, `git diff --check`, UTF-8/BOM scan, and existing local fixed-test/capability regressions.
- [x] Do not claim full WF-04/05, Windows production support, commercial CLI, or production sandbox evidence from this slice.

## Verified local result — 2026-11-02

The final serial related run used the existing project Python 3.14 environment, 35 modules:
**1272 passed, 0 failures/errors/skips, 181.04 seconds**. JUnit:
`/tmp/agent-fleet-remote-fixed-test-final-related-20261102.xml`. The four new modules
account for 115 of those cases (46 contract, 19 Node, 30 target, 20 execution); do not
add them again. All 34 remote test functions are registered in NODE-01. Lifecycle and
recovery subsets also belong to WF-03/WF-08; these journeys now contain 251/138/235
selectors respectively. Existing selectors and seven external checks remain.

The cross-host Python path regression failed before the bounded `recipe_argv` evidence
fix and passes with it. Reader 10 support exposed obsolete future-reader fixtures;
future-reader rejection now uses 11, while active Workflows still block domain gate-off.
Plain/encrypted remote-run backups preserve reader 10, frozen state and held reservation
without issuing a test command. Earlier diagnostic reports are retained separately.

Python 3.14 and 3.10 full `compileall -q hub tools tests` exited 0. The preflight checked
903 UTF-8/no-BOM text files, 642 Python ASTs, 262 local links/anchors across nine core
documents, and all 1546 unique required selector definitions; no errors. The final
documentation pass verified 263 links/anchors; its file manifest, matrix/JUnit hashes
and results are saved in `/tmp/agent-fleet-remote-fixed-test-preflight-final-20261102.json`. `git diff
--check` passed. No static type checker is configured; compilation is not a type proof.

### Remaining platform acceptance

### Task 7: Opt-in native Linux acceptance fixture

**Files:**
- Create: `tests/fixtures/workflow/remote_test_launcher.py`
- Create: `tests/fixtures/workflow/remote_test_node_canary.py`
- Create: `tests/fixtures/workflow/remote_test_probe.py`

- [x] Add a test-only fixed launcher that resolves the private checkout from its own cwd and delegates to the existing sparse namespace/seccomp launcher. The signed request still cannot provide paths, argv or launcher options.
- [x] Add an independent NodeRuntime process with actual capability observation/report/preflight, authenticated HTTP source receive and original signed command journals. Use bounded stdin/stdout, process timeout, owned SSH forwarding when selected, and no persistent service or dependency changes.
- [x] Drive original admission, Run dispatch/worker and Hub evidence publication for passing, failing, lost-receipt recovery and unknown outcomes. Prove the receive barrier, exact source/binding, terminal replay without execution, and held/isolated reservation for unknown.
- [x] Exercise real read-only source, sparse host filesystem, no secrets, denied sockets, namespace/capability/seccomp assertions within the fixed recipe. Preserve unconfined negative-control evidence in the existing Linux sandbox acceptance test and separate this fixture evidence from production sandbox certification.
- [x] Freeze and hash the source used by the authorized Linux run, retain all Node/Hub reports and journals, and record the native result independently from the full gate.

- [x] Execute this capability on an actual Linux NodeRuntime with the intended fixed launcher, including terminal replay/recovery without re-execution. This proves the opt-in fixture path on the tested host only; local availability overrides or launcher presence alone are not confinement evidence.
- [x] Run the corrected new source tree through the original complete strict Linux gate and both original release verifiers; preserve independent failed and corrected freezes.
- [ ] Complete full WF-04/05 and production/external acceptance. Windows execution for this action is outside the implemented Linux-only boundary. All gates stay disabled by default.

The 918-file native freeze `d78be33e3a86f9fe13644076e10100730d78de33` passed all four real Linux NodeRuntime cases (29.77s). Its full gate exposed one additional stale reader fixture in external output recovery: reader 10 is now supported, so the future-reader case must use 11. The failing case was independently reproduced before changing that test parameter. The three related recovery/target modules then passed **97 cases, zero failures/errors/skips, 20.523s** (`/tmp/afrt-reader-final.xml`). This correction is outside the d78 freeze; the original failed result is retained separately from the corrected complete run below.

The original complete d78 run exited 1 with **6009 passed, 1 failed, 197 passing subtests, zero errors/skips** (1674.55s); only the reader-10 case failed. WF-06/WF-08 were incomplete, 40/42 journeys passed, and all seven external checks stayed not_run. All 918 source hashes and the clean checkout were unchanged.

## Corrected frozen strict Linux result — 2026-11-02

The corrected 918-file revision `2915fe6c5ca859f99650951b6e07981d1da44634` completed
the exact original strict command once on a fresh clean Linux checkout: **6010 passed,
197 passing subtests, zero failures/errors/skips**, driver exit 0 in 1696.14 seconds.
All **42/42 journeys, 1839 selectors and 44 PNGs** passed the original remote and local
`tools.testing.release_evidence` verifiers. Python 3.13.16, Node 22.21.1, Playwright
1.63.0, Chromium 153.0.8010.12, `LC_ALL=C`, the original sandbox and DAC/NoNewPrivs
controls were retained. All 918 source hashes were unchanged; checkout remained clean.
The previous paragraph describes the old failed freeze; this new complete run supplies
the corrected gate without combining counts.

All **50 evidence files / 7,986,144 bytes** were copied and verified by size and SHA-256.
The archive is 5,531,169 bytes, SHA-256
`e1b1766400d0da06d519e48c7f93b449c9f3cc07814ac24d857facb3c41fcc9b`; matrix SHA-256
is `1edb4ec4acd110e606cfb24f4237286963b9af58269992b448ed4d21ced57e44`. Evidence root:
`/Users/mango/.cache/agent-fleet/verification/remote-fixed-test-native-vn9iy3ix/`,
with `full-copy/copy-verification.json` and original reports/screenshots in
`full-copy/controlled-full/`; remote root `/tmp/afrt-vn9iy3ix/` on `gz-cvm`.

The approved old `/tmp/afrt-ud96payd/evidence/controlled-full/fixtures` was fully archived
and verified before single-path deletion; all 49 outside entries were unchanged. Source,
reports, logs, screenshots, journals and runtimes remain. This closes the corrected strict
gate only. Full WF-04/05, Windows execution for this action, real CLI/model and production
confinement remain open; all seven external checks are `not_run` and gates default off.
This result documentation was recorded after the tested freeze; runtime code, tests
and the matrix remain identical to that freeze.

Detailed implementation and evidence: [implementation progress](../../platform/workflow-platform/implementation-progress.md#2026-11-02-独立远程固定测试能力).
