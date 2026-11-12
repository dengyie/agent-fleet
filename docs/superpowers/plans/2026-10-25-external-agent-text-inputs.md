# External Agent Text Artifact Inputs Implementation Plan

> Execute inline in the existing isolated checkout. No additional agents, commits, publication, deployment or real integration calls.

**Goal:** Send complete, authorized small UTF-8 artifacts to the original external Agent task and preserve the exact request across uncertain sends and restart.

**SSOT:** [integration contract §7.11](../specs/2026-10-20-artifact-capability-integration-contract.md#711-外部-agent-文本文件输入与固定请求), preserving §7.6 and the original Workflow execution contract.

**Architecture:** A pure canonical request type, one concrete application input collector over an artifact I/O port, and one nullable private request column in the original external binding. The existing StepExecutor, transactions, reservations, journal identity, feature gates and transport cancellation remain authoritative.

**Stack:** Python 3.10+, stdlib dataclasses/hashlib/json, existing Flask/SQLite/ArtifactStore/pytest. No new dependencies or downloads.

## Decisions and bounds

- Each/total logical artifact input ≤8192 bytes; only text/plain, text/markdown, application/json; strict UTF-8, complete content. Full prompt ≤8192 characters, private snapshot ≤65536 bytes.
- Bounded dictionary deduplication: O(n+B) time/O(n+B) memory; n≤16, B≤8192. Each distinct artifact uses one bounded verified descriptor and it always closes.
- Only the artifact and durable store boundaries use ports. No facade, second scheduler, automatic retry, new capability or implicit exporter.
- External schema/reader 2 with nullable request_snapshot; Workflow reader 5 only for that domain. Gate-off readers retain legacy schema 1; enabled initialization upgrades transactionally.
- Legacy no-input prompt stays exact. Original accepted task recovery does not reread inputs. Lookup uses the saved prompt; explicit permitted resend checks current metadata and original scope before external I/O.
- Memory/summary, code bundles, binary inputs, Node capability requirements, output export and live pilots remain open. All gates stay default-off.

## File map

| File | Responsibility |
|---|---|
| `hub/domain/external_request.py` | Immutable text input/request, canonical envelope, complete integrity and bounds validation |
| `hub/domain/workflow_external.py` | Allow only bounded concrete artifact inputs and carry private request pin |
| `hub/application/workflow_external_inputs.py` | Scoped bounded input collection, deduplication and current authorization port |
| `hub/infrastructure/workflow_external_schema.py`, `workflow_schema.py` | Independent legacy/new schema recognition and compatibility fence |
| `hub/infrastructure/workflow_external_repository.py` | Atomic migration, immutable request/send transaction and historical validation |
| `hub/application/workflow_external_execution.py`, `hub/infrastructure/workflow_bootstrap.py` | Original dispatch/recovery wiring using exact request and authorization |
| `hub/application/workflow_coordinator.py`, `hub/infrastructure/workflow_repository.py` | Separate external Hub text from existing Node receive requirements without weakening Node checks |
| `tests/test_external_agent_text_inputs.py`, existing external tests | Failing-first deterministic request, durable crash/restart/backup and coordinator evidence |
| `docs/testing/journeys.json`, workflow navigation/progress/acceptance | Required selectors and evidence-backed local status |

## Task 1: Failing evidence and pure request

- [x] Add failing deterministic contracts and real SQLite/ArtifactStore/Muse-journal scenarios before implementation; retain JUnit outside repo.
- [x] Validate exact envelope, ordering, complete bytes, hash, content type and all byte/character limits; retain old no-input prompt exactly.
- [x] Collect via scoped metadata and verified bounded descriptor; deduplicate and close resources even on errors, preserving causes.

## Task 2: Durable authority and recovery

- [x] Recognize schema 1/2 without gates; migrate only under enabled explicit init, atomically preserving old facts.
- [x] Add immutable request to the original send transaction. Reject mismatches, missing started text requests, future readers and corrupt history.
- [x] Wire input collector to StepExecutor; collect before send intent, use saved prompt for lookup/retry, check current metadata before allowed sends, never replay an original write blindly.
- [x] Verify transaction rollback, two repositories, lost receipt/restart, revocation, current gate/control/lease/budget and cancellation behavior.

## Task 3: Verification and evidence

- [x] Run new and related external/Workflow/artifact/backup/gate suites on Python 3.10.21 and 3.13.7, serially when packaging tests are included.
- [x] Review schema migration, secret/content exposure, bounds, descriptors and original task safety; fix all observed defects with retained evidence.
- [x] Register all new selectors in WF-06; run compileall, AST, UTF-8/BOM, links, JS syntax and diff checks. Report the absent static type checker honestly.
- [x] Freeze source and run the complete installed-browser strict journey gate; preserve Linux skips and external not_run.
- [x] Update this plan, implementation progress, parent roadmap and acceptance from actual results. Keep the whole platform open.

## Commands

```bash
PYTHONPATH=.:tests python3 -m pytest tests/test_external_agent_text_inputs.py -q --tb=short
PYTHONPATH=.:tests python3 -m pytest tests/test_external_agent_workflow.py tests/test_external_agent_persistence.py tests/test_external_agent_adapter.py -q
python3 -m compileall -q hub tools deploy tests
git diff --check
```

Verified local evidence: serial Python 3.10.21/3.13.7 twenty-module sets each 688 passed, no skips. Full installed-browser gate 5172 passed、0 failures/errors、5 skipped、195 subtests passed; WF-06 271 selectors / 597 cases, 41/42 journeys, strict exit 1 solely for Linux RELEASE-01. Seven external checks remain not_run. Frozen evidence: `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-external-text-input-full-20261025-65xjehc5`. Earlier red/intermediate/clock evidence is retained in implementation progress; whole-platform work remains open.
