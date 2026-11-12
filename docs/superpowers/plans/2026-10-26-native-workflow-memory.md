# Native Workflow Memory Implementation Plan

> **For agentic workers:** Use executing-plans inline, preserve failing evidence and run fresh verification before checking each task. No commit, push, merge, deployment or live external calls.

**Goal:** Deliver complete explicitly selected Memory values to the existing local Native Workflow Run while preserving original execution identity and current authorization.

**Architecture:** Reuse owner-scoped Memory, Run config, original transaction, worker and Workflow state machine. A pure immutable snapshot is persisted atomically with the Run; an injected storage guard validates binding/lease and current Memory before the worker uses the snapshot. No second queue or execution engine.

**Tech Stack:** Python 3.10+, Flask, SQLite, stdlib and existing pytest/Chrome tooling; no new dependencies.

## Global Constraints

- Authority: [execution contract §5.3](../specs/2026-10-20-workflow-execution-contract.md#53-native-run-的显式完整-memory), original Memory contract §7.12, existing roadmap.
- Local native_run only. Summary, CLI, fixed test/review, remote/capability context and external structured output remain open; its first handoff now uses existing Hub ArtifactRef under integration contract §7.13.
- At most 16 selected logical references; full canonical Memory items total ≤8192 bytes. Original digest envelope and final token-bearing Run config must fit the existing 65536-byte JSON bound or reject context_too_large before persistence. Snapshot and reference-only system message each ≤32768 UTF-8 bytes. No truncation, search or latest selection.
- Original token/payload/generation, exact retry, unknown and held/isolated reservations remain unchanged. All gates stay default-off.
- Workflow schema 1/current reader 7; upgrade only in the original native-Memory Run creation transaction. Legacy contracts, config and prompts unchanged.
- UTF-8 without BOM, preserve all existing work, no primary-worktree or Obsidian changes.

## Task 1: Pure snapshot and failing end-to-end evidence

**Files:** Create `hub/domain/workflow_native_memory.py`, `tests/test_workflow_native_memory.py`; modify only the explicit future-reader test in `tests/test_external_agent_memory_inputs.py` when reader 7 is implemented.

**Interfaces:** `build_native_memory(resolved, items) -> NativeWorkflowMemory`; `parse_native_memory(value, resolved) -> NativeWorkflowMemory`; immutable snapshot and `.message` use exact schema in §5.3. `WorkflowNativeMemoryError(code)` retains stable errors and original causes across I/O boundary.

- [x] Write tests before runtime implementation: canonical complete Unicode and ordered duplicate refs; private repr; exact fields/types/digest/reference matching; 8192/32768 bounds; no-context old behavior; real Native Run/Memory/SQLite/worker provider-capture and zero legacy Tasks.
- [x] Run the new tests with existing task-local Python, retain failing JUnit under `/tmp/agent-fleet-native-memory-red-20261026.xml`.
- [x] Implement pure snapshot validation and rendering using `WorkflowMemoryItem`, `memory_selection`, `canonical_json` and exact `parse_resolved`; O(n+B), no framework/time/random/I/O.

## Task 2: Atomic creation, original lookup and current authorization

**Files:** Modify `hub/infrastructure/workflow_memory_repository.py`, `workflow_run_executor.py`, `workflow_input_resolver.py`, `workflow_schema.py`; create `hub/infrastructure/workflow_native_memory_repository.py`; modify `hub/application/run_worker_service.py`, `hub/infrastructure/workflow_bootstrap.py`.

**Interfaces:** Concrete `WorkflowMemoryRepository.read_selected(..., connection=None)` reuses caller transaction; external callers keep the original two-argument contract. `NativeWorkflowMemoryGuard.prepare(claim, now=...) -> NativeWorkflowMemory | None` is injected into the original worker. Stored Run lookup parses original `workflow_memory` and recomputes the original dispatch config digest without rereading mutable Memory. Guard reads current values once before provider creation.

- [x] Extend existing owner/ID Memory read to caller connection, preserving bounded projection, strict persisted values and one batch query.
- [x] Allow resolver only external_agent/local native candidate Memory; other context kinds/consumers still reject. The actual executor remains authoritative for local vs remote configuration.
- [x] Run adapter accepts only explicit Memory for plain local Native targets; rejects injected `workflow_memory`, mixed old memory_context, remote/capability/legacy/review/test inputs. Persist snapshot and reader 7 in original message/Run/link transaction; rollbacks undo all writes.
- [x] Original lookup/inspect validate saved private snapshot and config/message without rereading live Memory. Missing/corrupt snapshots and low reader refuse recovery; same token/config still returns one original Run after edit/deletion.
- [x] Worker guard verifies current lease/binding/generation/deadline/control/workspace and current Memory; binding_not_ready uses existing requeue branch, all other failures call no provider. Inject full reference-only message into existing worker; DTO/events contain no private Memory.
- [x] Cover real owner mismatch, edit/disable/delete, same-revision tampering, malformed persisted types, duplicate revision conflicts, gate-off/no-provider, rollback at Run creation and reader update, queued revocation, missing guard, forged claim, binding race and worker restart.

## Task 3: Historical closure, integration and final verification

**Files:** Modify `hub/infrastructure/workflow_repository.py`, `hub/infrastructure/platform_backup.py`, `docs/testing/journeys.json` and current Workflow development/testing entries.

**Interfaces:** `validate_native_memory_runs(connection)` scans dispatch history in ≤128 rowid-keyset pages, first page without a lower bound, validates complete original snapshot independent of current Memory or runtime gates. No history scan per request.

- [x] Validate complete startup/backup history, original binding/message/config/current reader and marker presence, including negative/zero rowid and corrupted last page. Preserve frozen plaintext/encrypted restores and original unknown/held.
- [x] Exercise real bootstrap/coordinator/resolver/worker and HTTP DTO redaction; register actual new required selectors only after passing.
- [x] Run focused green and serial Python 3.10/3.13 related suites, compileall/AST/JS/UTF-8/BOM/local-link/diff checks. No static type checker is configured; compilation is not typing proof.
- [x] Freeze sources for complete strict journey run; retain actual exit, skips, source digests and seven not_run external checks. Do not interrupt or mutate frozen sources while running.
- [x] Record fresh evidence and remaining full-platform requirements; post-document source comparison must show runtime/tests/matrix unchanged after the gate.

## Verification commands

```bash
PYTHONPATH=. /tmp/agent-fleet-muse-identity-py313-20261026/bin/python -m pytest tests/test_workflow_native_memory.py -q --tb=short --junitxml=/tmp/agent-fleet-native-memory-red-20261026.xml
PYTHONPATH=. /tmp/agent-fleet-muse-identity-py313-20261026/bin/python -m pytest tests/test_workflow_native_memory.py tests/test_workflow_dispatch.py tests/test_external_agent_memory_inputs.py tests/test_workflow_input_resolver.py tests/test_platform_run_worker.py tests/test_workflow_repository.py tests/test_platform_backup.py tests/test_platform_backup_encrypted.py -q --tb=short -rs
```

Strict full run uses the documented `--require-journeys`, existing Chrome/Playwright, JUnit/journey report and before/after Git-source hashes. A macOS platform skip keeps the strict gate failed; it is never rewritten as passing Linux/release evidence.

## Verification checkpoint (2026-10-26)

Native Memory: 20 selectors / 52 cases pass in `/tmp/agent-fleet-native-memory-focused-verified-20261026.xml`. Serial Python 3.10.21 and 3.13.15 each pass 821 cases across 26 related modules, zero skips; JUnit `/tmp/agent-fleet-native-memory-related-verified-python310-20261026.xml` and `...-python313-20261026.xml`. Full strict gate is pending. Preserve original red, history, marker-injection and envelope-budget failures; no weakening of assertions or release-gate semantics.

Latest frozen gate: 5346 passed, 5 macOS platform skips, 195 passing subtests, zero failures/errors; WF-06 327/759; 41/42 journeys. Strict exit 1 remains correct for Linux RELEASE-01; seven external checks not_run. Serial related Python 3.10/3.13 each 886 passed; focused 52 passed. Evidence and remaining requirements are recorded in implementation-progress.md.
