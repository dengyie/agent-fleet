# Versioned Summary Context Implementation Plan

> **For agentic workers:** Use executing-plans inline. Do not delegate this plan.

**Goal:** Close the F5 summary input gap with explicit immutable, scoped summary versions and actual Native/external consumption, recovery and backup verification.

**Architecture:** Keep the existing Flask/SQLite WAL Workflow and Run/dispatch lifecycles. A summary is explicit operator-authored reference data, independently versioned from Memory; owner/workspace, creator, creation time and complete content participate in its canonical digest. Append-only versions and an epoch-fenced enable/disable head provide durable authority. Original private requests remain authoritative after submission; execution guards authorize the exact version, never substitute the current head.

**Tech Stack:** Python 3.10+, sqlite3, existing Flask routes and pytest; no dependencies or external downloads.

## Global constraints

- Work in `codex/functional-root-fixes`; preserve all existing development and primary main.
- UTF-8 without BOM; `hub -> fleet_core <- tools` remains unchanged.
- Only explicit summary selection; no automatic model generation, implicit search, truncation, conversion to Memory, new tool permissions or acceptance evidence.
- Summary revision is positive; exact SHA-256 identifies a canonical version, scoped to owner/workspace. New versions do not revoke old versions; explicit disabling revokes new consumption of all versions.
- Summary, Memory and text share the existing 8192-byte logical input budget. Old no-summary Native and external snapshots remain byte-compatible.
- Workflow reader 11 fences the authority and summary snapshots; old databases are not upgraded until summary data is persisted. Startup, gate-off inspection and backup recognize complete, partial, future and corrupt summary domains.
- Keep actual full Linux/browser gate and real model/CLI acceptance open; do not download pending large software packages or deploy in this implementation.

## Task 1: Durable version authority and usable operator transport

**Files:** Create `hub/domain/workflow_summary.py`, `hub/infrastructure/workflow_summary_schema.py`, `hub/infrastructure/workflow_summary_repository.py`, `hub/application/workflow_summary.py`, `tests/test_workflow_summary.py`; modify `hub/infrastructure/workflow_schema.py`, `hub/infrastructure/workflow_repository.py`, `hub/infrastructure/platform_backup.py`, `hub/composition/workflows.py`, `hub/http/workflow_routes.py`.

**Interfaces:** `WorkflowSummaryVersion` holds canonical complete content and provenance. `WorkflowSummaryRepository.publish(owner, workspace, summary_id, *, title, content, client_token, expected_epoch, now)` atomically creates revision 1 or appends the next version; an owner-scoped token returns the original result on identical retries and conflicts on changed payload. `get(..., revision=...)`, `list(..., limit, cursor)`, `set_enabled(..., expected_epoch, enabled)` and `read_selected(owner, workspace, references, *, connection=None)` expose bounded authority.

- [x] Add failing real SQLite tests for persistence/reconstruction, immutable old versions, idempotence, epoch conflicts, exact scope/hash, disabled authority and UTF-8/budget failures. Run `python3 -m pytest tests/test_workflow_summary.py -q` and retain red output.
- [x] Implement canonical domain values and optional schema domain with reader 11 in the same write transaction as the first publication. Validate complete history with signed-rowid batches of 128 and indexed head/version consistency; startup/backup preserve all versions, even disabled.
- [x] Wire explicit publish, bounded list, exact version get and enable/disable under the existing operator-only Workflow boundary. Creator comes from authenticated owner; submitted provenance fields are rejected. No deletion API.
- [x] Verify publish rollback, two-process last-epoch contention, HTTP owner/workspace isolation, restart and plain/encrypted frozen backup. Record exact commands and reports.

## Task 2: Complete Native and external input consumption

**Files:** Modify `hub/domain/workflow_native_memory.py`, `hub/domain/external_request.py`, `hub/application/workflow_external_inputs.py`, `hub/infrastructure/workflow_run_executor.py`, `hub/infrastructure/workflow_native_memory_repository.py`, `hub/infrastructure/workflow_input_resolver.py`, `hub/infrastructure/workflow_external_schema.py`, `hub/infrastructure/workflow_external_repository.py`, `hub/composition/workflows.py`; create `tests/test_workflow_summary_inputs.py`.

