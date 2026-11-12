# Persistent MCP Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification checkpoints.

**Goal:** Persist administrator registration, admitted/pending discovery and revocation across Hub processes without granting queued Runs new tools.

**Architecture:** One immutable record and one SQLite I/O port extend the existing ConnectorCatalog. Discovery/network I/O stays outside transactions; epoch CAS serializes publication and approval. Runtime adapters remain explicitly attached process dependencies. Existing ToolBroker and Run pins remain the only execution path.

**Tech Stack:** Python 3.10+, existing dataclasses/json/hashlib/sqlite3/jsonschema, Flask bootstrap and pytest; no new dependencies.

## Global Constraints

- SSOT: integration contract §7.1, §7.5 and §7.8.
- All gates remain default-off; no live nodes/model/MCP/Muse calls, commit, deployment or Obsidian writes.
- Work only in the existing isolated checkout; preserve all existing work; UTF-8 without BOM; no deletions.
- ≤128 connector registrations including revoked history; ≤20 exposed tools; original schema/transport limits unchanged.
- Canonical private snapshots contain SecretRef only, no resolved secrets or runtime transport authority.
- Platform reader 4 is required only once catalog tables exist; Workflow tables are not required.

## Task 1: Exact discovery identity and isolation

**Files:** `tools/platform/connectors/mcp.py`, `tools/platform/connectors/snapshots.py`, `tests/test_mcp_catalog_persistence.py`.

**Interfaces:** `encode_config(config) -> bytes`, `decode_config(raw) -> MCPConnectorConfig`, `encode_discovery(discovery) -> bytes`, `decode_discovery(raw, config) -> MCPDiscovery`, `discovery_digest(discovery) -> str`.

- [x] Add regressions proving descriptions/server/capabilities/annotations require a new revision, order does not, and returned snapshots cannot mutate admitted definitions.
- [x] Run these regressions before implementation and retain JUnit evidence.
- [x] Implement bounded strict canonical codecs using existing validators; compare complete discovery identity and preserve original pin format.

```bash
PYTHONPATH=. python3 -m pytest tests/test_mcp_catalog_persistence.py tests/test_mcp_connector.py -q
```

## Task 2: Persistent state and short transaction CAS

**Files:** `hub/domain/mcp_catalog.py`, `hub/application/connector_catalog.py`, `hub/infrastructure/mcp_catalog_schema.py`, `hub/infrastructure/mcp_catalog_repository.py`, same test module.

**Interfaces:** `MCPCatalogRecord`, `MCPCatalogStore.register/get_many/publish/admit/revoke`; `ConnectorCatalog(enabled=False, store=None)`; `revoke(connector_id, expected_digest=...)`.

- [x] Test restart with no rediscovery, idempotent/conflicting registration, cross-process pending/admission/revocation, exact revision fencing, interrupted/outdated refresh and registration bound.
- [x] Implement schema 1, immutable records, row validation, one bounded query per read snapshot and epoch CAS. Retain first revocation, refuse disabled/revoked mutations and ignore seed discovery in durable registration.
- [x] Synchronize durable entries before pins/definitions/calls. Build independent connector snapshots; no lock or database transaction spans network I/O.

```bash
PYTHONPATH=. python3 -m pytest tests/test_mcp_catalog_persistence.py tests/test_mcp_connector.py tests/test_mcp_run_pinning.py -q
```

## Task 3: Passive bootstrap and backup/restore

**Files:** `hub/bootstrap.py`, `hub/infrastructure/platform_db.py`, `hub/infrastructure/workflow_repository.py`, `hub/infrastructure/platform_backup.py`, same test module.

- [x] Test gate-off has no directory tables; gate-on with Workflow off initializes durable catalog without network; new process can attach original adapter without discovery; future readers fail closed.
- [x] Bootstrap durable catalog from trusted `mcp_connectors` adapters when no explicit catalog injection is supplied, sharing it with original ConversationService and worker.
- [x] Extend platform reader recognition, gate-independent backup history checks and restored connector disabling for both plain/encrypted restore, including no-Workflow databases.
- [x] Test corruption beyond first keyset page, nonpositive rowids, partial schema, source preservation and nonreactivation by startup registration.

```bash
PYTHONPATH=. python3 -m pytest tests/test_mcp_catalog_persistence.py tests/test_mcp_http.py tests/test_platform_backup.py tests/test_workflow_gates.py -q
```

## Task 4: Acceptance evidence

**Files:** journey matrix, workflow progress/navigation, acceptance and roadmap.

- [x] Run related tests on Python 3.10.21 and 3.13.7, compileall and diff checks; repair all new failures and preserve red evidence.
- [x] Register new selectors under WF-06; run browser-enabled full gate and independently verify JUnit/journeys/source digests.
- [x] Record actual limits and remaining full-platform gaps. Do not claim external pilots, Linux/Windows evidence or complete platform readiness.

## Final verification and limits

The 13-module related set passed **428 tests** on Python 3.10.21 (20.969 s) and Python 3.13.7 (20.065 s), without skips. JUnit files: `/tmp/agent-fleet-mcp-catalog-final-verified-python310.xml` and `...-python313.xml`.

The final browser-enabled full gate has **4971 passed、0 failures/errors、5 skipped、195 subtests passed**; WF-06 has **188 selectors / 396 cases passed**, including this slice's 34 selectors / 65 cases. **41/42 journeys passed**, strict exit 1 / ci_status=failed remains for required Linux RELEASE-01 evidence; all seven external checks remain not_run. Evidence: `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-mcp-catalog-full-20261025-pi2sm2o7/`. All 825 Git source digests were unchanged; matrix SHA256 `ce1ea411ba30e7ef3c76b92f57effe25956bfc25680c87101081688962ec4efa`.

Original failing evidence is retained: `/tmp/agent-fleet-mcp-catalog-before.xml` (seven failures), `...-review-before.xml` (high-water corruption accepted), and `...-related-python313.xml` (423 passed / two coexistence failures before Workflow learned the independent platform reader). Bootstrap's intermediate missing time import and the corrected missing-adapter error expectation remain in the development JUnit history. The repository-only final sets above and full gate are the current evidence.

No static type checker is configured. Python compileall/AST, frontend syntax, UTF-8 no-BOM, local Markdown links and diff checks are separate evidence. The local catalog plan is complete; configuration UI, external Agent input/output wiring, Linux/Windows, approved real nodes/models/CLI/isolation and MCP/Muse pilots remain open. All gates remain default-off; no commit, push, merge, deployment, downloads or Obsidian edits.
