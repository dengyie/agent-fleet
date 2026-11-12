# Terminal Postcheck Original-command Guard Implementation Plan

> **For agentic workers:** Execute inline with `executing-plans` in the existing protected feature worktree. Preserve all frozen sources and red/green evidence. No agents, development commits, merge, push, deployment, installation, settings/service changes or Obsidian edits. Cleanup is limited to the two exact fixture directories separately confirmed by the user below. Existing home-win/gz-cvm fixed capability and Linux full-gate authorization persists.

**Goal:** Require the owner-scoped original Command to remain unknown even when a complete terminal postcheck and its canonical audit already exist.

**Architecture:** Reuse the existing indexed `_require_unknown_command` in the original repository's terminal fast return. Pending completion and missing-audit recovery keep their existing transactional guard. No new domain state, API, DTO, schema, interface, capability, retry contract or background execution is introduced.

**Tech Stack:** Existing Python 3.10+, Flask, SQLite/WAL and pytest; existing installed native-test runtimes.

## Global constraints

- Authority: [M1.9/M1.10 original postchecks](../../platform/agent-fleet-self-hosted-platform.md#m19-deterministic-workspace-post-check), [atomic completion guard](2026-10-30-postcheck-atomic-audit.md#task-2-one-bounded-completion-transaction) and [capability contract](../specs/2026-10-20-artifact-capability-integration-contract.md#64-hub-固定能力-run-与-command-派发子集).
- Original command, descriptor/result/timestamps, helper, audit, Run/Workflow/binding/generation/budget and held/isolated reservation remain unchanged on refusal. No command or helper I/O is replayed.
- Owner-scoped missing original returns existing `command_not_found`/404; an original no longer unknown returns existing `reconcile_requires_unknown`/409. Repository errors retain the original application exception chain.
- A complete terminal read stays free of write transactions. One exact primary-key SELECT supplies the reused original guard; no scan, global lock, new facade or extra abstract layer.
- Fixture ownership/status changes are confined to newly created synthetic SQLite rows, retain the original row and never delete data. Missing-audit controls construct a historical terminal descriptor directly without deleting an audit.
- UTF-8 without BOM; fresh basetemp and independent evidence for each invocation. Preserve every old frozen revision/report with its own scope. Full WF-04/05 and all external checks remain open, all gates default false.

## Reproduced evidence

`/Users/mango/.cache/agent-fleet/verification/postcheck-terminal-guard-20261031-pijmzuw8` retains the original HTTP repro, six per-case observations, commands/logs/JUnit and an assertion-identical source-parameterized repro. Both separate red reports contain **6 failed / 0 errors/skips** on frozen `adf137251e21acbad036a3e51221d8cf6805731d`. Each GET returns 200/terminal matched after the original's owner or status changes; the documented result is 404 or 409. This frozen full-suite run remains immutable and cannot prove the corrected source.

The root cause is `_finish_postcheck` returning a complete descriptor after matching its saved audit before calling `_require_unknown_command`. Pending and missing-audit branches already call that guard in `_complete_postcheck`. The smallest correction is the original repository fast branch, with no service/API changes.

## Task 1: Complete regression file and red run

**File:** Create `tests/test_postcheck_terminal_guard.py`.

**Interface:** `test_terminal_postcheck_requires_original_unknown_and_owner(tmp_path, monkeypatch, kind, change, has_audit) -> None` uses original typed `Case` preparation and covers 3 original kinds × 2 original invalidations × complete/missing audit = 12 cases. Check direct repository error, fresh application error/cause, original HTTP status, unchanged all original rows and complete-read zero write transaction.

- [x] Write the full following file and run it before the product change; retain the six failing complete-audit cases and six already guarded missing-audit controls.

```python
"""Terminal postcheck reads keep the original unknown and owner boundary."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

from hub.application.task_service import ApplicationError
from hub.infrastructure.command_repository import CommandRepositoryError
from test_platform_postcheck import OWNER
from test_postcheck_atomic import KINDS, _prepare


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('change', ['owner_changed', 'status_changed'])
@pytest.mark.parametrize('has_audit', [True, False])
def test_terminal_postcheck_requires_original_unknown_and_owner(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, change: str, has_audit: bool) -> None:
    case = _prepare(tmp_path, kind)
    if has_audit:
        assert case.service.get_any(OWNER, 'original')['postcheck']['state'] == 'matched'
    else:
        encoded = json.dumps(case.result, sort_keys=True, separators=(',', ':'), allow_nan=False)
        with closing(case.repository._connect()) as connection:
            connection.execute('UPDATE ' + case.table + ' SET state=?,result=?,evidence_id=?,updated_at=? '
                'WHERE owner_id=? AND command_id=? AND kind=?',
                ('matched', encoded, 'postcheck-' + case.check_id, 200, OWNER, 'original', kind))
    before, audits = case.descriptor(), case.audits()
    assert len(audits) == int(has_audit)
    with closing(case.repository._connect()) as connection:
        if change == 'owner_changed':
            connection.execute('UPDATE platform_commands SET owner_id=? WHERE command_id=?',
                ('other@example.test', 'original'))
        else:
            connection.execute('UPDATE platform_commands SET status=? WHERE command_id=?', ('succeeded', 'original'))
    original = case.repository.get('original')
    check = case.repository.get(case.check_id)
    code = 'command_not_found' if change == 'owner_changed' else 'reconcile_requires_unknown'
    statements: list[str] = []
    connect = case.repository._connect

    def traced_connection() -> sqlite3.Connection:
        connection = connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(case.repository, '_connect', traced_connection)
    with pytest.raises(CommandRepositoryError) as raised:
        case.finish()
    assert raised.value.code == code
    with pytest.raises(ApplicationError) as application:
        case.restart().get_any(OWNER, 'original')
    assert application.value.code == code
    assert isinstance(application.value.__cause__, CommandRepositoryError)
    assert application.value.__cause__.code == code
    response = case.app.test_client().get('/api/platform/v1/commands/original/postcheck')
    assert response.status_code == (404 if change == 'owner_changed' else 409)
    assert 'postcheck' not in response.json
    assert case.descriptor() == before and case.audits() == audits
    assert case.repository.get('original') == original and case.repository.get(case.check_id) == check
    forbidden = ('BEGIN', 'INSERT', 'UPDATE', 'REPLACE') if has_audit else ('INSERT', 'UPDATE', 'REPLACE')
    assert not any(statement.lstrip().upper().startswith(forbidden) for statement in statements)
```

## Task 2: Minimal indexed fast-branch guard

**File:** Modify `hub/infrastructure/command_repository.py::_finish_postcheck` only.

**Interface:** Reuse `_require_unknown_command(connection: sqlite3.Connection, owner_id: str, command_id: str) -> None`. Keep completion signatures, fixed storage dispatch and saved terminal outcome intact.

- [x] Insert the reused guard before the existing fast return:

```python
if row['state'] in _TERMINAL_POSTCHECK_STATES and self._matching_reconciliation(
        connection, self._postcheck_audit(row, source)) is not None:
    self._require_unknown_command(connection, owner_id, command_id)
    return self._decode_postcheck(row)
```

- [x] Rerun the same twelve in-repo cases and the unchanged parameterized HTTP repro against the corrected source. Retain separate reports; all must pass, with no failures/errors/skips and no changed repro test body.

## Task 3: Current source verification and scope

**Files:** Register the new exact selector in NODE-01/WF-08, update this plan, the atomic/full-gate plans, contract and existing progress/testing indexes.

**Interfaces:** Original pytest/JUnit/journey commands and native capability fixture remain unchanged; each new frozen source map identifies its own revision and private test roots.

- [x] Run the original eight related modules plus the new regression on Python 3.10 and 3.13, independently. Run release-evidence/passive-gate checks and the original six local capability cases where the new read path is exercised. Compile changed files and verify exact registration, error causes and executable source hashes. Final documentation links/no BOM/diff checks follow the new freeze.
- [x] Preserve the earlier full-gate run as the pre-guard baseline; freeze a fresh corrected complete source and rerun the original authorized twelve native capability cases in fresh private roots. Independently verify Hub/Node evidence, every source digest and copied native file.
- [x] Complete the original strict full Linux gate without exclusions in its own fresh basetemp, then verify original source/report/PNG hashes and the original release verifier on remote and copied local evidence. The first corrected-source storage-exhausted invocation remains retained separately; the unchanged corrected revision passes the fresh rerun recorded below.
- [x] Record actual corrected native results and the failed full invocation with its storage evidence. Never reinterpret an earlier pass as current verification, add independent test totals or mark the full platform production ready.

## Verified local guard — 2026-10-31

The in-repo red report contains **6 failed / 6 passed / 0 errors/skips**. Its six complete-audit failures reproduce the documented HTTP boundary; the six historical missing-audit controls are already guarded. The first corrected in-repo run contains **12 passed**, and the assertion-identical parameterized HTTP repro independently contains **6 passed**. The repro's test-function body is unchanged.

Python 3.10.21 and 3.13.15 each independently complete the nine related modules: **291 passed / 0 failures/errors/skips**. Separate original local capability runs each contain **6 passed**, and separate release-evidence/passive-gate checks each contain **121 passed**, all with zero failures/errors/skips. Each compileall exits 0; before/after executable and matrix digests match. Python 3.13 also completes the unchanged six-case external repro. Commands, JUnit, logs and exact source maps are retained under `/Users/mango/.cache/agent-fleet/verification/postcheck-terminal-guard-20261031-pijmzuw8/verified-py310/` and `verified-py313/`.

The selector `tests/test_postcheck_terminal_guard.py::test_terminal_postcheck_requires_original_unknown_and_owner` is appended only to the existing NODE-01 and WF-08 journeys. Their former selectors and all seven external checks are preserved. The application retains the original repository error as its cause; refusal changes no original command/helper/descriptor/audit and a complete-audit read opens no write transaction.

The earlier full run on `adf137251e21acbad036a3e51221d8cf6805731d` is explicitly **pre-terminal-guard-baseline**: 5833 passed, 197 passing subtests, 42 journeys and 44 PNGs. Its 50 copied files / 7,943,776 bytes match remote digests and the original local verifier exits 0. It contains neither this guard nor its selector. Corrected native verification and the later failed full invocation have their own scope below; this earlier result cannot satisfy the corrected full gate.

## Corrected native result and full-run blocker — 2026-10-31

Fresh revision `85488105cd2d3b610fcbfa18fc2273bd02618db1` contains **898 files**. An exact comparison with the pre-guard baseline proves the one-line repository guard, the complete new regression file and only the new selector appended to NODE-01/WF-08; all original native fixtures and driver assertions are unchanged. Both local full-source compileall commands exit 0. Before result documentation, 314 local Markdown links, no-BOM, registration and diff checks pass.

The original native command completes **12 passed / 0 failures/errors/skips**, pytest 49.85 seconds; its bounded controller takes 50.28 seconds. Actual home-win Windows/Python 3.12.7 and gz-cvm Linux/Python 3.13.16 use fresh private roots. All 898 source digests match before/after on controller and both hosts; checkouts stay clean. Twelve independent read-only Hub database checks and both native journal audits verify one original write, one independent digest, zero duplicate/restart I/O, one canonical audit and unchanged original unknown/binding/generation/budget/held-or-isolated reservation. Each host supplies 32 copied files: **64 files / 545,258 bytes**, with every size/hash matched. The explicit fixture-only unknown Workflow setup and `full_workflow_completed=false` remain unchanged.

Evidence root `/Users/mango/.cache/agent-fleet/verification/postcheck-guard-20261031-t0mae74m` contains the complete source/provenance, original native commands/JUnit, `verification-summary.json` and both copied native archives. `source.bundle` is 3,243,671 bytes / SHA-256 `4713251b48ae3a81657695f744f37230200e424c5159b26a9ba415ebe91bcd03`.

The same revision's original strict Linux full command runs separately in `/tmp/afcg-full-t0mae74m` with a fresh basetemp and unchanged installed runtime. It aborts with **exit 120** after 838.98 seconds; no final JUnit or journey report is written. All 898 source digests still match and the checkout stays clean. Direct inspection finds `/dev/vda1` 50 GB root disk **100% used, 28,672 free bytes**, and inode allocation **100% used, 100 free**. The retained full log is truncated at 4096 bytes. Storage exhaustion invalidates this invocation; the partial log cannot independently explain each earlier `F`, so no passing counts or release status are inferred.

The blocker and raw storage/process/log observations are retained in `full/failure-diagnosis.json` and companion files. At that failed-run checkpoint the baseline fixtures occupy approximately 2.00 GB/41,981 entries and this failed invocation's fixtures approximately 1.07 GB/10,094 entries; no cleanup or product patch has yet occurred. The later authorized archive/deletion and successful unchanged rerun are recorded below. This failed invocation has no passing total. Full WF-04/05/NODE-LIVE/model/CLI/production/external acceptance remains open; seven external checks stay `not_run`, all gates default false.

## Verified fixture preservation and pending capacity recovery — 2026-10-31

Both exact fixture trees have now been streamed directly from gz-cvm into private local archives under the corrected evidence root's `full/`; no remote archive, extraction or deletion is performed. Each archive is checked against complete before/after SHA-256/mode/type/link manifests with bounded 64 KiB reads and indexed hardlink identities. Independent gzip and archive-digest checks pass; all file bytes, modes, hardlinks, symlink targets and FIFO metadata match, no external hardlinks exist, and both source manifests remain unchanged.

| Exact remote directory | Allocated bytes / du inode entries | Local archive / bytes / verified members |
|---|---|---|
| `/tmp/afpc-full-j20pyod1/evidence/controlled-full/fixtures` | 2,001,920,000 / 41,981 | `full/baseline-fixtures-archive/fixtures.tar.gz` / 61,857,101 / 41,983 |
| `/tmp/afcg-full-t0mae74m/evidence/controlled-full/fixtures` | 1,074,814,976 / 10,094 | `full/failed-fixtures-archive/fixtures.tar.gz` / 17,453,940 / 10,096 |

Baseline archive SHA-256 is `13f3395d66545f7f3c17bec51dca45e03f4a8af1ef8e469b0c1ea83264529662`; failed-run archive SHA-256 is `eade1614fc8c1344b74b5f718eccfb7cc398f95d398524484238da3d8132007f`. Each directory's `verification.json` and `conservation-check.json` preserve all checks. The first baseline verifier's hardlink-type assertion failure and original script remain retained; the corrected verifier rechecks the original immutable archive and complete source without weakening byte/mode/type/link assertions. This is an evidence-only correction, not a product change or full-gate result.

Read-only inspection checks 136 processes and 738 descriptors plus cwd/root/exe/maps; no fixture references or inspection errors are observed. Root disk/inodes still have only 28,672 bytes/100 available. `full/capacity-recovery-proposal.md` and `.json` make the recovery action reviewable: the two directories account for 3,076,734,976 allocated bytes / 52,075 du inode entries, and the 79,311,041-byte local archives preserve 52,079 named members. Original reports, source, Hub/Node journals, browser PNGs and installed runtimes lie outside the proposed directories.

The user's AGENTS.md requires: “Before deleting, list the matching items and get the user's confirmation for each item. Delete each confirmed item in a separate command.” At this preservation checkpoint no deletion authorization has been received and no remote directory has been deleted. The later explicit confirmation for each exact directory and separate deletion commands are recorded below; they grant no broader cleanup permission.

- [x] Preserve and independently verify both complete fixture archives before proposing capacity recovery.
- [x] Record exact directory identities, allocation, member/link manifests and observed live-reference/storage facts.
- [x] Obtain separate explicit user confirmation for each exact directory proposed for deletion.
- [x] Recheck directory identities, archives and live references, then delete only each confirmed directory in its own command; retain all evidence outside it.
- [x] Recheck actual disk/inode headroom, then rerun the unchanged original full command on revision `85488105cd2d3b610fcbfa18fc2273bd02618db1` in a new private root and previously nonexistent basetemp. Require completed original JUnit/journeys/PNGs and the original remote/local release verifiers before any full-gate claim.

While awaiting the two directory confirmations, read-only `full/capacity-recheck-l6xbjh6e/verification.json` confirms both original directory device/inode identities and both complete archive sizes/digests unchanged; available space remains 28,672 bytes/100 inodes. No full-test process is launched.

A complete local-only rerun package was prepared at `full/rerun-prepared-7_12st95/`, with planned fresh remote root `/tmp/afcg-rerun-7_12st95`. All 898 original source digests and bundle/zip/provenance copies match. Seven original control scripts compile independently on Python 3.10/3.13; only fixed remote directory references change, with the original full test driver, selectors/assertions and release verifiers otherwise identical. `prepared-rerun.json` records every copied script digest, compiler command/result and the complete planned strict pytest command. The initial display's script-count typo is preserved/corrected against the compiler's actual seven-script outputs; it affects no code or test evidence. Its `prepared_only=true`, `remote_directory_created=false`, `tests_started=false` and `full_gate_completed=false` describe that historical preparation only. The subsequent run and verification have separate records below.

## Authorized recovery and verified corrected full gate - 2026-10-31

The user separately confirmed deletion of each exact directory in the table above. `full/authorized-recovery-k5a1cb98/` retains `authorization.json`, fresh complete remote manifests, archive size/SHA-256/gzip checks, directory device/inode and ancestor checks, mount and live-reference checks, and one independent command/result per deletion. Both commands exit 0 and both exact directories are absent. Every file outside each fixture subtree under its old run root retains its previous bytes, mode and inode. The complete local archives remain retained; historical proposal/conservation records are not overwritten. Final observed headroom is **6,126,874,624 free bytes / 12,011,729 free inodes**. The earlier improvement before the first deletion is not attributed to these commands. `recovery-summary.json` records this recovery checkpoint, before the later test.

The unchanged corrected revision `85488105cd2d3b610fcbfa18fc2273bd02618db1`, **898 files**, then runs in new `/tmp/afcg-rerun-7_12st95` with a previously nonexistent basetemp. The original strict command completes **5845 passed / 197 passing subtests / 0 failures/errors/skips**, pytest **1730.57 seconds**; the foreground controller records **1732.42 seconds**, exit 0. The same original process runs from `2026-10-31T02:10:11.793920+00:00` to `2026-10-31T02:39:04.217882+00:00`, PID 3414182/start ticks 193334474. Observation does not start another pytest child. Python 3.13.16, Node 22.21.1, Playwright 1.63.0, Chromium 153.0.8010.12 and the original process-scoped sandbox/runtime environment are unchanged.

The original release verifier passes remotely and against copied local evidence: **42 journeys / 1747 selectors / 5845 JUnit cases**, **44 PNGs**, no failures/errors/skips and all seven external checks `not_run`. Both frozen checkouts stay clean and all 898 source hashes match before/after. `full/rerun-prepared-7_12st95/corrected-preservation.json` and `linux-evidence/controlled-full/release-verifier.json` retain the full commands/results. All **50 copied files / 7,940,720 bytes** match the remote size/hash manifest; archive SHA-256 is `3eaeec70306e25075202b4a93a2ace343af1d61d2eb63dae8dd2bffcddcf8aef`. The remote evidence wrapper's local zsh read-only `status` variable error is retained separately; the remote result was already produced, and a direct original verifier invocation independently exits 0. No source, test, selector or verifier assertion is changed.

This closes the corrected-source full-gate item, independently of the twelve-case native matrix and the old 5833-pass baseline. Full WF-04/05, NODE-LIVE, real model/CLI, production isolation and external acceptance remain open; `full_workflow_completed=false` for the native fixture, and all gates stay default false. No development commit, push, merge, deployment, installation, global setting/service change or Obsidian edit is performed.
