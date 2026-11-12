# Native Workflow Code Package Inputs Implementation Plan

> **For agentic workers:** Use inline executing-plans to implement this plan task-by-task. Preserve the protected primary worktree and all existing development. No commit, push, merge, deploy, package installation, download, or live external call.

**Goal:** Let explicitly registered local Native Workflow Runs inspect complete, verified `code_revision` source packages through bounded read-only tools while preserving the original Run lifecycle.

**Architecture:** Reuse the existing `CodeRevisionReference`, owner-scoped ArtifactStore descriptor, and source-bundle verifier/extractor. Persist only the exact code revision references in the original private Run configuration; reconstruct a private read-only source view from the immutable package for each worker attempt, including safe checkpoint recovery. Add two separately named `workflow.input.list` and `workflow.input.read` tools so source files never become part of the user workspace or model prompt.

**Tech Stack:** Python 3.10+, Flask/SQLite, existing ArtifactStore, `fleet_core.workflow.source`, existing ToolBroker and pytest. No new dependencies.

## Global Constraints

- Only trusted `RegisteredRunTarget.inline_code_inputs=True` local `native_run` targets may consume `code_revision` references; default is `False`.
- Preserve the original 10 MiB source-bundle byte limit, 4096-file per-bundle limit, verified Git tree/blob checks, existing source exclusions, and the original Run token/transaction/lease/cancel/unknown semantics.
- Total distinct source-bundle bytes and extracted files for one Run are each limited to 10 MiB and 4096; at most 16 code references are accepted. Repeated references to the identical immutable revision share one extracted view.
- Persist reference metadata only, never archive/file bytes, in the Run snapshot. Raise Workflow reader to 12 in the same successful Run creation transaction.
- The model reads source through `workflow.input.list/read`; it receives no absolute path, workspace write access, source-execution capability, or tool grant from package contents.
- Restore and history validation must recognize the new private snapshot shape even when workflow gates are off; old schema 1/2/3 Native snapshots and no-input Run bytes stay unchanged.
- Keep UTF-8 without BOM. Do not install dependencies, download software, invoke external services, touch production, or alter other user's edits.

## File Map

- `fleet_core/workflow/code_inputs.py`: immutable code-reference contract, limits, snapshot encoding/parsing helpers.
- `hub/application/workflow_code_inputs.py`: owner/workspace manifest checks, verified extraction into a caller-owned private temporary root, bounded list/read operations.
- `hub/domain/workflow_native_memory.py`: schema 4 private context containing code references and a reference-only system message.
- `hub/infrastructure/workflow_native_memory_repository.py`: current authorization, reader-12 check, and historical artifact-reference closure.
- `hub/infrastructure/workflow_run_executor.py`: trusted target flag, native target validation, saved reference restoration, and atomic snapshot/reader update.
- `hub/application/run_worker_service.py`: per-attempt source-view lifetime and Native tool wiring before provider I/O.
- `hub/platform/tool_broker.py`: gated `workflow.input.list/read` definitions and dispatch to the private view.
- `hub/application/conversation_service.py`: reject caller overrides of the trusted code-input marker.
- `tests/test_workflow_native_code_inputs.py`: lifecycle, provider-wire, safe-checkpoint recovery, artifact authorization and backup/reader evidence. Recovery cases remain here to reuse the real source fixture and original Native Workflow setup.
- `tests/test_workflow_native_text_inputs.py`, `tests/test_workflow_native_text_recovery.py`: compatibility regressions.
- `docs/superpowers/specs/2026-10-20-workflow-execution-contract.md`, `docs/superpowers/plans/2026-10-20-workflow-platform-roadmap.md`, `docs/testing/workflow-platform-acceptance.md`, `docs/platform/development-completion.md`: contract, TODO, journey and completion evidence.

## Task 1: Contract and red Native-chain tests

**Interfaces:** `CodeRevisionInput(ref_key, reference)` is immutable. `code_revision_inputs(resolved)` returns contract-ordered exact code references. `WorkflowCodeInputs(artifacts, root_factory)` exposes `open_view(resolved, references, should_cancel)`, whose context yields a private `WorkflowCodeView` with `list(ref_key, path, offset, limit)` and `read(ref_key, path, offset, max_bytes)`.

- [x] Add actual Native provider tool declaration/call tests against a real exported source artifact, complete source verification, and private/public-boundary checks; also cover safe `ready` checkpoint recovery.
- [x] Extend negative coverage for special archive entries, missing view, and forged code marker. Tests cover tampered archive tails, symlink/hardlink/FIFO/device/path rejection, all six manifest fields, invalid UTF-8, aggregate file/byte and reference limits, consumer-exception cleanup, disabled/unsupported targets, wrong owner, cancellation and direct wire access without a private view.
- [x] Run focused code-input tests and the Native text/memory/checkpoint/dispatch/worker/provider/backup/source plus release-evidence regression set. Latest result: 37 focused passed; combined sixteen-module set 708 passed. Earlier four text-recovery fixture failures were due to an outdated test override signature and now pass after updating the fixture to accept `references=`.