**Interfaces:** Explicit summary inputs remain separate from legacy Memory arrays. Native schema 3 and external inner prompt schema 3 include complete ordered summary values only when present. External metadata version 4 plus Workflow reader 11 fences new prompts; parsers retain old byte identities. Selection routes each kind to its own authority.

- [x] Capture current summary dispatch rejection in an automated original Run/worker and external adapter scenario; retain the initial failure.
- [x] Resolve exact summary revisions and complete values; combine text/Memory/summary budgets, ordering, hash and duplication checks without changing old representations.
- [x] Persist summaries in the original private Run/request transaction; guard current head enabled, exact immutable version and scope before provider/send. New head versions do not replace or invalidate selected old versions. Original lookup/recovery does not reconstruct content.
- [x] Validate saved historical summary reference closure independently of enabled state; reject corruption, missing authority, low readers and unsupported execution contexts before I/O. Keep held/isolated and unknown facts unchanged.
- [x] Verify original worker consumption, mixed inputs, revocation-before-send, crash/lookup with new revisions, private DTOs, final historical batch corruption, rollback and backup under both old and new formats.

## Task 3: Contract, journey and evidence closure

- [x] Extend execution/artifact contracts with explicit operator summary provenance, revision/epoch rules, consumption schemas and reader compatibility. Link this plan from the existing development ledger and acceptance matrix.
- [x] Register implemented selectors in WF-06/WF-08; run relevant Native/external/backup/retention/gate/boundary suites, Ruff, diff, Python 3.10 AST and UTF-8/no-BOM checks.
- [x] Record completed scope and exact evidence; leave remaining F5 inputs/configuration, full release gate and real pilots open. No overall completion or publication claim.

## Native Windows backup correction discovered during verification

The exact `4a3e15f1` snapshot passed 64 summary cases on Windows and failed all six summary backup/restore cases: staging opens ordinary files read-only, but Windows `_commit`/`fsync` requires write access. Fix `_fsync_path` to request a writable ordinary-file descriptor on Windows, preserve POSIX read-only directory/file handling, and keep flush errors fatal before publication. Add real file-flush and publication-failure preservation regressions; retain the original six failures. Verify the same candidate on Windows with summary plus existing plain/encrypted backup suites, and on macOS/Linux with the associated storage suites. This does not establish power-loss durability of NTFS directory rename or change the existing Windows directory-sync limitation.

- [x] Record the original Windows failure and reproduce an ordinary file flush before the fix.
- [x] Correct descriptor access, verify complete file bytes and failure preservation, then re-run plain/encrypted backup and summary on native Windows.
- [x] Preserve original and corrected source/report identities, and keep any unrelated native-host failures visible.


## Verified results — 2026-11-11

All evidence is retained under `/Users/mango/.cache/agent-fleet/verification/summary-context-20261111/`. These are independent verification Git snapshots, not development commits, PRs or deployments.

| Immutable source | Verification and scope |
| --- | --- |
| `ebd91052770766810b3a79d40c93ead2d109bbf8` / 1156 files | Initial macOS associated 831 passed; initial Linux 830 passed/1 failed. Original macOS full run 7041 passed/1 failed/5 Linux-only skips/204 subtests, 42/44 journeys. Both failures preserved: obsolete pre-suspension event assertion on macOS, revision scan on SQLite 3.40.1 on Linux. |
| `4a3e15f186b5bcd2b303123012876681dd1a2076` / 1156 files | Corrected Linux 23-module associated run 832 passed/0 skip/346.992s. macOS Python3.13.7, existing Chrome/Playwright1.62.1 full run 7043 passed/0 failures/errors/5 Linux-only skips/204 subtests/911.90s, 43/44 journeys, 2071 selectors, 46 complete PNGs; original verifier still exits 1 with `journey_incomplete`. Windows summary run 64 passed/6 failed, all backup file-flush failures. |
| `b44fe1bc8377ee966889f19a93b9e4007c23613f` / 1156 files | Windows file-flush correction: identical four-module summary/plain/encrypted backup scope on macOS 96 passed/8.58s, actual Windows11/Python3.12.7 96 passed/76.234s and SG Debian12/Python3.11.2 96 passed/38.856s; all zero failures/errors/skips. This is an increment after the full run, not a new full gate. |

