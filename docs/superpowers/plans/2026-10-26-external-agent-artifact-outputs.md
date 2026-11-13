# External Agent ArtifactRef Outputs Implementation Plan

> **For agentic workers:** Use executing-plans inline. Preserve red evidence, implement complete files and run fresh checks. No commit, push, merge, deploy or live external calls.

**Goal:** Export and bind complete published Hub ArtifactRefs from the original external Agent task into the existing Workflow output/validation/acceptance chain.

**Architecture:** Pure immutable export DTO; bounded artifact I/O verifier; original external executor returns private export evidence, and original apply_observation transaction commits it with fenced state/output references. No new queue, download channel, worker or empty internal façade.

**Tech Stack:** Python 3.10+, Flask/SQLite, stdlib, existing jsonschema and pytest/Chrome environments.

## Global Constraints

- Authority: integration contract §7.13.1 and execution contract; original task/token/payload/generation/unknown/held/isolated/exact retry unchanged.
- Explicit export capability only; Muse remains unsupported; max16 outputs, 256 MiB distinct total, artifact max_bytes, structured/descriptor 64 KiB, source bundle10 MiB. Verified owned descriptor, complete streamed bytes, never truncate.
- Workflow reader8 barrier in original output admission; external schema3 unchanged; old output-free bytes unchanged; observation snapshot ≤16384 bytes.
- Only artifact/structured/code_revision contract kinds; schema validation does not fabricate validation evidence. No external schema fetch.
- UTF-8 without BOM; preserve primary tree and all unrelated work. No new dependencies.

## Task 1: Immutable export contract and failing original-chain evidence

**Files:** Create hub/domain/external_outputs.py and tests/test_external_agent_outputs.py; update hub/domain/external_agent.py, external_request.py, workflow_external.py, workflow_execution.py.

**Interfaces:** ExternalArtifactOutput(output_key, artifact); ExternalOutputSet(workflow_id, step_key, generation, task, outputs) with canonical snapshot; parse_external_outputs(raw, resolved, task) validates exact immutable fields. ExecutionObservation has optional private external_outputs. Existing adapter export receives original task/specs and returns ExternalOutputSet.

- [x] Write red tests for strict fields/types/identity/required and optional sets/duplicate IDs/limits, original SQLite admission and adapter invocation, tampered manifests/bytes, repeated restart, rollback and no-output compatibility.
- [x] Run pytest tests/test_external_agent_outputs.py -q --tb=short with task Python and retain JUnit red evidence.
- [x] Implement exact pure parsing/canonicalization/subject calculation; dictionaries for bounded O(n) matching, no I/O/time/random.

## Task 2: Complete file validation through original owned I/O

**Files:** Create hub/application/workflow_external_outputs.py; update tools/platform/workflow_source.py, workflow_external_execution.py and workflow_bootstrap.py.

**Interfaces:** ExternalOutputArtifacts.get/open_verified; WorkflowExternalOutputs.verify(resolved, task, result) returns verified immutable output set; stream source verification reuses original manifest/tree/blob rules. Store authorizes current output read before adapter export. Executor recovers persisted exports before calling adapter.

- [x] Implement full streamed hash/length verification, size budgets, exact scoped metadata and descriptor lifecycle; deduplicate byte reads by ID without hiding conflict.
- [x] Validate structured JSON with existing Draft202012Validator and frozen schema; code descriptor exact F3 format and stream bundle validation, no executable or network.
- [x] Wire original executor success observation; adapter errors retain causes/unknown; cancellation owns and joins bounded I/O. Muse remains unsupported.

## Task 3: Atomic output/state binding and complete historical closure

**Files:** Update hub/infrastructure/workflow_external_repository.py, workflow_repository.py, workflow_schema.py, platform_backup.py and original reconciliation call if needed.

**Interfaces:** ExternalWorkflowRepository.authorize_outputs(binding,target,now); commit_external_outputs(connection, claim, record, observation, now) is called only in original apply_observation transaction. Existing observation_snapshot stores canonical export evidence. All file I/O remains outside transactions.

- [x] Upgrade reader8 atomically only for output admission; enforce target capability and old reader closure without changing external schema3/legacy snapshots.
- [x] Recheck exact current original binding/task/claim/generation/workspace/control/deadline, then commit output refs/subject/observation and state/reservations atomically. Same output set stable; conflicts fail closed; no independent evidence.
- [x] Verify history via joined signed rowid keyset pages ≤128; batch output-reference closure, original subject/observations, no N+1 scan. Gate-off backup and frozen plain/encrypted restore include all descriptor/source bundle refs.
- [x] Cover real transactions/triggers, two processes/restarts, all crash windows, cancel/frozen/deadline/owner/generation races, partial sets and corrupt final history page. Unknown requires explicit reconcile and no resubmit.

## Task 4: Register verified scope and fresh complete gate

**Files:** Update docs/testing/journeys.json, development/testing entries and only future-reader assumptions in existing tests.

- [x] Run focused suite then serial related Python3.10/3.13 checks, compileall/AST/JS/UTF-8/BOM/link/diff checks. Preserve original red evidence and reject assertions weakened for green.
- [x] Register exact passing selectors in WF-06/08 as applicable; document real implemented scope and remaining platform/live requirements.
- [x] Freeze sources and run full --require-journeys with JUnit/report/before-after source digests; keep strict platform skips failed and external checks not_run.

Final evidence: 47 new selectors / 178 cases; serial Python3.10.21/3.13.15 related sets each have1153 passing cases without skips. Full frozen run has5524 passed, zero failures/errors, five macOS platform skips and195 passing subtests. WF-06 is374/937 and WF-08 is179/380, both passing; strict exit1/failed remains because RELEASE-01 requires three Linux pidfd/proc cases. Seven external checks remain not_run. All866 frozen source digests are unchanged; matrix SHA-256 is `3cc55faee99ee24cf41f62d2cf64d55924809517f87de286070fc3621f32a3a6`. Evidence directory: `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-external-outputs-full-20261027-ilpb41wj`. This completes the local output slice and its evidence registration, while whole-platform release and live acceptance remain open.
