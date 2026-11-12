# Capability Workflow Lifecycle Verification Plan

> **For agentic workers:** Execute inline with `executing-plans` in the existing isolated feature worktree. Preserve the verified corrected-source snapshot and all prior evidence. No agents, development commits, merge, push, deployment, installation, global settings/services, cleanup or Obsidian edits. This local verification does not authorize another remote or external-service pilot.

**Goal:** Exercise automatic capability admission through the original Run worker, signed Node HTTP execution and coordinator observation, including an actual write followed by uncertain completion and an independent postcheck.

**Architecture:** Add one complete integration-test module around the original coordinator, RunStepExecutor, LocalRunWorkerService, CommandDeliveryService, NodeRuntime and SQLite journals. A bounded deterministic provider requests one fixed workspace write; a fixture waiter advances the original Node poll once. No substitute execution state, manual admission, direct terminal-row updates, protocol, schema or new production service is introduced.

**Tech Stack:** Existing Python 3.10+, Flask, SQLite/WAL, cryptography and pytest. Existing installed local Python 3.10/3.13 environments; no dependency changes.

## Global constraints

- Authority: [capability binding and postcheck contract](../specs/2026-10-20-artifact-capability-integration-contract.md#64-hub-固定能力-run-与-command-派发子集), [automatic routing contract](../specs/2026-10-20-artifact-capability-integration-contract.md#65-协调器自动选点与输入目标固定子集), [Workflow observation and recovery](../specs/2026-10-20-workflow-execution-contract.md#7-版本变更重试和恢复).
- Both write and separate digest helper are declared before admission, with exact original retry classes. Offers use original authenticated HTTP; Node preflight receives the exact bound definition and local allowlist.
- Success must release held capacity only from the bound terminal Run observation, retaining the release-evidence digest. Missing required output remains visible and is not reported delivered or accepted.
- Unknown comes from the original backend completing a fixed write and then raising before returning; original Node journal/receipt, worker and coordinator propagate it. Never call `Fixture.isolate_write` or set Run/Workflow/dispatch/reservation terminal state directly.
- Independent matched/mismatch/read_failed postchecks preserve unknown, original binding/token/generation, budget and held reservation. Retry remains forbidden and repeated/restarted reads cause no write or digest replay.
- Only test-owned temporary files may be written/renamed. Preserve original fixed bytes for negative digest controls. UTF-8 without BOM; exact fresh per-run basetemp; no deletion or broad cleanup.
- Indexed identity queries remain O(1) indexed lookups; fixed table observations are bounded and hashing is O(n) in at most one small file. Reuse existing services; no new facade or interface for internal calls.
- Existing source `85488105cd2d3b610fcbfa18fc2273bd02618db1` remains independently verified with 5845 cases. New tests have their own scope until new verification. Full WF-04/05, actual-host coordinator lifecycle, model/CLI/production and external checks remain open; all gates default false.

## Task 1: Complete integration module

**Create:** `tests/test_capability_workflow_lifecycle.py`.

**Interfaces:** Original `WorkflowCoordinator.tick(now)`, `LocalRunWorkerService.run_once(owner_id)`, `NodeRuntime.report_capabilities()/poll_once()/close_all()`, `CommandPostcheckService` via original HTTP, and original owner-scoped repository reads. Complete four cases: successful write and unknown write with matched/mismatch/read_failed digest.

- [x] Assemble an immutable one-step native definition retaining a required report, trusted route, predeclared write/helper definitions and target, real loopback HTTP and original Node runtime/journal. Advance only the original coordinator to create the Run.
- [x] Drive one fixed tool call through the original worker/provider boundary and original signed Node command. Observe command/run/Workflow state and capacity/budget from bounded read-only database projections.
- [x] Verify successful execution does not fabricate required output/validation/acceptance; unknown cases request the independent helper over original HTTP and retain every original execution fact before/after repeated/restarted queries.
- [x] Run the complete file with fresh JUnit/log/basetemp. If any failure appears, retain red evidence, trace the actual boundary and correct only its demonstrated cause; preserve the original failing assertions.

## Task 2: Focused verification and registration

**Modify:** Existing NODE-01/WF-08 exact selectors, this plan, implementation progress and testing indexes after actual results. Product files change only if Task 1 supplies failing evidence of an existing contract violation.

- [x] Independently run the new module and related coordinator/dispatch/Node/worker/postcheck modules on installed Python 3.10 and 3.13. Compile touched Python files, read all results and retain command/stdout/stderr/JUnit and source hashes.
- [x] Register only actual test functions in NODE-01/WF-08, preserve existing selectors and seven external checks. Check links, UTF-8/no BOM and `git diff --check`.
- [x] Record independent outcomes, verified scope and remaining actual-host/full Workflow/external work. Do not combine counts with the previous full gate or claim the entire platform complete.

Commands use a freshly created evidence directory `EVIDENCE` and a distinct nonexistent basetemp per invocation:

```bash
PYTHONPATH=. /Users/mango/.cache/agent-fleet/verification/linux-20261028-cm40i40b/sandbox-macos-py310/bin/python -m pytest tests/test_capability_workflow_lifecycle.py -q --tb=short -rs --junitxml="$EVIDENCE/results.xml" --basetemp="$EVIDENCE/fixtures"
PYTHONPATH=. /Users/mango/.cache/agent-fleet/verification/linux-20261028-cm40i40b/sandbox-macos-py313/bin/python -m pytest tests/test_capability_workflow_lifecycle.py -q --tb=short -rs --junitxml="$EVIDENCE/results.xml" --basetemp="$EVIDENCE/fixtures"
```

## Verified result (2026-10-31)

The new integration module contains two real test functions and four parameter instances. Both installed environments passed the fresh module independently: Python 3.10 **4 passed** and Python 3.13 **4 passed**. The related coordinator/dispatch, Workflow dispatch/control/coordinator, Node capability/runtime and postcheck regression set passed **400 cases** on each Python version. Touched Python files compiled successfully.

The first red run reached the intended boundary: the Node write committed its file, then the simulated lost receipt produced `unknown`; the Workflow step, Run and Command were unknown, and the reservation remained held. The dispatch row still had stage `bound`, so the already-approved read-only digest helper was rejected with `capability_changed`. The minimal production fix is in `hub/infrastructure/workflow_repository.py`: a normal unknown observation now atomically records dispatch stage `unknown` in the same transaction as the observation and step update. No retry, binding, generation, reservation or helper contract changed.

The green lifecycle cases now prove automatic coordinator admission, the original Run worker and signed Node HTTP path, one successful write, matched/mismatched/read-failed digest postchecks, unchanged binding/token/generation and held reservation, `unresolved_execution` on retry, and zero write/digest I/O after repeated or restarted terminal reads. Red evidence remains under `/tmp/af-lifecycle-red-cVRei9`, `/tmp/af-lifecycle-red-all-u3za4K` and `/tmp/af-lifecycle-red-pc-XZbYwV`; corrected evidence remains under `/tmp/af-lifecycle-green-final-Kshrow`, `/tmp/af-lifecycle-313-K3y3Z6`, `/tmp/af-lifecycle-related-310b-r4onXc` and `/tmp/af-lifecycle-related-313-9ktvFX`.

This closes the local automatic capability lifecycle slice only. Full WF-04/WF-05, actual-host coordinator lifecycle, live model/CLI, production isolation and external checks remain open; the seven external checks stay `not_run` and every gate remains default-off. The local lifecycle counts are independent and are not added to any earlier full-gate count.

## Task 3: Current-source original Linux full gate (2026-11-01)

The 5845-case original full gate predates this module, the dispatch-stage fix and its two NODE-01/WF-08 selectors. Reverification of that retained evidence succeeds, but does not verify this later source. Current source contains 900 versioned/visible files; development HEAD and all ongoing work remain untouched.

- [ ] Freeze all current visible source into a separate private evidence repository, retain exact hashes/provenance and compile the frozen source with both installed local Python runtimes.
- [ ] Verify sufficient free bytes/inodes before starting the unchanged original Linux command. Preserve the last rerun's entire fixture tree locally and verify every member before proposing any single-directory deletion; obtain its own confirmation under AGENTS.md. The two earlier confirmed directories are already absent and provide no new cleanup authorization.
- [ ] Run the original complete `pytest tests -q --require-journeys` on the new clean source, with fresh basetemp, original Python 3.13/Node 22/Chromium 153/Playwright 1.63/bubblewrap environment, removed DAC override capabilities and NoNewPrivs. Keep one bounded live process and inspect its actual handle before any retry.
- [ ] Run the original release verifier, verify all source hashes before/after and copy exact reports, JUnit, journey/PNG evidence with size/SHA-256 checks. Re-run that verifier against the copied evidence and frozen local checkout before recording completion.

Preparation evidence is `/Users/mango/.cache/agent-fleet/verification/lifecycle-full-20261101-fdeguqmp`. Fresh current-worktree lifecycle verification on Python 3.10 and 3.13 separately reports **4 passed**, under `/tmp/af-lifecycle-resume-py310-20261101` and `/tmp/af-lifecycle-resume-py313-20261101`. These are focused checks, not the new full gate. Full WF-04/05, actual-host lifecycle, live CLI/model, production isolation and all seven external checks remain open.
