# Atomic Postcheck Evidence and Audit Implementation Plan

> **For agentic workers:** Execute inline with `executing-plans` in the existing isolated feature worktree. Preserve all source and evidence. Do not spawn agents, make development commits, merge, push, deploy, install software, change settings/services or delete files. The independent evidence Git snapshot is only a verification identity. Fixed remote capability tests were explicitly authorized on 2026-10-30; their source and reports remain separate from prior runs.

**Goal:** Persist a postcheck's terminal evidence and its unique reconciliation audit atomically, and recover historical terminal descriptors with a missing audit without replaying a command.

**Architecture:** Keep the original application derivation, fixed helper actions, DTOs and CommandRepository. The two existing repository completion methods share one bounded SQLite transaction; the existing explicit reconciliation method uses the same idempotent audit writer. Terminal refresh uses saved evidence, and complete terminal reads avoid write transactions. Original command, Run, Workflow, binding and reservation state remain immutable.

**Tech Stack:** Python 3.10+, Flask, SQLite/WAL and pytest already installed. No new dependency, table, worker, gate, API request field or domain state.

## Global constraints

- Authority: [capability postcheck §6.4](../specs/2026-10-20-artifact-capability-integration-contract.md#64-hub-固定能力-run-与-command-派发子集), [Workflow reconciliation §7](../specs/2026-10-20-workflow-execution-contract.md#7-版本变更重试和恢复) and [original platform M1.9/M1.10](../../platform/agent-fleet-self-hosted-platform.md#m19-deterministic-workspace-post-check).
- The independent digest helper remains predeclared and `read_only`; the original write remains `reconcile_before_retry`. This persistence fix grants no capability, retries no write, selects no new target, and releases no held/isolated reservation.
- Cover the single reproduced root cause in `workspace_digest`, `service_inspect` and `service_logs`. Result/source/outcome meanings stay exactly as documented.
- Results remain at most 16 KiB UTF-8 JSON. Exact indexed descriptor/audit lookups are bounded; fixed type dispatch uses a dictionary. There is no scan, unbounded loop or hot-path global lock.
- Use one explicit `BEGIN IMMEDIATE` for pending descriptor update and audit insert. Idempotent audit reuse compares the full stable audit payload, including owner, command, actor, outcome, source and evidence reference. A collision must roll back the descriptor.
- Terminal recovery trusts saved descriptor state/result/id, never current service configuration or a stale competing caller's proposed outcome. Saved terminal rows/timestamps are not rewritten. A complete terminal read must perform no insert/update or write transaction.
- Preserve the SQLite exception as CommandRepositoryError cause and that error as ApplicationError cause. Connection context managers own rollback/close; do not suppress rollback errors or open a replacement connection in an integrity-error handler.
- Existing service-health observations remain the original independent evidence path. This task changes only descriptor/audit completion and its error chain.
- UTF-8 without BOM. Use fresh private evidence/basetemp for every run. Prior frozen results remain historical; this task cannot claim a new full release gate or remote platform acceptance.

## Reproduced baseline

Evidence root: `/Users/mango/.cache/agent-fleet/verification/postcheck-atomic-20261030-zms34vh4`.

The complete external regression `red/test_postcheck_audit_gap.py` uses original Flask requests, Command claims/receipts and a SQLite BEFORE INSERT trigger. Python 3.10 produced **3 failed**: each HTTP query returned 503 while its descriptor was already `matched` and no audit existed. After disabling the retained fault switch, another HTTP query returned 200 but the audit count was still zero. The original command remained unchanged. `red/results.xml`, stdout/stderr, exact command, three fault observations and `root-cause.json` are retained.

The cause is the application's separate `finish_*_postcheck()` commit followed by `record_reconciliation()` commit, plus an early return for a terminal descriptor. The correct fix layer is the original SQLite repository transaction.

## Task 1: Persist the failing and recovery tests

**File:** Create `tests/test_postcheck_atomic.py`.

**Interfaces:** A typed local preparation helper returns original Flask/CommandRepository/service objects, descriptor identity and fixed succeeded/failed receipt. Tests exercise original HTTP/application entries; they never install or invoke external software.

- [x] Cover audit INSERT failure for workspace matched/mismatch/read-failed and both service kinds' matched/read-failed outcomes. Assert the descriptor stays pending, the audit count is zero, the original command is identical and the typed exception retains the SQLite cause. Disable the test fault switch and recreate the application service; its retry must finish with exactly one audit and no new command.
- [x] Block descriptor UPDATE after the completion starts; assert all descriptor/audit changes roll back and retry succeeds.
- [x] Create a historical terminal descriptor with its canonical evidence id but no audit. Query through a fresh service, prove one matching audit is repaired from saved evidence, descriptor/result/timestamps remain byte-identical, and no helper is queued or executed.
- [x] Cover an existing mismatched audit id, a stale competing completion proposal, four concurrent refreshes and terminal reads with SQLite trace assertions proving no write transaction.
- [x] Run the complete new file before product changes and preserve the independent red report:

```bash
python -m pytest tests/test_postcheck_atomic.py -q --tb=short --junitxml=/fresh/evidence/red.xml --basetemp=/fresh/evidence/fixtures
```

Expected: audit-failure/legacy-recovery/cause-chain cases fail against the reproduced implementation; ordinary controls can already pass. Never relax assertions to obtain green.

## Task 2: One bounded completion transaction

**File:** Modify `hub/infrastructure/command_repository.py`.

**Interfaces:** Preserve public `record_reconciliation(...)`, `finish_postcheck(...)` and `finish_service_postcheck(...)` signatures. Add a typed private `_save_reconciliation(connection, values, created)` used inside the caller's transaction and a typed private `_finish_postcheck(...)` shared by the existing completion methods. Fixed storage metadata selects only the two original tables and their explicit projected columns.

- [x] Replace the duplicate-insert exception/reconnect path with this same-connection audit writer:

```python
@staticmethod
def _save_reconciliation(connection: sqlite3.Connection, values: dict[str, str], created: float) -> dict[str, Any]:
    prior = connection.execute(
        "SELECT reconciliation_id,command_id,owner_id,actor,outcome,evidence_source,evidence_reference,created_at "
        "FROM platform_command_reconciliations WHERE reconciliation_id=?", (values["reconciliation_id"],),
    ).fetchone()
    if prior is not None:
        if any(prior[key] != value for key, value in values.items()):
            raise CommandRepositoryError("reconciliation_conflict")
        return dict(prior)
    connection.execute(
        "INSERT INTO platform_command_reconciliations(reconciliation_id,command_id,owner_id,actor,outcome,"
        "evidence_source,evidence_reference,created_at) VALUES(?,?,?,?,?,?,?,?)",
        tuple(values[key] for key in ("reconciliation_id", "command_id", "owner_id", "actor", "outcome",
            "evidence_source", "evidence_reference")) + (created,),
    )
    return values | {"created_at": created}
```

- [x] The explicit reconciliation method opens/closes one transaction, rechecks the owner-scoped original unknown command, validates its injected finite timestamp and calls the writer. Translate database/type/value errors with `raise CommandRepositoryError("command_store") from exc`.
- [x] Pending completion reads the exact descriptor and original unknown command under `BEGIN IMMEDIATE`, validates the canonical `postcheck-<check_command_id>` evidence id, serializes bounded JSON with `allow_nan=False`, updates only the pending descriptor and calls the writer before commit. The audit outcome is `confirmed_succeeded` for saved `matched`, otherwise `remains_unknown`; its source is the existing kind's workspace/service/log source.
- [x] A terminal completion proposal returns the saved descriptor/outcome. If its matching audit exists, use an indexed read path with no transaction/write. If missing, re-read the descriptor in one transaction and append only its saved audit. Reject a mismatched prior audit, invalid state/id, absent original or an original that ceased to be unknown.
- [x] Retain current bound result limit and original schema. Both compatibility completion methods validate their supported kind before sharing the same implementation.

## Task 3: Application completion and exception chain

**File:** Modify `hub/application/command_postcheck_service.py`.

**Interfaces:** Original POST/GET routes and public DTOs remain unchanged. `_refresh`, `_refresh_service` and `_refresh_service_logs` return typed dictionaries and use original completion methods once; they do not separately append audit.

- [x] Remove all three post-completion `record_reconciliation()` calls and the now-unused local audit outcome variable. Keep original observed-result and service-health derivation unchanged.
- [x] Replace each terminal early return with the same repository completion method using saved `state`, `result` and `evidence_id`. This exact workspace branch also applies to the corresponding original service completion method and kind:

```python
if row.get("state") != "pending":
    return self.repository.finish_postcheck(
        owner_id, row["command_id"], kind=KIND, state=row["state"],
        result=row.get("result"), evidence_id=row.get("evidence_id"), now=float(self.clock()),
    )
```

- [x] Preserve original exception causes at every application catch wrapping these completion calls with `raise self._translate(exc) from exc`. Existing stable public error messages remain redacted.

## Task 4: Verification and evidence scope

**Files:** Update this plan, the capability plan/contract, original platform postcheck description, project progress and testing entries. Register the new file's exact selectors in applicable original journeys without changing external `not_run` checks or their acceptance state.

- [x] Rerun the original external three-case regression fresh. It must become **3 passed** without changing the external repro file.
- [x] Run the complete new file plus original workspace/service postchecks, Command/reconciliation and capability HTTP/dispatch/Node regressions independently on Python 3.10 and 3.13. Compile the changed Python files in each runtime. Retain exact commands, source hashes, stdout/stderr/JUnit and zero-failure/error/skip counts.
- [x] Re-run the six-case actual local capability fixture only because this task changes its persistence path. Preserve separate counts for each Python version and existing state/reservation/one-write/one-digest/zero-restart-I/O assertions.
- [x] Verify UTF-8/no BOM, complete annotations for added functions, local Markdown links/anchors and `git diff --check`. Self-review rollback, collision, concurrency, historical recovery and terminal read-lock behavior against the documented invariants.
- [x] Preserve revision `3055506b8162c6dae99681728438341c2e57ed5e` as historical. Freeze a new independently hashed source candidate after all authorized local checks. The fixed two-host capability test is explicitly authorized; use fresh private remote roots and installed runtimes, retain exact source/report hashes and run only the documented fixture. Revision `16f10ebd630d38fa53a839ce9d2c432719014120` passes the original twelve-case native matrix; source/report audits are recorded in the [capability plan](2026-10-30-capability-platform-postcheck.md#native-frozen-verification).

This plan completes one persisted evidence bug. Full WF-05, NODE-LIVE, production isolation and external integrations remain open; all product gates remain default false.

## Verified local result — 2026-10-30

The original application and repository now complete descriptor/audit in one transaction, reuse the full saved audit payload, repair a missing historical audit from saved terminal evidence and keep complete terminal reads free of write transactions. All three postcheck kinds retain their documented result semantics and original unknown Command/Run; capability binding and reservation control are unchanged. SQLite errors retain their cause through both repository and application.

- The in-repo red baseline contains **36 failed / 12 passed / 0 errors** across 48 cases. The first green run is **48 passed / 0 failures/errors/skips**; exact reports are `inrepo-red/` and `green-py310/` under the evidence root above.
- The unchanged original external repro becomes **3 passed / 0 failures/errors/skips** in fresh `external-green/`; its file was not edited.
- Python 3.10.21 and Python 3.13.15 each independently run the eight related modules: **279 passed / 0 failures/errors/skips**. Separate original local capability runs each produce **6 passed / 0 failures/errors/skips**, preserving one write, one digest, zero restart I/O and unchanged original snapshots. Each compileall exits 0 and before/after executable source hashes match.
- Full commands/logs/JUnit/source maps are retained in `verified-py310/verification-summary.json` and `verified-py313/verification-summary.json`. Independent counts are never added into a release total. These local runs do not replace the previous export full gate or a new all-repository gate.

The fixed two-machine capability test was authorized after local regression registration. Its later fresh full-source freeze and original native command complete independently, as recorded below; the former `3055506b...` candidate remains historical.

## Registration verification — 2026-10-30

The eight new selectors are registered in the original NODE-01 and WF-08 journeys. Additional release-evidence and passive Workflow-gate checks complete independently on Python 3.10.21 and 3.13.15: **121 passed / 0 failures/errors/skips** per runtime. The original Python 3.10 check had exited with SIGTERM after 58 dots and no traceback; it remains an incomplete historical run, and its signal origin is undetermined. A fresh verbose bounded diagnosis completes all 121 cases in 7.90 seconds without source changes; Python 3.13 separately completes the same set. Exact commands, JUnit and unchanged source hashes are retained in `registration-diagnosis-_j14w64m/` and `registration-py313-12qrcyck/` under the evidence root. No source fix was inferred from the unexplained signal.

## Native follow-through — 2026-10-31

The [original fixed Windows/Linux matrix](2026-10-30-capability-platform-postcheck.md#native-frozen-verification) completes **12 passed / 0 failures/errors/skips** on source `16f10ebd630d38fa53a839ce9d2c432719014120`, including this atomic repository/application fix and its registered regressions. All 895 source digests match before/after on both hosts and the controller. Each native journal and each read-only Hub audit independently confirms the original unknown state, unchanged binding/generation/reservation and one canonical audit. Native evidence and 64 verified copied files are retained under `/Users/mango/.cache/agent-fleet/verification/capability-platform-20261030-edwzm7df/`. This result remains separate from local regression counts and prior all-repository gates; full Workflow and external acceptance are still open.

## Terminal original-command guard follow-through — 2026-10-31

The [subsequent guard plan](2026-10-31-postcheck-terminal-guard.md) closes a separately reproduced owner/unknown omission in complete-audit reads. The reused original guard preserves this plan's transaction and recovery behavior. Its local Python 3.10/3.13 runs independently contain 291 related tests, six original capability cases and 121 release/passive-gate checks, with zero failures/errors/skips and compileall exit 0. The new twelve-case selector is registered in NODE-01/WF-08.

The [5833-pass full run](2026-10-31-postcheck-full-gate.md#verified-pre-guard-baseline--2026-10-31) predates that guard and has explicit pre-guard scope. The corrected revision `85488105cd2d3b610fcbfa18fc2273bd02618db1`, 898 files, independently passes the original native twelve-case matrix and, after authorized capacity recovery, the [original full gate](2026-10-31-postcheck-full-gate.md#verified-corrected-source-rerun---2026-10-31): **5845 passed / 197 subtests / 0 failures/errors/skips**, 42 journeys, 44 PNGs and both original release verifiers passing. No old counts or revisions are reinterpreted or combined; complete Workflow and external acceptance remain open.
