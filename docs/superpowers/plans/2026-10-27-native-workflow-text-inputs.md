# Native Workflow Complete Text Inputs Implementation Plan

> **For agentic workers:** Use executing-plans inline. Preserve the protected primary worktree and all existing development. Implement complete files and verify each task; no commit, push, merge, deploy, download or live external call.

**Goal:** Deliver complete, bounded, owner/workspace-authorized text ArtifactRef contents to explicitly enabled local Native Runs through the original Run lifecycle.

**Architecture:** Reuse immutable text input values and the scoped artifact I/O reader for both external and Native inputs. Extend the original private Native Memory snapshot/guard with a compatible text-bearing version, then commit it in the original Run transaction and recover it before new file I/O. No parallel queue, execution engine or forwarding façade.

**Tech Stack:** Existing Python3.10+, Flask/SQLite, stdlib and pytest/Chrome environments; no new dependencies.

## Global Constraints

- Authority: execution contract §5.4, §5.3 and integration contract §7.11. Default trusted target flag False; only local provider-backed native_run may enable complete text.
- Text MIME: text/plain, text/markdown, application/json. Artifact-only references; binary/code inputs, summary, CLI/review/test/remote/capability context remain unsupported.
- Each input set≤16 text and≤16 Memory selections; complete logical bytes share8192 budget, private snapshot/system message≤32768 and finalRun config≤65536. Never truncate.
- Native Memory schema1 and no-input/reference-only bytes unchanged. Text uses private schema2 plus workflow_text_mode={revision:1}; reader9 upgrades only in the original successful creation transaction.
- Original token/payload/generation/lease/cancel/unknown/held-or-isolated semantics unchanged. Current metadata outsideSQLite transaction, final bound authority/Memory under a fresh read snapshot and injected clock.
- UTF-8 without BOM. No network, package installation, extraction, execution or permission derived from input content.

## Task 1: Shared immutable text contract and red original-chain evidence

**Files:** Create `hub/domain/workflow_text_inputs.py` and `tests/test_workflow_native_text_inputs.py`; modify `hub/domain/external_request.py` and `hub/domain/workflow_native_memory.py`.

**Interfaces:** `WorkflowTextInput(ref_key, reference, content_type, text)` is frozen with private text repr; `artifact_input_references(resolved)` and `text_input_value(expected, actual)` enforce complete exact values. Existing `ExternalTextInput` remains a compatibility alias. `NativeWorkflowMemory` gains a tuple of text inputs; `build_native_memory(resolved, items, texts=())` and `parse_native_memory(value,resolved)` retain exact schema1 and validate schema2.

- [x] Add failing tests for actualprovider text delivery, explicit/default target behavior, complete canonical/private snapshots, mixedMemory/shared budget, unsupported types/targets and immutable original lookup.
- [x] Run `PYTHONPATH=. /tmp/agent-fleet-muse-identity-py313-20261026/bin/python -m pytest tests/test_workflow_native_text_inputs.py -q --tb=short --junitxml=/tmp/agent-fleet-native-text-red-20261027.xml`; retain genuine failing evidence.
- [x] Implement exact strongtypes, schema1/2 parsing, ordered hash/size/MIME matching and reference-only messages; O(n+B) with request-local ID indexes and noI/O/time/random in domain.

## Task 2: Owned artifact collection and original atomic Run binding

**Files:** Create `hub/application/workflow_text_inputs.py`; modify `hub/application/workflow_external_inputs.py`, `hub/infrastructure/workflow_run_executor.py` and `hub/infrastructure/workflow_schema.py`.

**Interfaces:** `WorkflowInputArtifacts.get/open_verified` is the only new cross-I/O port; `WorkflowTextInputs.collect(resolved)->tuple[WorkflowTextInput,...]` and `.authorize(resolved,inputs)` own complete reads/current metadata. `RegisteredRunTarget.inline_text_inputs:bool=False` is immutable/trusted. `RunStepExecutor` accepts optional text reader and restores saved text before collecting new data.

