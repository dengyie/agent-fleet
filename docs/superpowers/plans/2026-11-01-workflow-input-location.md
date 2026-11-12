# Workflow Artifact Input Location Repair

> **For agentic workers:** Execute inline in the existing isolated feature worktree using executing-plans and evidence-driven-bugfix. Preserve all existing work and evidence. No development commit, push, merge, deployment or installation is part of this repair. The separately authorized strict Linux verification reuses the existing private runtime; fixture cleanup requires the user's exact per-path confirmation and verified archival.

**Goal:** Keep the documented Hub-local review/test and complete Native text consumers usable when Node artifact transfer is enabled, while preserving mandatory receipts for remote targets.

**Architecture:** Derive immutable, exact Hub-input registrations only from trusted local Run targets during assembly. The coordinator uses the same repository-owned registration index as the admission transaction. Missing registrations retain the existing Node receipt requirement; a generation already pinned for Node reception cannot be converted to Hub input. Reuse the current Run/Workflow, authorization, artifact reader, lease and publication lifecycle.

**Tech Stack:** Existing Python 3.10+, Flask, SQLite, pytest and standard library. No dependency, table, column, wire marker or worker change.

## Authority and observed failure

- [Execution contract §6.2](../specs/2026-10-20-workflow-execution-contract.md#62-f3-固定测试的执行接线约束) places fixed tests in the existing Hub worker with private source extraction.
- [Artifact contract §8.3](../specs/2026-10-20-artifact-capability-integration-contract.md#83-f3-只读命令评审适配) places registered command review in the same worker.
- [Artifact contract §4.1](../specs/2026-10-20-artifact-capability-integration-contract.md#41-已实现的接收子集与恢复边界) requires actual Node reception for Node-local inputs; a Hub ArtifactRef alone is not a Node receipt.
- `tests/test_workflow_e2e.py::test_transfer_gate_preserves_hub_review_test_and_acceptance` produces **2 failed** with `input_alias_unregistered`, after actual fixture CLI/source publication and before model review. Evidence: `/tmp/agent-fleet-hub-input-location-red-20261101.xml`.

## Implementation and verification

- [x] Reproduce with the existing cookie/HTTP/CLI/source/review/test/acceptance chain, for both positive and negative review outcomes, with the original transfer gate enabled.
- [x] Add a frozen scoped Hub-input registration containing target, adapter/policy revisions and execution kind. Exact indexed matching is O(1); registrations come only from local non-legacy Run targets. Reject duplicates and invalid types.
- [x] Assemble registrations alongside existing executors; coordinator preparation and repository admission consult that same index. Preserve Node-pinned generations and all existing remote/default receipt guards.
- [x] Re-run the original failure and add direct admission, scope/revision/kind, collision and Native text composition regressions. Keep real Node HTTP receive and capability input-pinning regressions unchanged.
- [x] Compile on both installed Python runtimes and run the related Workflow/transfer/capability/backup/gate regression sets. Register new selectors without changing previous required selectors.
- [x] Record fresh results and limits in the specification, implementation progress and testing entry point. The previous 902-file Linux gate remains historical and cannot prove this new repair.
- [x] Run a new frozen strict Linux full gate and independently verify its copied evidence. Preserve all previous results and external-check boundaries.

## Local verification — 2026-11-01

- Original failing composition: 2 failed with `input_alias_unregistered`; the same HTTP/source/review/test/acceptance cases now pass. Complete focused Native/input-location/e2e modules: **140 passed**, 12.91s; the new boundary module contributes 39 cases.
- A further failing test proved collision rejection had already changed the repository index. Publishing the registration only after the conflict check fixes that failure; rejected assembly retains the original input policy.
- Related 54-module regression set, including real Node receive, capability pinning, default gates, plain/encrypted backup and release guards: Python 3.10 **1949 passed, 7 skipped, 34 passing subtests**, 224.80s; Python 3.13 serial **1949 passed, 7 skipped, 34 passing subtests**, 207.97s. The seven skipped cases require Playwright and are not accepted as full-gate evidence.
- The first Python 3.13 run overlapped the Python 3.10 packaging test in the same checkout and had one staging-presence failure. The packaging module alone then passed **38 cases / 4 subtests**, and the complete serial set above passed with the original assertions. Both failed and passing reports are retained.
- Full `compileall hub tools tests` exited 0 on both runtimes. Added 9 boundary selectors and the transfer composition selector to the applicable journeys, appending 30 entries without changing any previous required selector. Specification: execution §5.5 and artifact §4.1.

Reports are preserved in `/Users/mango/.cache/agent-fleet/verification/workflow-input-location-20261101-nzcdxbz9/`. These local results are distinct from both the previous 902-file strict Linux freeze and the new terminal result below; complete WF-04/05, live models/commercial CLI and seven external checks remain open.

This repair establishes input location for existing consumers. Cross-node review/test execution, new remote commands, actual two-host complete Workflow delivery, production isolation and seven external checks remain separate requirements.

## Frozen full-gate checkpoint — 2026-11-01

The independent evidence revision is 0de03b72e0b2ea34f595f334ac3f6a95ac44864c, with 904 source files. Both frozen checkouts match the provenance hashes and are clean; the development worktree matched all 904 files before result-only documentation updates. The original strict Linux gate started at 2026-11-01T12:24:16Z on gz-cvm using the existing Python 3.13.16 / Node 22.21.1 / Playwright 1.63.0 / Chromium 153.0.8010.12 runtime, unchanged assertions/selectors/timeouts, no DAC override/read-search capability and NoNewPrivs. This checkpoint is running evidence, not a terminal pass.

The user explicitly confirmed deletion of the single prior fixtures path /tmp/afla-7sx6gzta/evidence/controlled-full-rerun-1/fixtures. Before the separate deletion command, the 62,311,415-byte local archive and all 42,102 source entries were reverified; its SHA-256 is 310142c4f2f44536db7183774a0f627794d946a2ea1b41464849311657ef5871. No process references or submounts were found. After deletion the path is absent, all 54 outside evidence entries retain their inode/mode/size/hash, and free space is 3,967,401,984 bytes. Source, prior reports/logs/PNGs, runtime and the complete local archive remain preserved. Exact audits are confirmed-delete-preflight.json, confirmed-delete-result.json and confirmed-delete-postcheck.json in the evidence root.

## Verified strict Linux terminal result — 2026-11-01

The original full command completed with exit 0 at 2026-11-01T12:53:35Z: **5891 passed, 197 passing subtests, zero failures/errors/skips**, 1757.26 seconds in pytest and 1759.25 seconds in the bounded driver. JUnit contains 5891 explicit testcase records; its summary includes 6088 tests with subtests, which are not added again to the passed count. All 42 required journeys pass, with 1781 selector entries and 44 complete PNGs. Matrix SHA-256 is 489b6c6a6f98d84741e6284723680c4c7e56c5f9ec6aea731b6341621d742645.

Original tools.testing.release_evidence exited 0 on gz-cvm and on the copied local evidence against the same independent frozen checkout. All 904 source hashes remain identical and both frozen checkouts are clean. The 50 copied evidence files total 7,947,220 bytes; every size/SHA-256 matches. The 5,518,828-byte evidence archive SHA-256 is 6f97b7e8ff44e978d80bfad34f28c564f2f496aed54509512eb2da8e3ce04ead. The original driver retained PID 4008332/start ticks 205658895 and free bytes before/after of 3,963,764,736 / 1,776,926,720. No assertion, timeout, selector, browser, sandbox or runtime was weakened.

The evidence root contains provenance.json, source.bundle/source.zip, launch-command.json, the original command/JUnit/log/journeys/PNGs and verifier output under full-copy/controlled-full, plus full-copy/copy-verification.json and both source audits. Scope review and document/matrix audits are separate records; result-only development documents are not retroactively included in this tested revision. Prior 902-file results, original red composition and local packaging failure remain independent historical evidence. This closes the input-location repair and its full gate; all seven external checks remain not_run, gates stay default-off, and the complete platform goal remains open.
