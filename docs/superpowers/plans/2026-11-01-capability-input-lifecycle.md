# Capability Input Lifecycle Implementation Plan

> **For agentic workers:** Execute inline with `executing-plans` in the existing isolated worktree. Preserve all prior work and evidence.

**Goal:** Verify that signed Node input receipt gates automatic capability admission and the original Run worker, including contention and terminal recovery.

**Architecture:** Compose the existing lifecycle fixture with the original transfer/input repository and NodeRuntime. Keep source bytes in the separate registered input directory; the deterministic provider performs a fixed workspace write and does not claim to consume or execute the input. No production protocol or remote validator is introduced.

**Tech Stack:** Existing Python 3.10+, Flask, SQLite, cryptography and pytest, without additional dependencies.

## Global Constraints

- Authority: [artifact contract §4.1 and §6.4–6.5](../specs/2026-10-20-artifact-capability-integration-contract.md), [execution contract §5.5](../specs/2026-10-20-workflow-execution-contract.md), and [WF-04/05 acceptance](../../testing/workflow-platform-acceptance.md).
- Original coordinator, Run worker, signed HTTP Command delivery, receive journal and command journal must all participate. Do not manually admit or manufacture terminal rows.
- No intent, execution budget, capability binding, Run or held reservation before the original Node receive receipt. Waiting for capacity must retain the original input target and grant.
- Unknown execution retains original generation/token/binding and held reservation; retry and replacement execution remain forbidden. Successful execution releases capacity only through ordinary terminal observation.
- Missing required outputs remain unavailable. No validation, acceptance, source consumption, remote fixed test, physical-host or full WF-04/05 claims. All gates remain default-off.
- UTF-8 without BOM; no dependency installation, downloads, remote cleanup, commits, pushes or deployment in this slice.
- Indexed identity lookups remain O(1) amortized; receipt matching is O(n) for n≤16 inputs and fixed test observations are bounded. No new production facade, port, queue or schema.

## Task 1: Compose existing fixture without changing its default behavior

**Modify:** `tests/test_capability_workflow_lifecycle.py`.

- [ ] Add an opt-in complete ArtifactRef, separate private input root and exact registered input alias to the fixture. Preserve the original no-input definition/configuration/registration defaults.
- [ ] Enable transfer only for that opt-in fixture and configure the same NodeRuntime with receive plus its original write and read-only digest actions. Keep receive outside capability routing.

## Task 2: Verify original combined lifecycle

**Create:** `tests/test_workflow_capability_inputs.py`.

- [ ] Verify zero Run/intent/budget/capability binding/held reservation while waiting for receipt, exact downloaded bytes and hash, then original automatic admission and real worker/Node write.
- [ ] Cover lost receive response and restarted Node recovery, retaining the original grant/receipt and one download; no second logical execution.
- [ ] Create a second real Workflow while the first holds the only execution slot. Receive its input normally, verify no admission until ordinary successful release, and verify unknown retains the slot and blocks the second Workflow.
- [ ] Repeat coordinator ticks and Node restart; retain target/generation/token and forbid repeat backend writes.

## Task 3: Verify and record

- [ ] Run the new tests and original lifecycle/coordinator modules; investigate failures before changing production logic.
- [ ] Run related input/dispatch/Node/worker/reservation regressions on both existing Python environments, compile changed files, inspect JUnit and run `git diff --check`.
- [ ] Register only the new proven selectors in the existing NODE-01/FILES-01/WF-08 subset, preserving every previous selector. Update progress/testing indexes with exact evidence and remaining scope.
- [ ] Verify final source hashes, UTF-8/BOM, local links and result anchors. Report focused evidence separately from the preceding frozen 5891-pass full gate.

Reproduction: `PYTHONPATH=. python -m pytest tests/test_workflow_capability_inputs.py tests/test_capability_workflow_lifecycle.py tests/test_capability_coordinator.py -q --tb=short -p no:cacheprovider --junitxml=<new-private-evidence>/results.xml --basetemp=<new-private-evidence>/fixtures`. The evidence and fixture paths must be new.