- [x] Implement full bounded owned descriptor reads, distinct ID deduplication and current scoped metadata, retaining original external request/error behavior and root causes.
- [x] Validate explicit target support before file I/O; lookuporiginal Run in a read transaction before collect, then recheck lookup and all originalcreation authority in the write transaction.
- [x] Build schema2 text/Memory snapshot with shared8192 budget, trustedmode and original configdigest; store message/Run/binding/budget link and reader9 atomically. Failed collection/transaction never publishes partial execution.
- [x] Verify exact repeats/old bytes, file/permission mutation races and two originalcreators; cancellation joins ownedI/O and does not fabricate stopped or retry facts.

## Task 3: Worker authorization, recovery and full historical closure

**Files:** Modify `hub/infrastructure/workflow_native_memory_repository.py`, `hub/application/run_worker_service.py`, `hub/application/conversation_service.py` and `hub/infrastructure/workflow_bootstrap.py`; create `tests/test_workflow_native_text_recovery.py` and use existing backup/native history entry.

**Interfaces:** `WorkflowNativeMemoryRepository(workflows,text_inputs=None,clock=None).prepare(claim,now)` returns originalschema1/2 input snapshot, requiring textreader/clock for schema2. Currenttext metadata is checked outsideSQL; the finaloriginal read verifies claim/config/control/deadline/Memory. Existing worker inserts its complete reference-only systemmessage before provider I/O. Bootstrap attaches the shared reader only to actual registeredRun executors and the originalguard.

- [x] Verify mandatory mode/body/reader/guard, no-context defaults and secure ordinaryturn/target marker rejection. Provider receives original complete values; invalid scope/state/binding/Memory/deadline yields stable errors with original causes and no provider call.
- [x] Restore committed content before current file/Memory/reader checks; preserve same Run/message/config under missing inputs, changed configuration and response-loss/restart windows.
- [x] Extend existing signed-rowid128 joined history validation to schema2; reject final-page corrupt content, configuration, reader and reference closure. Plain/encrypted restore stays frozen without requiring current mutable inputs.
- [x] Add actual upstreamexternal ArtifactRef output→frozen downstream→Nativeprovider and defaultbootstrap scenarios, plus true concurrency/crash/cancellation boundaries.

## Task 4: Fresh verification and evidence registration

**Files:** Update genuine future-reader assumptions, the existing exact text-read assertion to include its new EOF probe, and explicit release-module inventory as applicable; update `docs/testing/journeys.json`, development/testing entries and thisplan. Keep all authority, bounds, lifecycle and legacy-cause assertions.

- [x] Run the new focused suite and original external text/Memory/outputs/Native Memory/worker suites with configured Chrome under Python 3.10.21 and 3.13.15; preserve reds and do not weaken assertions.
- [x] Run compileall/AST/JS/UTF-8/no-BOM/local-link/diff checks; record static typing as not configured unless an existing checker is found.
- [x] Register exact actualselectors in WF-06 and recoveryselectors in WF-08; record supported versus unsupportedscope without upgrading real/platform status.
- [x] Freeze source and run full `--require-journeys` with JUnit/report/source digests. Keep Linux platform skips and seven live checks honestly incomplete/not_run; verify and record only Markdown changes after the gate.

## Evidence retained before the final gate

The original red has48 failures. A first four-module run had240 passed/8 failures; the corrected core has264 passed without failures or skips in `/tmp/agent-fleet-native-text-core-resumed-20261027.xml`. Fixture corrections retain the true initial reader, schema1 without Memory, valid registered project identity and a deadline after the initial worker clock; the manifest callback advances the injected clock so the final deadline assertion remains meaningful.

Fresh closure red `/tmp/agent-fleet-native-text-closure-red-resumed-20261027.xml` reproduces history and provider execution after original input-reference deletion. The repair batches at most128 identities/2048 expected refs in one indexed query, bounds results at expected+1 and compares exact scope/key/hash/size sets. Expanded red also proves that a text-only bound Run could disappear without history rejection. Closure/reader green has23 passed in `/tmp/agent-fleet-native-text-closure-and-reader-green-20261027.xml`; corrupt-storage fixtures use a closed disposable SQLite connection to inject invalid foreign keys deliberately.