- Original summary authority red: 31 failed; original missing-consumption red: 16 failed. Send-intent revocation and gate-off corrupt authority failures are retained; current tests cover the original request transaction, full provider/adapter input, mixed Memory/text/summary, original lookup after head changes/disable, private DTOs, rollback and frozen plain/encrypted restore. Real two-process publication and signed-rowid128 historical closure execute against SQLite; summary binding crash injection is not mislabeled as an OS-kill test.
- SQLite 3.40.1 used only the first three key columns for tuple-IN and scanned 1500 revisions: original exact closure probe reached 21600 instructions. VALUES/CROSS JOIN now drives complete four-column primary-key seeks. Native selected/closure measurements are 3100/3600 instructions at 100-instruction sampling; the existing 4000 threshold remains. A 129-version, two-page historical test preserves multiple revisions and disabled authority and rejects corruption on the final version.
- Current-reader-plus-one replaces stale future-reader literals in existing Memory/text/output tests; low-reader and unknown-future rejection remain. The original remote acceptance test now accounts for immediate receipt or the actual waiting_node/restarted-worker path, retaining its exact ordering, two provider calls and one journaled Node command; 52 acceptance/suspension/checkpoint associated cases passed independently.
- Native Windows actual ordinary file flush reproduced `OSError: [Errno 9] Bad file descriptor` before the change. Owned staged files now use a writable CRT descriptor; flush errors still reject publication and preserve staged bytes. The six original backup failures and isolated file-flush red are retained. No NTFS directory power-loss guarantee is claimed.
- Final matrix is 44 journeys/2073 selectors, 7050 collected instances, no missing selector. This collection is not execution evidence. CI fatal/undefined/unused-import Ruff, Python3.10 AST for 817 Python files, UTF-8/no-BOM and diff checks pass. The post-freeze removal of an already unused test import is recorded separately from immutable source.
- Original packager for `b44fe1bc` produced 1080 backend files, 6628994 bytes, SHA-256 `e59506aa4fb541d7b516d7f8c093dc16ab5fa7868fc059e13261c5ac433e62a5`, all checked against frozen source. No development commit, push, merge, model/CLI call or deployment occurred.

## Explicit operator UI follow-up — 2026-11-11

The administrator UI is a separate, explicit operator surface over the authority described above. It supports workspace-scoped list/head/history reads, immutable manual publication and enable/disable; it does not generate summaries, select them for runs, or grant acceptance authority. The current branch also registers the browser and actual-DTO parser contracts in WF-06.

The first real Chrome run caught two UI/API integration failures: publish parsing required an `idempotent_replay` field the HTTP route does not return, and the head wrapper returned a bare DTO while recovery expected `{summary}`. Both were corrected. Actual publish/list/head/version/enable DTO parser tests and administrator Chrome acceptance now pass. Combined focused summary authority/input/DTO/account tests: **79 passed**; administrator and ordinary-account Chrome journey: **1 passed**; Ruff, JS syntax and `git diff --check` pass. These are focused current-worktree results, not a new full release gate.

Remaining global work stays open: same-source complete Linux/browser release gate; real commercial CLI/model delivery and production sandbox; complete WF-04/05 and live MCP/Muse/mail/deployment; remaining F5 binary/code-package/other contexts/requirements/configuration, external adapter stop, archive/fence retirement and F6 real workload observations. Automatic summary generation remains unimplemented. Download approval for Chromium (195836009 bytes) and libllvm15 (23115156 bytes) remains absent.
