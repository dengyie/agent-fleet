# External Agent Explicit Memory Inputs Implementation Plan

> Execute inline in the existing isolated checkout. No new agents, commits, publication, deployment, downloads or real integration calls.

**Goal:** Deliver complete explicitly selected Memory revisions to the original external task, preserving exact requests across restart and enforcing current send authorization.

**SSOT:** [integration contract §7.12](../specs/2026-10-20-artifact-capability-integration-contract.md#712-外部-agent-的显式-memory-输入与版本权威), retaining §7.6/7.11 and the original Workflow execution contract.

**Architecture/stack:** Pure immutable Memory/request values; one bounded SQLite Memory I/O adapter shared by the existing input resolver and external collector; original Workflow/ExternalStepExecutor/send transaction/journal/backup. Python 3.10+, stdlib, existing SQLite/Flask/pytest. No extra scheduler, facade, search, truncating renderer or dependency.

## Decisions and file map

- WorkContract 1 stays byte compatible. WorkContract 2 adds memory revision 0, with explicit nested version and Workflow reader 6 before persistence. Definition/resolved outer schema 1 remains stable.
- External schema/reader 3 migrates 1/2 in one transaction, retaining all original facts. Gate-off accepts legacy schemas; Memory requires 3.
- At most 16 distinct owner-scoped selected IDs in one read snapshot, dictionary matching/deduplication. O(n+B), with shared logical Memory/text budget B≤8192 bytes, full prompt≤8192 characters, request≤65536 bytes.
- Only external_agent consumes Memory. Summary and Native/CLI/capability context remain unsupported; original artifacts/Node receipts/postcheck semantics remain intact.
- Domain: `workflow_memory.py`, `external_request.py`, `workflow_contract.py`, `workflow_contract_types.py`.
- Infrastructure: `workflow_memory_repository.py`, original input resolver, external schema/repository, Workflow schema/repository, backup and bootstrap.
- Application: original external inputs/executor, using a Memory store port only at I/O boundary.
- Tests: `test_external_agent_memory_inputs.py`, original contract/dispatch/resolver/external/persistence/identity/backup suites; WF-06 selectors and documentation links.

## Task 1: Contracts and failing evidence

- [x] Add deterministic tests before implementation: schema 1/2 revision semantics, complete canonical Memory/hash/ordering, mixed input and shared budgets, corruption and unsupported kinds.
- [x] Add real SQLite/Memory/Muse-journal tests for owner scope, revision changes, disable/delete, exact restart and first-send rollback; preserve red JUnit outside repository.
- [x] Implement frozen domain values and strict compatible parser/request mapping.

## Task 2: Durable authority and execution

- [x] Implement one bounded projected batch read; validate exact persisted types and fields, no N+1 or silent truncation. Resolver grants only actual supported external context.
- [x] Freeze schema 2 compatibility marker in creation transaction; check backup/restore marker consistency.
- [x] Atomically migrate external 1/2 to 3; preserve original text prompts/task identities and reject legacy Memory or missing started request.
- [x] Collect before original send intent; authorize current complete Memory before allowed sends; retain original prompt for lookup and never reread for bound-task recovery.
- [x] Wire the existing bootstrap; explicitly reject unimplemented Native context consumption.

## Task 3: Verification and evidence

- [x] Review owner/revision/hash/limits, raw persisted corruption, migration rollback, request privacy and unknown/held/isolated safety, preserving all failed evidence.
- [x] Run meaningful related suites serially on Python 3.10.21 and 3.13.7; fix every observed failure before claiming this slice passes.
- [x] Register new WF-06 selectors; run UTF-8/BOM, compileall/AST, JS syntax, local links and diff checks. No configured static type checker; do not call AST a type proof.
- [x] Freeze runtime/tests/matrix and execute full installed-browser strict journey gate. Preserve Linux/Windows/live missing evidence explicitly.
- [x] Update roadmap, acceptance and implementation progress from measured results; keep the whole platform goal active.

## Verification commands

```bash
PYTHONPATH=.:tests python3 -m pytest tests/test_external_agent_memory_inputs.py -q --tb=short
PYTHONPATH=.:tests python3 -m pytest tests/test_external_agent_text_inputs.py tests/test_external_agent_workflow.py tests/test_external_agent_persistence.py tests/test_workflow_contract.py tests/test_workflow_dispatch.py tests/test_workflow_input_resolver.py -q
python3 -m compileall -q hub tools deploy tests
git diff --check
```

All feature gates remain default-off. No test or fixture proves real MCP/Muse/model/node identity or production isolation. Native Memory, summary, binary/code bundles, structured external export, remaining configuration and external/platform acceptance stay open.

Final verification: serial Python 3.10.21/3.13.7 23-module sets each 784 passed, no skips. New module 31 selectors/84 cases; final full gate 5256 passed, 5 platform skips, 195 subtests, WF-06 302/681, strict exit 1 for Linux RELEASE-01, external not_run. Evidence `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-external-memory-full-20261025-dnxqbpck`. Red/intermediate/history evidence retained in implementation progress; whole platform remains active.