## Task 2: Verified private source view

- [x] Implement exact resolved-reference parsing and aggregate package/file budgets without archive I/O.
- [x] Implement `WorkflowCodeInputs.open_view` using current manifest metadata and `ArtifactStore.open_verified`; stream each source bundle through `extract_source_stream` into a private temporary root and verify the exact `CodeRevisionReference`.
- [x] Implement deterministic namespace mapping by `ref_key`, validated relative paths, bounded directory pages, bounded UTF-8 byte chunks, and stable error codes. Never return a host path.
- [x] Extend view lifecycle tests for cleanup on body exceptions and repeated identical references sharing one extraction; normal cleanup, cancellation rejection and descriptor close are covered.

## Task 3: Persist and recover trusted code references

- [x] Extend `NativeWorkflowMemory` with ordered code references and canonical private schema 4; preserve exact schemas 1/2/3 and no-input behavior.
- [x] Add `RegisteredRunTarget.inline_code_inputs=False`; validate it is an exact bool and permit it only for the local provider-backed Native target.
- [x] Add private `workflow_code_mode={revision:1}` to eligible original Run snapshots, reject caller/target marker injection, and include code references in the existing resolved/config identity.
- [x] Save code reference metadata and raise reader 12 in the original successful Run creation transaction; an existing Run lookup restores saved references before artifact I/O.
- [x] Extend `WorkflowNativeMemoryRepository.prepare` to require code reader/mode/reader agreement, authorize current exact artifact manifests outside SQLite, then repeat the original fenced read with the injected clock.
- [x] Extend historical closure so schema 4 references exactly match resolved code revisions and `workflow_artifact_refs` input rows; backup restore uses the existing frozen history validator. Current test restores the real code-input Run, checks reader12 and the exact artifact reference, and verifies gate-off/startup/backup reject a downgraded reader. Plain/encrypted restores, seven final-page corruption cases, and the separate 129-Run × 16-reference closure/backup probe are now verified; see the current results below.
- [x] Verify committed Run retry performs no artifact authorization I/O and a fresh worker attempt reconstructs the same private view after a saved `ready` checkpoint without replaying completed read calls.

## Task 4: Read-only model tools and worker lifecycle

- [x] Add fixed tool schemas for `workflow.input.list` and `workflow.input.read`, with exact keys, validated ref key/path/offset/page limits, and bounded results.
- [x] Register these tools only for a code-input-enabled local Native Run; direct wire names without that capability return `tool_not_allowed`.
- [x] Keep the original workspace backend for `workspace.*`; route code tools only to `WorkflowCodeView`.
- [x] Hold the private source view only for the current worker execution and close it on success, failure, unknown, cancellation, and exceptions.
- [x] Rebuild the private source view for recoverable checkpoints; preserve pending-call unknown semantics and do not replay provider/tool side effects. `ready`, `response` and `receipt` recovery are exercised end-to-end. Pending provider/tool checkpoints remain unknown and never replay; plain/encrypted backup and final-page corruption are covered.

## Task 5: Verification, journey and documentation

- [x] Run code-input tests plus Native text, Native memory, Native checkpoint, provider/ToolBroker, workflow dispatch, backup, worker, external text/Memory, summary, input-location, boundary and release-evidence suites: 708 passed across the current sixteen-module set.
- [x] Register the actual provider source-input selector in WF-06; verify collection/execution as part of the next full journey gate.
- [x] Update the execution contract with target eligibility, snapshot schema 4, reader 12, tool wire shape, budgets, restore/unknown behavior, and unsupported binary/remote/review/test contexts.
- [x] Update the roadmap, Workflow acceptance matrix, and development completion ledger without converting partial platform/live evidence into passes.
- [ ] Run the configured full gate and record its exact browser/Linux skips. Ruff selected checks, Python compile, JSON parsing, UTF-8/no-BOM and `git diff --check` pass; focused release-evidence and 16-module integration regressions pass (708 tests). The full macOS gate has now run with the existing Playwright 1.63.0/Chrome: the first frozen candidate failed (details below), and the corrected candidate must pass a fresh run. The Linux host still lacks Chromium; its complete browser gate remains open.

## Verification Commands

```bash
PYTHONPATH=. python -m pytest tests/test_workflow_native_code_inputs.py -q --tb=short
PYTHONPATH=. python -m pytest tests/test_workflow_native_text_inputs.py tests/test_workflow_native_text_recovery.py tests/test_workflow_native_memory.py tests/test_native_run_checkpoints.py tests/test_workflow_dispatch.py tests/test_platform_run_worker.py tests/test_platform_provider.py tests/test_platform_backup.py -q --tb=short
python -m compileall -q fleet_core/workflow hub/domain hub/application hub/infrastructure hub/platform
git diff --check
```

Completion is limited to this F5 code-package input slice. Arbitrary non-code binary media, remote/capability-bound context, CLI/review/test context, unrelated configuration UI, real external service acceptance, full WF-05, and the final Linux/browser release gate remain separate open work unless independently completed and evidenced.