Target-capture red `/tmp/agent-fleet-native-text-target-capture-red-20261027.xml` proves configuration drift before late target capture. Creation now captures one immutable target before request validation/construction, uses it through original lookup and all creation fields, and rejects current-target drift before commit. The unchanged original I/O, retry and token/generation semantics remain in place.

The first11-module regression has591 passed/one packaging failure in `/tmp/agent-fleet-native-text-first-regression-20261027.xml`: HEAD-only packaging correctly omits uncommitted new modules. The release test now snapshots the current source into a temporary Git object store, checks both new modules byte-for-byte, and still proves that uncommitted drift and untracked runtime data cannot enter the package. Its new1-case green is `/tmp/agent-fleet-native-text-package-green-20261027.xml`; no development-repository commit is required or performed.

The three genuine future-reader fixtures use10 rather than9, because9 is now supported. The exact external fragmented-read assertion includes its mandatory EOF probe; original descriptor ownership, limits, errors and direct exception causes remain asserted.

The first38-module serial sets have1523 passed/2 passing subtests/no skips on each Python version. They predate the last local-alias fix: `/tmp/agent-fleet-native-text-local-alias-red-20261027.xml` has4 failures/1 passing default case, proving that the original Node catalog check rejected the worker's supported empty/local/hub/host aliases. Creation now follows the same local-alias rule; other Node IDs still require enabled registration. An actual admitted code_revision-input Workflow also verifies explicit pre-descriptor rejection. Final collection has38 selectors/112 cases across the two Native text suites; the final frozen related and full-gate evidence must still be recorded below.

## Related verification before the configuration-bound correction

The final 38-module serial runs under Python 3.10.21 and 3.13.15 each passed 1529 tests and 2 subtests, with no failures, errors or skips. The JUnit files each contain 1529 testcase elements; the suite aggregate includes the 2 subtests and must not be added again. Both runs include all 112 Native text cases after the local-alias correction. Evidence: `/tmp/agent-fleet-native-text-related-final-python310-20261027.xml`, `/tmp/agent-fleet-native-text-related-final-python313-20261027.xml` and `/tmp/agent-fleet-native-text-related-final-verified-20261027.json`.

All 871 source-file digests stayed identical before and after those runs. The frozen matrix SHA-256 is `48b455d49d9c3e098fdb27908c59cc1627b85e47a48e2732a98c66d898b2aed6`; it registers 412 WF-06 selectors and 201 WF-08 selectors. Native text contributes 38 selectors / 112 cases, including the existing upstream external output-to-Native provider path and original recovery checks. The subsequent full report remains the authority for journey execution status.

Preflight `/tmp/agent-fleet-native-text-preflight-frozen-20261027.json` passed UTF-8/no-BOM checks for 869 text files, AST parsing for 613 Python files, 22 CJS and 8 modified frontend module syntax checks, links in 23 documents, both Python compileall runs and git diff checks. Static typing is not configured. This checkpoint predates the configuration-bound correction; final evidence is recorded below. No real service or platform acceptance is implied.

## Initial request configuration-bound correction

The first frozen full run in `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-native-text-full-20261027-501nx3l0` passed 5636 tests and 195 subtests, with no test failures and 5 platform skips. All 871 source digests stayed unchanged; WF-06 412/1049 and WF-08 201/451 passed. Strict exit 1 remains due to Linux RELEASE-01, and all seven external checks were not_run. This run predates the following correction and does not verify the final tree.

While that source remained frozen, an independent disposable repro proved that a valid 65442-byte registered configuration can exceed the final limit after adding original Run metadata. Text, Memory and mixed input cases all raised dispatch_unknown and changed the original step to unknown despite no Run being created. Automated red `/tmp/agent-fleet-native-text-initial-config-bound-red-20261027.xml` has 3 failures. The original request builder only translated json_too_large after receiving the completed Memory snapshot, missing its initial request construction.

