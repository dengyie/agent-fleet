# Native fixed-test acceptance integration

> Execute inline with evidence-driven-bugfix and verification-before-completion in the existing feature worktree. Preserve every freeze, failed run and host service. Return to the [development ledger](../../platform/development-completion.md).

**Goal:** Verify current automatic remote fixed-test Workflow execution on the actual SG Linux Node, including passed/failed/lost-receipt/unknown outcomes, original capability binding and report publication.

**Architecture:** Reuse the preserved source tree's three explicit opt-in fixtures: `remote_test_probe.py`, `remote_test_node_canary.py`, `remote_test_launcher.py`. Keep original Workflow coordinator/Run worker/Command/NodeRuntime/Artifact APIs and existing namespace/seccomp launcher. Adapt proven neutral-module import and execution callback mismatches to current interfaces; no product change without a new failure signal.

**Stack:** Python 3.10-compatible source, existing local controller Python, SG private Python3.11/bwrap runtime, SSH-scoped reverse forwarding. No software installation, production mutation, global configuration, commit, push or deployment.

## Tasks

- [x] Preserve target collection failure in `/tmp/remote-fixed-native-integration-red-20261111.log`: the documented explicit probe is missing.
- [x] Restore the three existing fixtures, reproduce current import/interface failures, then adapt only demonstrated incompatibilities.
- [x] Freeze the complete integrated source independently and copy the small source bundle into a new private SG checkout; preserve previous native artifact evidence unchanged.
- [x] Run all four original actual-Node outcomes, verify one fixed execution, original signed command/capability binding, exact source/report/JUnit, receipt replay without rerun and unknown reservation retention.
- [x] Run appropriate local remote-test/Node-control regressions, source audits, lint, Python3.10 AST, UTF-8/no-BOM and diff checks.
- [x] Record exact commands/revision/results in this plan, ledger and acceptance matrix. Keep complete Linux/browser release evidence, real commercial CLI/model and full project acceptance open.

## Runtime admission root fix

Readiness repro on 2026-11-11: the fixed recipe uses `sys.executable`, but both backends admit only the basenames `python` and `python3`. The actual Homebrew `python3.14` runtime is rejected before spawn with `command_not_allowed`; the same mismatch affects versioned runtimes and Windows `python.exe`. Evidence: `~/.cache/agent-fleet/verification/readiness-20261110T201052Z/runtime-admission.json`. The associated 142-case regressions currently retain 37 failures; each must be rechecked rather than presumed to share this cause.

- [x] Add a real current-interpreter execution regression for DirectoryBackend and SandboxBackend, including a policy with no generic interpreter alias. Keep the sandbox launcher explicit. Capture red evidence before product edits.
- [x] Reuse one admission predicate from `fleet_core/backends/directory.py` in `sandbox.py`: keep existing aliases and additionally accept the exact absolute current runtime path. Do not accept arbitrary version-like names. Keep existing argument checks and missing-launcher behavior.
- [x] Verify actual script output and zero side effects for an unregistered executable, then rerun the existing fixed-test, cancellation, Node-control and boundary regressions. Preserve any unrelated failures for separate diagnosis.
- [x] Correct the lost-receipt fixture to recover through the durable receipt outbox and assert the pending-to-acknowledged transition, while keeping execution identity, result and zero-rerun assertions.
- [x] Freeze and rerun the four actual Linux Node scenarios on the new revision; preserve previous reports and record the remaining release scope honestly.

## Verified results — 2026-11-11

- Preserved initial freeze `a362378a24a80f7a79ea9a891b7f4fe297339fea`: all four actual Node scenarios failed because the canary wrapper omitted the current `should_cancel` keyword. After correcting the wrapper, freeze `46894eece7a980b7a09083831e54ef820f364dd3` produced three passes and one lost-receipt failure. The latter was an obsolete fixture expectation: a manual POST bypassed local acknowledgement, so the outbox correctly sent one pending receipt. All original stderr, journal evidence and reports remain intact.
- Backend admission regression was red with **2 failed / 8 passed**. Both actual current-runtime calls were rejected before spawn. Shared exact-runtime admission fixes this without broad version-name matching; cancellation tests no longer patch their executable allowlists. Direct original `--version` probes now return Python3.14.8 with exit0.
- Local associated suites: **157 passed / 43.81s** and separate **36 passed / 4.59s**, no failures/skips. A case-identity audit confirms all142 original cases are covered, including all37 earlier failures now passing; these are overlapping scope results, not a complete project gate.
- Immutable freeze **`51f716144721758f27db021a25324912f03aa837`**, tree `98bba48e10959d8078145fa9bf1c13038cbe39f2`, 1146 source files. Bundle7363423 bytes, SHA256 `5b45e0a4f0d06f394e433e67f4f2f58af639f3cede5130c59cedfa9110e864f4`.
- Actual controller→SG Linux Node: **4 passed / 80.91s**, outcomes passed/failed/lost/unknown. Each executes the fixed recipe once; report/JUnit match published bytes. Passed/failed/lost each have six actual sandbox test cases, with the deliberate `test_delivery` failure only in the failed outcome. Lost recovery uses the original outbox: pending true→false, one receipt sent, later replay zero receipts/executions. Unknown retains its reservation and publishes no successful output.
- Same frozen revision: Linux ten-module subset **172 passed / 73.58s**, Windows runtime/control/timing/process subset **102 passed / 13.76s**, no failures/errors/skips. Windows was reconstructed in a new private checkout from the earlier preserved freeze plus a440320-byte source delta; both full Git tree and exact commit identity match, and all1146 source digests were verified. Previous checkouts were preserved.
- Three frozen source trees remain unchanged/clean. Current product source digests match the freeze; subsequent development edits only remove two unused test imports and record documentation results. Ruff, diff checks, UTF-8/no-BOM and809 Python files'3.10 AST checks pass; FILES-01 adds two selectors, matrix44 journeys/2023 selectors.
- Evidence: `/Users/mango/.cache/agent-fleet/verification/readiness-20261110T201052Z/verified-summary.json`, with separate command/JUnit/log records for each scope. No development commit, push, PR or production deployment. Complete Linux/browser gate, real commercial model/CLI full delivery and the overall project objective remain open.