## Full-gate findings and extended verification — 2026-11-12

The first independent snapshot `af6332630718ef75bba055501f1235caccf20b4d` contains 1193 source files. The original macOS Python 3.14/Playwright 1.63.0/Chrome gate completed in 984.26 seconds with **7127 passed, 136 failed, 11 Linux-only skips and 204 passing subtests**. Source hashes remained unchanged and the checkout stayed clean; the original verifier rejected `journey_incomplete`. This failed run remains evidence, not a release pass.

- The Native code opt-in guard was incorrectly applied to existing review/fixed-test/remote-test source consumers. This caused 133 failures across the original execution and complete HTTP delivery chains. The guard now applies to the Native input target; existing specialized consumers keep their own source-contract validation. New three-consumer regressions were red before the fix, while the disabled Native target still rejects before artifact I/O. New tests also check original lookup and absence of Native input markers in specialized snapshots.
- Two identity-management tests assumed that no other management feature existed. They now expect the independently enabled summary-management feature while still checking exact feature maps and the original Fleet gates.
- A usage-browser navigation timed out at its original 30-second deadline. A fresh original-browser case passed in 3.91 seconds; no assertion or timeout was changed. The failed complete report is retained, and the corrected complete run must establish the final result.
- On the first snapshot, the same sixteen-module code/text/memory/checkpoint/backup/source set passed **708 tests on native Linux**, with zero skips and identical source hashes. Windows completed **706 passed/2 failed**: both failures incorrectly required POSIX execute bits on NTFS. The fixture now explicitly creates a Git `100755` entry and checks archive mode, metadata mode and exact extracted bytes on every platform; POSIX also checks filesystem execute bits. The corrected source module passed **39 tests on macOS and 39 on Windows**. The test-only rerun is recorded separately and does not rewrite the original Windows report.
- An independent original-dispatch probe creates **129 code Runs with 16 references each**. It verifies a full 128-Run code-history page, the final page, all **2064 references**, ten missing/extra/hash/size/workspace corruption cases at the two page boundaries, and plain/encrypted frozen backup restoration. Both restores preserve every original private Run config, reader 12, all references, SQLite integrity and foreign-key consistency. The initial probe counting/comparison mistakes and corrected audit reports remain separately recorded.
- The original packagers produced 1115 backend and 69 frontend files from the first snapshot; every source file hash matches, including both summary UI modules. These unpublished candidate artifacts do not certify the later correction.

Evidence root: `/Users/mango/.cache/agent-fleet/verification/native-code-release-20261112-k4k2zd4a/`. A new coherent freeze is required for the executor correction and additional tests. Full Linux/browser, production configuration/acceptance and the remaining project TODOs stay open.


## Corrected frozen results — 2026-11-12

Independent snapshot `5fe6bbb2b55ac8a728dcd2e9f7173840c8d31d6e` / 1193 files passed the complete macOS Python 3.14/Playwright 1.63/Chrome execution: **7267 passed, zero failures/errors, 11 Linux-only skipped, 204 passing subtests**, 946.79 seconds. All 44 journeys/2175 selectors resolve; 41 journeys pass and all 46 PNGs validate. The original release verifier still rejects `journey_incomplete` for NODE-01/RELEASE-01/WF-01; the eleven Linux instances remain required in the full Linux gate.

The same snapshot passed **963 tests on native Linux with zero skips** (25 related modules plus all eleven Linux-only required instances), 426.60 seconds. Original Windows sixteen-module execution is **709 passed/3 failed**, all new specialized POSIX Hub fixtures failing before dispatch because of temporary-file sharing. After explicitly marking those three Hub fixture cases POSIX-only, the Native code module passed **38 cases/3 platform skips** against the same product code. The full failed Windows report is retained. This does not establish Windows Hub support or rewrite its original report. Current macOS source+Native modules passed **80 cases**, including all three specialized source consumers. Product code remains identical to the snapshot; later differences are test platform marks and result documentation.

The 129 actual dispatches × 16 references, ten page-boundary corruptions, and plain/encrypted restores were repeated against this corrected snapshot; all 129 private configurations/2064 refs, frozen mode/reader12 and SQLite integrity/FK checks match. Original packagers produced 1115 backend and 69 frontend files, each matching frozen source, backend 6750407 bytes/SHA256 `fb51ed07afb5dd42fa4ef6b407f0f4477091c34649a2c53fbc1173109a14dbe7`. Candidate remains unpublished. Installed receiver audit found the same one-line missing jsonschema preflight; reviewed candidate and exact hashes are prepared in the evidence directory, not installed.

Evidence: `/Users/mango/.cache/agent-fleet/verification/source-consumer-fix-20261112-tyf7lkdc/verified-summary.json`. Remaining: complete Linux/browser gate, receiver installation, production configuration and actual task/recovery acceptance, and the other project TODOs. Linux Chromium195836009 bytes and libllvm15 23115156 bytes still require the user's explicit large-download approval; no download occurred.