After the frozen run ended, the original request builder was changed to recognize complete input requirements before snapshot construction and apply its existing context_too_large/uncertain=False and final token-size checks there too. The dispatch service, token/generation and replay policy are unchanged. The original json_too_large exception remains the direct cause. Fresh `/tmp/agent-fleet-native-text-initial-config-bound-green-20261027.xml` passed all 8 original repro, promoted regression, final body-size and no-input checks; the repro command also reported one cache-location warning because its disposable test is outside the repository. No descriptor is opened and no message/Run/reader/child link is committed; the original generation records a deterministic failure.

The promoted three-case regression is registered in WF-06. Final collection contains 39 selectors / 115 cases in `/tmp/agent-fleet-native-text-selectors-final-v2-20261027.json`; WF-06 registers 413 selectors and WF-08 retains 201, with 22 Native recovery selectors / 71 cases. The following verification records cover the corrected tree.

## Final related verification after the configuration-bound correction

The corrected 38-module serial runs under Python 3.10.21 and 3.13.15 each passed 1532 tests and 2 subtests, with zero failures, errors or skips. Both include all 115 Native text cases. Their JUnit files contain 1532 testcase elements each; the 1534 suite aggregate includes the subtests. Evidence: `/tmp/agent-fleet-native-text-related-final-v2-python310-20261027.xml`, `/tmp/agent-fleet-native-text-related-final-v2-python313-20261027.xml` and `/tmp/agent-fleet-native-text-related-final-v2-verified-20261027.json`.

All 871 source digests remained identical before and after those runs. The registered matrix SHA-256 is `72f435e112437a766adf2891f527fd096dac438f743cd59bd54e00b99bbf38db`; WF-06 has 413 selectors and WF-08 has 201. Preflight `/tmp/agent-fleet-native-text-preflight-final-v2-20261027.json` passed 869 UTF-8/no-BOM texts, 613 Python AST files, 22 CJS and 8 modified frontend module syntax checks, links in 23 documents, both Python compileall runs and git diff checks. Static typing remains not configured. Complete strict-gate status and final documentation verification are recorded below.

## Final corrected complete strict gate

全仓 **5639 passed、0 failures/errors、5 个平台 skip、195 passing subtests**；JUnit 实际 5644 个 testcase，不重复累计 subtests。WF-06 **413 selectors / 1052 cases**、WF-08 **201 / 451** 全通过，41/42 journeys。严格退出 1、ci_status=failed，仅 RELEASE-01 缺少三项 Linux pidfd/proc 必需实例；另两项 Supervisor proc 平台 skip，七项外部检查均 not_run。 Frozen evidence: `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-native-text-full-20261027-u52fnau1`. All 871 source digests remained unchanged; matrix SHA-256 `72f435e112437a766adf2891f527fd096dac438f743cd59bd54e00b99bbf38db` matches the report. 44 browser PNGs passed signature, chunk CRC, dimension and full-tail checks.

All 39 Native selectors / 115 cases pass; 22 recovery selectors / 71 cases also pass in WF-08. The earlier 5636-pass run is historical. Summary/binary/code inputs, other execution contexts/configuration, Muse export and platform/live acceptance remain open.

## Post-documentation verification

The post-documentation comparison retained all 871 source paths and changed only 11 Markdown files. Runtime, tests and the matrix match the final gate byte-for-byte. Actual JUnit and journey records independently confirm all 115 Native cases and the 22-selector / 71-case WF-08 recovery subset.

Post-documentation preflight passed 869 UTF-8/no-BOM texts, 613 Python AST files, 22 CJS / 8 frontend module syntax checks, 23 documents / 273 local links, both Python compileall runs and git diff checks. Static typing is not configured. Candidate evidence is in `post-documentation-candidate.json` and `post-documentation-preflight-candidate.json` in the final evidence directory; the final source/preflight records are saved there too. The Native slice is verified locally; strict Linux and platform/live acceptance remain incomplete.
