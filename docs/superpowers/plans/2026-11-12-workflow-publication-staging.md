# Workflow publication staging implementation plan

Execute inline without delegation. Return to the [development ledger](../../platform/development-completion.md) and [Workflow acceptance](../../testing/workflow-platform-acceptance.md).

**Goal:** Make original source-upload and report publication readable by ArtifactStore on Windows, preserve cleanup and original receipt identities, and remove the temporary platform skips hiding this defect.

**Architecture:** The application owns staging lifetime. Use a private TemporaryDirectory with an ordinary file whose writer is closed before synchronous or owned asynchronous ArtifactStore I/O. Keep store validation, original Workflow fences, source attempt IDs, publication hashes and lookup behavior unchanged. This fixes portable publication helpers; it does not certify all Windows Hub capabilities or enable unsupported sandbox execution.

**Tech stack:** Existing Python 3.10+, tempfile, ArtifactStore, SQLite, Git and native Windows/macOS/Linux runtimes. No dependencies, downloads, installation, deployment or development commits.

## Task 1: Capture actual failure

**Files:** `tests/test_workflow_publication_staging.py`, `tests/test_workflow_native_code_inputs.py`.

- [x] Add original report/source-upload tests that reopen staged files through the real ArtifactStore, check bytes/hash, and check cleanup on success and injected store failure.
- [x] Run the tests on existing native Windows before implementation. Preserve the original report and source-upload failure traces; previous specialized-consumer failures remain available in the 5fe6bbb2 evidence.

## Task 2: Correct owned staging lifetime

**File:** `hub/application/workflow_coding_delivery.py` (`record_task_source`, `_publish`).

- [x] Replace open NamedTemporaryFile staging with private directory and closed ordinary-file staging at both publication boundaries. Keep the directory alive until owned I/O completes.
- [x] Remove the three new specialized-consumer POSIX skips and verify dispatch/lookup on native Windows instead of assuming the fixture is inherently unsupported.
- [x] Repeat success, failure, cleanup and original same-attempt retry checks; retain zero automatic model/test execution in these regressions.

## Task 3: Verify related execution paths and record limits

**Files:** `docs/testing/journeys.json`, `docs/platform/development-completion.md`, `docs/testing/workflow-platform-acceptance.md`.

- [x] Register publication selectors in existing WF-01/WF-03 journeys; preserve external acceptance requirements.
- [x] Verify source/task/report, Native source consumers and related original execution suites on macOS and the same frozen source on Windows/Linux. Compare source hashes and report copies; retain the Windows fixture failure and separately verify its correction.
- [x] Record results here and link from the existing ledger/acceptance. Full same-version Linux/browser, production and unrelated TODOs remain open; the 7267-pass earlier freeze does not certify this later product change.

## Verified results and boundaries — 2026-11-12

Evidence root: `/Users/mango/.cache/agent-fleet/verification/publication-staging-20261112-3i0_14ep/`.

- Original native Windows red: five publication tests failed with PermissionError for open staging files. Original XML/log copied from the previous frozen checkout, matching remote SHA-256 (`windows-red/sha256.json`).
- Product fix frozen as `49a83f2313a0fed3c1139813065abd7b4b33c4c1`, 1195 files. macOS eight related modules: 235 passed, zero skips, 92.65 s (`macos-related.xml`).
- Same-source Linux 28 selected modules plus 11 required platform cases: 1027 passed, zero failures/errors/skips, 436.41 s. Same-source Windows 18 modules: 736 passed, one failure, zero skips, 348.13 s. Both original source manifests remained unchanged and checkouts clean. Original commands, reports and copied-report SHA-256 are retained under `linux/` and `windows/`.
- Remaining Windows failure independently reproduced in the original Native module: 11 passed, one failure before stop (`diagnostic-native.xml`). RemoteExecutionFixture created ordinary inherited-ACL input and receive-journal roots; the native receiver consequently reported capability_unavailable. The fixture now reuses the existing two-node private-directory helper for both roots; product receiver validation remains unchanged.
- A separate checkout of the same product freeze, amended only in `tests/test_remote_workflow_test_execution.py`, passed all 41 Native code-input/source-consumer instances and all five publication instances on Windows: 46 passed, zero skips, 92.89 s (`windows-fixture-fix/`). macOS those two modules plus the complete remote-execution module: 66 passed, zero skips, 23.03 s (`macos-fixture-fix.xml`). These scoped reruns do not rewrite the original 737-case report. The amended test file SHA is recorded and checked against the development tree.
- Public read-only probes still serve main `301e259`; no development commit, push, merge, receiver installation or production deployment occurred. Full final Linux/browser release gate and real production model/Node/recovery acceptance remain open. Large Chromium/libllvm15 downloads still need the user's explicit approval under AGENTS.md.
