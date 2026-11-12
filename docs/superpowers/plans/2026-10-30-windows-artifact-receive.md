# Windows Artifact Receive Implementation Plan

> **For agentic workers:** Use executing-plans inline in the existing isolated worktree. Preserve other development and test evidence; no commit, push, merge, deployment, system configuration change or cleanup.

**Goal:** Implement and verify Windows Node artifact reception using actual kernel handles, without weakening the original signed command, receipt, recovery or POSIX boundaries.

**Architecture:** Reuse ArtifactReceiver, its independent SQLite journal and the original NodeRuntime lifecycle. Add one Windows filesystem adapter backed by stdlib ctypes and native handle-relative operations; platform branches stay at filesystem I/O boundaries. Export remains unavailable on Windows until its separate source-file protection is implemented and tested.

**Tech Stack:** Python 3.10+, stdlib ctypes, existing SQLite/cryptography/pytest; Windows 11 NTFS and existing macOS/POSIX regression environments. No new dependency.

## Global Constraints

- Authority: [artifact contract §4–4.2](../specs/2026-10-20-artifact-capability-integration-contract.md#4-下载接收和本地目录). Fixed alias, signed grant, owner/workspace/node/generation, exact bytes/hash and original receipt remain mandatory.
- No arbitrary destination, URL, program, automatic extraction, installation or execution. Default transfer gate remains false.
- Native Windows operations must reject reparse points, junctions, alternate data streams, device/UNC namespaces, relative/ambiguous path components and target replacement. No path-based check/use fallback.
- Pin ancestors and final root with handles that deny delete sharing. Open children relative to the pinned root with NtCreateFile; verify directory/file type, local filesystem, stable volume/file ID, owner/DACL and link count.
- A private root belongs to the current token user. Write-capable allow ACEs may only grant that user, LocalSystem or Builtin Administrators; null/unsupported ACLs fail closed. Do not change an existing administrator-selected root's ACL automatically.
- Staging uses exclusive creation, explicit identity recorded before bytes, non-following restart, exact hash and FlushFileBuffers. Publish uses handle-relative FileRenameInformation with ReplaceIfExists=false, followed by final identity/hash verification. Unsupported durability/identity facilities fail closed.
- Per-grant and two-slot locks use real nonblocking kernel byte-range locks on fixed private files, released on handle close/process exit. Existing POSIX flock behavior remains intact.
- Stream size/hash work O(n), fixed 64 KiB buffer, O(depth) root traversal with bounded path depth; lock pool has two entries. File identity comparisons are O(1). No new state schema, queue, background worker, façade or generic platform framework.
- Windows capability advertisement requires the actual native adapter and root protection. Windows export advertisement must remain independent of receive support.
- UTF-8 without BOM. Real Windows test evidence must be separate from Linux/macOS results; unsupported-platform tests do not count as Windows passes or replace the original strict Linux gate.

## Task 1: Native Windows filesystem boundary

**Files:** Create `tools/platform/artifact_windows.py`, `tests/test_artifact_windows.py` and `tests/fixtures/workflow/windows_artifact_probe.py`.

**Interfaces:** `windows_supported() -> bool`; `root_handle(root: Path) -> ContextManager[int]`; `file_info(fd: int)` with `st_dev/st_ino/st_size/st_nlink`; `open_child(parent: int, name: str, *, writable: bool, create: bool = False, exclusive: bool = False) -> int`; `entry(parent: int, name: str)`; `publish(parent: int, source: str, target: str) -> None`; `lock_file(parent: int, name: str) -> ContextManager[None]`. Windows has no POSIX directory-fsync substitute in this adapter: the same source file is flushed before and after handle-relative rename, and every journal recovery verifies file identity and complete bytes before returning ready. Process-exit tests do not claim power-loss durability.

- [x] Record the original Windows receive-unavailable result and preserve the frozen source digest before implementing the adapter.
- [x] Implement exact-width ctypes structures/function signatures, error translation preserving WinError causes, lazy loading, and strict local drive/component validation.
- [x] Implement handle-relative opens and root pinning, actual owner/DACL checks, reparse/type/link checks, full stable identity, exclusive staging and no-replace handle rename.
- [x] Run the Windows probe against private fixtures: successful bounded write/read/rename, existing target preservation, reparse/junction/hardlink refusal, broad-writer ACL rejection, root rename denial, ADS/device/UNC refusal and actual lock conflict/release.

## Task 2: Original receive/journal integration

**Files:** Modify `tools/platform/artifact_transfer.py`, `tools/platform/artifact_lock.py`, `tools/platform/artifact_export.py`; extend `tests/test_artifact_windows.py` and the Windows probe.

**Interfaces:** Original ArtifactReceiver constructor, receive/recover/pending_receipts/acknowledge and signed command/receipt bytes remain unchanged. Internal filesystem branches use the adapter; `export_supported() -> bool` preserves the original POSIX-only exporter until independently extended.

- [x] Demonstrate a failing Windows signed receive before enabling support, then execute the same command through the adapter.
- [x] Keep receive attempts, receiving→publishing→ready and reconciliation unchanged; retain exact volume/file IDs as decimal journal strings without schema changes.
- [x] Verify restart returns the same receipt with no second network download; hash/length/cancellation errors never publish.
- [x] Verify actual spawned-process exits before/after rename and ready, plus partial bytes; recovery uses the original command, grant and target. Verify per-grant kernel locks and the fixed two-slot pool across independent processes; the original receiver contention cases additionally use independent instances/threads.
- [x] Verify NodeRuntime advertises only protected configured receive roots, never Windows export merely because receive becomes supported.

## Task 3: Regression, frozen evidence and document alignment

**Files:** Update artifact contract, roadmap, implementation progress and testing documents after real results exist; only add executable journey selectors that run on that journey's actual platform.

- [x] Run `python -m pytest tests/test_artifact_windows.py tests/test_artifact_receive.py tests/test_artifact_export.py tests/test_artifact_capacity.py tests/test_workflow_two_nodes.py tests/test_platform_node_http_e2e.py tests/test_node_capabilities.py -q` on POSIX without changing existing assertions.
- [x] Run Windows-only probe/receive cases on authorized home-win in a unique temporary directory, using installed dependencies; preserve commands, source hashes, machine/Python/filesystem facts and JUnit/logs.
- [x] Run Python 3.10/3.13 compileall and UTF-8/no-BOM, local Markdown link and diff checks. Any Windows failure keeps receive unavailable; each Linux result remains scoped to its original revision.
- [x] Run the original strict Linux gate against the final frozen source and verify its original JUnit, journey report and all PNGs with the unchanged release verifier; audit the local evidence copy.
- [x] Document actual implemented subset and remaining Windows export, two-host handoff and full WF-04/live acceptance boundaries. Do not declare the full project complete.

## Environment preflight (2026-10-30)

home-win was explicitly authorized for isolated temporary tests. Read-only SSH preflight confirmed Windows 11 `10.0.22000.0`, host `DESKTOP-VST7EIE`, existing Anaconda Python 3.12.7 at `E:\code\conda\python.exe`, pytest/Flask/cryptography/PyYAML/jsonschema available, and Git installed. No installation, configuration edit or remote test-directory write occurred during this preflight.

## Initial implementation evidence

The original Windows signed receive failed closed with `capability_unavailable`. The first native six-case probe and integrated 58-case Windows suite passed; macOS Python 3.10 related regression passed all 264 cases. The real Windows Node-to-disposable-Hub HTTP canary passed after losing the first receipt response: original Command succeeded, exactly one download/two receipt POSTs, restart preserved the receipt, and the original coordinator then moved waiting_node to ready. The first two canary attempts exposed fixture-only output decoding/import setup issues, both preserved in the evidence directory and corrected before the passing run. These intermediate results precede the final frozen package.

All test files and evidence remain in `/Users/mango/.cache/agent-fleet/verification/windows-artifact-20261030/` and the uniquely named remote temporary directory recorded in `environment.json`. No global installation, setting, service or existing cache was changed.

## Final frozen verification (2026-10-30)

The disposable evidence checkout fixes revision `9868cb0c9a51dfe33086bca3c9b15293efaf6404`, containing the same 882 source files as the final Windows package and the development worktree before these result-only document updates. It contains 880 UTF-8/no-BOM text files and two existing binary assets. The development HEAD remains `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf`; no development commit was made. Matrix SHA-256 is `d4e37da147eb33ca928335f93e7f40f94d726db973b3036db8182226682fab39` (1488 distinct required selectors, 1727 summed across 42 journeys).

| Verification | Actual result | Retained evidence |
|---|---|---|
| home-win Windows 11/NTFS, installed Python 3.12.7; explicit Windows probe and path suite | 60 passed, no failures/errors/skips; 5.62 seconds. Source hashes unchanged. A separate run with an explicit retained private basetemp also passed the same 60 cases in 4.95 seconds; these counts are not added together. | `windows-final.xml/log`, `windows-final-audit.json`; `windows-isolated-final.xml/log`, `windows-isolated-final-audit.json` |
| Actual remote Windows NodeRuntime → original disposable Hub HTTP, first receipt response lost after acceptance | 1 passed; one download, two receipt posts; restart preserved the exact original receipt and the coordinator then made the step ready | `http-final.xml/log`, `http-final-report.json` |
| macOS related regression, Python 3.10.21 / 3.13.15 | Each version separately passed all 264 cases, no failures/errors/skips; 27.28 / 22.50 seconds | `posix-related-final.xml`, `posix-related-final-py313.xml` |
| Frozen gz-cvm Linux full suite, Python 3.13.16 / Node 22.21.1 / Playwright 1.63.0 / Chromium 153.0.8010.12 | 5754 passed, 197 passing subtests, zero failures/errors/skips; 42/42 journeys; pytest exit 0, 1592.54 seconds | `linux-final-jfzfjw57/evidence/controlled-full/{full-command.json,full.log,results.xml,journeys.json}` |
| Original release verifier on that frozen checkout, remote then local | Both exit 0: 42 journeys, 1727 selectors, 5754 actual JUnit testcase elements; all 44 PNGs verified. Seven external checks remain `not_run`. | `release-verifier.json/log`, `release-verifier-local.json/log` |

Linux ran with `LC_ALL=C`, DAC bypass capabilities removed and NoNewPrivs, using the existing task-private sandbox runtime. All 882 source hashes remained unchanged and the frozen checkout stayed clean. The 50 remote evidence files (7,922,292 bytes) were copied locally and every size/SHA-256 matched, with no missing or extra files. `verification-summary.json`, `source-before-document-results.json` and `linux-final-jfzfjw57/evidence/copy-audit.json` record the correspondence. JUnit's suite-level count includes subtests; it is not used to inflate the actual testcase count.

Only the Windows reception implementation and its verification slice are closed. Windows protected-source export, two actual artifact Nodes exchanging a source bundle with independent tree/blob/fixed-pytest validation, full WF-04/05, actual CLI/model use and production/external pilots remain open. Windows process-exit recovery does not prove power-loss durability. Transfer stays disabled by default.
