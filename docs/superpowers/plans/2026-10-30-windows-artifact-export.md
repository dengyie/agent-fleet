# Windows Artifact Export Implementation Plan

> **For agentic workers:** Execute inline with executing-plans in the existing isolated feature worktree. Preserve the protected primary worktree, all ongoing changes and every test run. No development commit, push, merge, deployment, global configuration change, software installation or cleanup.

**Goal:** Complete the Windows fixed-source boundary for the original signed Node artifact export protocol and verify it on authorized home-win.

**Architecture:** Extend the existing native Windows filesystem adapter and ArtifactExporter at their filesystem I/O boundaries. Keep the original ExportRequest/ExportReceipt, HTTP transfer service, export journal, NodeRuntime, two shared slots and lifecycle. A native read handle pins the selected source without write/delete sharing; repeated exact identity and ACL checks precede publication.

**Tech Stack:** Python 3.10+, stdlib ctypes/SQLite, installed cryptography/pytest; Windows 11 NTFS and the existing macOS/Linux verification runtimes. No new dependency.

## Global constraints

- Authority: [artifact contract §4.2](../specs/2026-10-20-artifact-capability-integration-contract.md#42-固定-node-导出与本机交接), with §4.1's existing Windows root/reparse/ACL boundary.
- The signed command selects only `output_key`; workspace/run/node/command identities remain original. Administrator configuration supplies root, relative path and public name. No URL, path, command, capability or file generation override.
- Windows root stays local fixed NTFS, absolute drive path, at most 128 ancestor components. Output relative paths use `/`, at most 32 components/512 UTF-8 bytes; each component follows the existing Windows policy. Reject ADS, drive-relative/absolute/UNC/device namespaces, links/reparse, reserved device names, empty/dot/trailing-space-dot components, invalid Unicode and traversal.
- Hold all original parent handles without DELETE sharing. Open nested output directories relative to retained parents and verify their owner/DACL. The regular single-link source is opened relative to its final parent with FILE_SHARE_READ only; an existing writer must prevent admission and a later writer/rename must be denied.
- Persist volume serial, 128-bit file ID, size, exact native LastWriteTime/ChangeTime ticks, attributes, owner SID and a deterministic DACL fingerprint in the existing private identity JSON. Integers stay decimal strings; FILETIME conversion uses integer multiplication, no floating-point timestamps. Receipt and public DTO formats do not change.
- The DACL fingerprint covers descriptor control, ACL revision and ordered ACE type/flags/mask/SID. It is derived from actual checked handle security, never an address-dependent pointer representation. Existing current-user/SYSTEM/Administrators write policy and 256-ACE bound remain enforced; configuration ACLs are not automatically rewritten.
- Hash in 64 KiB blocks, upload at most 1 MiB per part, at most 256 parts/256 MiB. O(n) byte processing, O(depth) bounded traversal, O(1) identity/offset matching; no full source copy or in-memory assembly.
- Original `prepared → reserved → committing → ready` and reconciliation, three attempts, original transfer/artifact ID, two-hour command ceiling, cancellation/deadline and lost-response recovery remain intact. A ready outbox does not require the original source file to remain present. Unknown work is not re-executed or silently released.
- Windows capability advertisement verifies the configured output/slot roots. Recovery still enters the original execution checks so an unsafe previously admitted root becomes reconciliation, not an endless unavailable retry. Receipt lookup remains private and independent of source content.
- No new journal schema, worker, service, generic platform framework, empty façade, protocol or dependency. All output uses UTF-8 without BOM.
- Windows platform probes are explicit opt-in files, never skipped required Linux selectors. Real remote Hub acceptance is separate from filesystem/protocol test doubles. Source-bundle exchange between two actual artifact Nodes and full WF-04 are separate exit requirements.

## Task 1: Native source identity and protected opens

**Files:** Modify `tools/platform/artifact_windows.py`, `tests/test_artifact_windows.py`; create `tests/fixtures/workflow/windows_artifact_export_probe.py`.

**Interfaces:** `validate_relative_path(value: str) -> tuple[str, ...]`; `source_identity(descriptor: int) -> dict[str, str]`; `source_handle(root: Path, relative_path: str) -> ContextManager[tuple[BinaryIO, dict[str, str]]]`. Existing `FileInfo` and receive identity equality remain unchanged. `_Native.child(..., share_write: bool = True)` retains all receive/lock defaults; only source opens disable write sharing.

- [x] Add executable neutral path boundary tests and a signed Windows export case. Run them against the pre-export implementation; preserve the unavailable result before editing production code.
- [x] Implement exact component validation, native basic/standard/file-ID reads, deterministic checked owner/DACL identity, nested handle pinning and source opens with FILE_SHARE_READ only.
- [x] Verify nested source bytes/identity, writer/rename refusal, release on normal close/error/process exit, junction/hardlink/type/ACL refusal, NTFS/root boundaries and source metadata changes on real Windows. Test a live writable mapping separately; do not claim it is blocked without an actual result.

Neutral policy verification command:

```bash
python -m pytest tests/test_artifact_windows.py tests/test_artifact_export.py::test_output_registration_rejects_unsafe_paths -q
```

Explicit Windows command, with a fresh retained task-private basetemp and report for each run:

```powershell
python -m pytest tests/test_artifact_windows.py tests/fixtures/workflow/windows_artifact_export_probe.py -q --junitxml=windows-export.xml --basetemp=windows-export-fixtures
```

## Task 2: Original exporter and journal integration

**Files:** Modify `tools/platform/artifact_export.py`, `tools/platform/artifact_transfer.py`, `tests/test_artifact_export.py`, `tests/fixtures/workflow/windows_artifact_probe.py`; extend the new Windows export probe. Existing `artifact_export_journal.py` is reused without a schema change.

**Interfaces:** ArtifactExporter constructor/export/recover/pending_receipts/acknowledge/deferred and OutputFile remain unchanged. `_output_handle` and `_unchanged` select native Windows source identity only on Windows; original POSIX identity bytes remain unchanged. `export_supported()` is independent of receive configuration. `available` checks protected Windows configuration roots; actual export/recovery checks the selected source inside the original error/reconciliation scope.

- [x] Connect native source handles and identity to hash, each part, path revalidation and commit; preserve exact fixed HTTP request and response checks.
- [x] Check malformed Unicode at OutputFile registration with a typed error and original cause. Preserve legitimate POSIX paths; Windows applies its stricter component policy.
- [x] Verify original ready receipt replay without a source, same-ID resume after reserve/part/commit response loss, three-attempt stop, corruption, cancellation/expiry and changes before/after a part; unsafe sources never commit.
- [x] Verify 12 MiB source streaming under the existing 8 MiB tracemalloc limit and original combined receive/export two-slot capacity.
- [x] Execute four actual Windows process exits after reservation, part, commit and journal ready; recover the same original artifact/parts and release the original handles/locks. Test fixture transport results remain labeled as protocol evidence.
- [x] Keep receive configuration from automatically advertising export; rerun all original Windows receive probes after the shared native adapter changes.

POSIX regression command (run independently on Python 3.10 and 3.13):

```bash
python -m pytest tests/test_artifact_windows.py tests/test_artifact_export.py tests/test_artifact_receive.py tests/test_artifact_capacity.py tests/test_workflow_two_nodes.py tests/test_platform_node_http_e2e.py tests/test_node_capabilities.py -q
```

## Task 3: Actual remote Node export, frozen verification and documents

**Files:** Create `tests/fixtures/workflow/windows_export_canary.py` and `tests/fixtures/workflow/windows_export_hub_probe.py`; update artifact contract, roadmap, implementation progress, testing entry/chains and this plan only after actual evidence.

**Interfaces:** The controller requires existing `FLEET_WINDOWS_TEST_HOST`, `FLEET_WINDOWS_TEST_ROOT` and `FLEET_WINDOWS_TEST_EVIDENCE`. It uses original `export_node`/Hub fixtures and a temporary SSH reverse-loopback listener; the remote Node receives its synthetic credential through private stdin, never argv/logs. NodeRuntime polls the original signed command and posts the original receipt; no direct Hub database or manual file insertion substitutes for export.

- [x] Run the remote Windows NodeRuntime against the original disposable POSIX Hub. Lose an accepted part or terminal receipt response, restart with the original journal, and prove exact hash/bytes, one transfer/artifact ID, original output binding and original Command success. No downloaded content is executed.
- [x] Freeze all final source files once, retain development HEAD/branch, source map, matrix digest, platform facts and per-run commands/JUnit/logs. Verify source hashes before/after each actual environment; never splice runs or weaken the original verifier.
- [x] Run Python 3.10/3.13 compileall, new-file annotations/control depth, UTF-8/no-BOM, local links and diff checks. Compileall is syntax verification; no static type checker is currently configured.
- [x] Run the unchanged strict Linux whole-repository command and original release verifier against that final clean snapshot; independently verify the copied evidence/44 PNGs against the same revision and matrix.
- [x] Record actual Windows/POSIX/Linux/remote results and the exact implemented scope. Split any remaining two-Node/source-bundle/full-WF-04/05 and F3/F5 live requirements from this slice; do not mark the full project complete or enable production gates.

Remote opt-in controller command:

```bash
python -m pytest tests/fixtures/workflow/windows_export_hub_probe.py -q --junitxml=windows-export-http.xml
```

Strict Linux command and verifier:

```bash
python -m pytest tests -q --require-journeys --journey-report=evidence/journeys.json --junitxml=evidence/results.xml --tb=short -rs
python -m tools.testing.release_evidence --evidence evidence --checkout . --revision "$FLEET_FROZEN_REVISION"
```

The active transfer gate remains false. Every evidence result is tied to the source snapshot it actually ran; post-freeze documentation changes are separately audited.

## Pre-freeze evidence (2026-10-30)

Evidence root: `/Users/mango/.cache/agent-fleet/verification/windows-export-20261030-bc40db8dad18/`. The existing source maps retain the development branch/HEAD and all preliminary runs. No reports are merged into a passing total.

- `policy-red.xml`: 31 failures/38 passes before the path/Unicode implementation; `export-red.xml`: signed export refused as unavailable before native support.
- `export-green1.xml`: 21 failures/77 passes exposed a fixture source owned by Administrators. The fixture now creates its source through the original private descriptor; the current-user owner policy remains unchanged.
- `export-green2.xml`: 127 passes/one failure. All 38 export and 60 neutral cases passed; the original receive junction fixture exceeded PowerShell's path length. Its unchanged 30 cases passed separately in `receive-short.xml` with a shorter private basetemp. A fresh final combined run is still required.
- `metadata.xml`: three actual NTFS LastWriteTime/ChangeTime/attributes changes after a stored part were read back through the native API; each blocked commit, preserved bytes and released the source handle for rename. Native timestamps retain their exact integer tick values.
- `posix-py310-first.xml` and `posix-py313-first.xml`: each 295 passed/zero skip. An additional macOS invocation mistakenly selecting the Windows-only native fixture produced 38 setup errors; that platform mismatch report is retained and is not a product regression result.
- `http-first.xml`: two controller-fixture failures from intercepting the old `post_json` path. `http-second.xml`: two passed through the actual bounded JSON receipt path; part/receipt response loss resumed the original artifact, one part and original Hub output binding. DiskSink results remain protocol evidence only.

The preliminary results above retain their own source maps. The final frozen Windows/POSIX/Linux results are recorded below; the earlier receive-only revision and 5754-pass gate are not reused as export evidence.

## Final frozen verification (2026-10-30)

Final evidence root: `/Users/mango/.cache/agent-fleet/verification/windows-export-20261030-bc40db8dad18/final-snipwvt1`. All 886 Git-visible source files (884 UTF-8/no-BOM text and two existing binary brand assets) were copied once into an independent clean Git checkout at revision `6ba4384adde2a01f44dd96b8ec9c85c2e9a2d839`. The development tree stays on `codex/workflow-platform-docs`, HEAD `4cd816230fd1d8d5bf02c9f086959d8b5b1a27cf`, with its authorized changes preserved and no development commit. Matrix SHA-256: `061fcdd9c41f229ca0d6efa733e5a48a37a29ae2b2bcd7151b97fd8095eb2682`; 1490 distinct required selectors, 1729 summed across 42 journeys. `provenance.json` retains every source hash and archive hash; source bundle 3,179,075 bytes and ZIP 3,287,827 bytes.

| Environment / exact scope | Fresh result | Evidence |
|---|---|---|
| home-win Windows 11/NTFS, Python 3.12.7: 60 neutral + 41 export + 30 unchanged receive cases, one combined run | 131 passed, 0 failures/errors/skips; exit 0 | `windows-native.xml`, `windows-native.log`, `windows-command.json` |
| Actual home-win NodeRuntime → original disposable POSIX Hub, independent part/receipt response loss | 2 passed, 0 failures/errors/skips; exit 0; one PUT per case, receipt posts 1/2, original transfer/artifact ID and output binding | `windows-http.xml`, `windows-http-command.json`, `windows-http-report-part.json`, `windows-http-report-receipt.json` |
| macOS Python 3.10.21 related seven-module collection | 295 passed, 0 failures/errors/skips; exit 0; compileall exit 0 | `posix-py310.xml`, `posix-py310-command.json`, `compile-py310.log` |
| macOS Python 3.13.15 same collection, separate run | 295 passed, 0 failures/errors/skips; exit 0; compileall exit 0 | `posix-py313.xml`, `posix-py313-command.json`, `compile-py313.log` |
| gz-cvm Linux Python 3.13.16, original strict full command | 5785 passed, 197 passing subtests, 0 failures/errors/skips, 42/42 journeys; exit 0 | `linux-evidence/controlled-full/results.xml`, `full.log`, `full-command.json`, `journeys.json` |
| Unchanged original release verifier on gz-cvm, then independently on copied local evidence | Both exit 0; 42 journeys, 1729 selectors, 5785 JUnit testcase elements and all 44 PNGs validated | `linux-evidence/controlled-full/release-verifier.json`, `release-verifier-local.json` |

Linux used the existing task-private Node 22.21.1/Playwright 1.63.0/Chromium 153.0.8010.12, bubblewrap/seccomp and `LC_ALL=C`; setpriv removed DAC override/read-search and set NoNewPrivs. The single full pytest process PID 3114285/start ticks 187310298 ran UTC 09:26:10–09:52:48, 1598.84 seconds wall time. It was never restarted or combined with another run. All 886 source hashes remained unchanged and checkout status stayed clean. Windows native and HTTP source audits also retained all 886 hashes. The suite count 5982 includes 197 subtests; the 5785 testcase elements and 197 subtests are recorded separately.

The 50 original Linux evidence files total 7,935,727 bytes; every copied file size/SHA-256 exactly matches `linux-evidence-files.json`. The original local verifier rechecked complete PNG data and the exact frozen checkout/matrix. Seven external checks remain `not_run`. `verification-summary.json`, `platform-evidence-digests.json`, `source-before-document-results.json` and `frozen-source-link-audit.json` retain platform facts and provenance. New/touched boundary files passed annotation/control-depth checks; 137 local document links resolved; no static type checker is configured, so compileall is reported only as syntax verification.

Two audit-driver invocation errors before the final HTTP test were retained: Windows native `-c` quote stripping, then a local UTF-8 decoder encountering legacy PowerShell progress CLIXML. A retained script file, raw-byte stderr capture and disabled progress fixed the driver; no production source or assertion changed. The actual final two HTTP cases then passed.

Only documentation result records are changed after this freeze and audited separately. Windows source export and this verification slice are complete; the separate real two-artifact-Node source-bundle exchange, full WF-04/05, real CLI/model, production sandbox and MCP/Muse pilots remain open. Transfer/capability/integration gates stay default false. No software/global configuration/services were installed or changed, and all temporary test/evidence directories are retained.
