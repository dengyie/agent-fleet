# Persistent Fleet Integration Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` inline in the existing isolated checkout. Preserve existing work; no commits or additional agents.

**Goal:** Persist the existing three-tool Fleet MCP identity and revocation across processes, with authorization checked inside the original Workflow transaction.

**Architecture:** Keep templates, budgets, scoped token namespaces and Workflow repositories. Use an immutable domain identity, one authentication I/O port and two additive SQLite tables. Startup injection becomes idempotent registration rather than live request authority. Restore disables imported credentials.

**Tech Stack:** Python 3.10+, Flask, SQLite WAL, existing pytest/HTTPS fixture; no new dependencies.

## Global constraints

- SSOT: [integration contract §7.7](../specs/2026-10-20-artifact-capability-integration-contract.md#77-fleet-集成身份持久化与撤销), [execution contract](../specs/2026-10-20-workflow-execution-contract.md), [F5 roadmap](2026-10-20-workflow-platform-roadmap.md).
- Work only in the existing `codex/workflow-platform-docs` checkout; UTF-8 without BOM; no deletion, download, deployment or real integration calls.
- `platform_fleet_mcp_enabled=false` remains the default; no additional management endpoint or tool scope.
- Registration is immutable; revocation and restore-disable cannot be reversed by startup injection.
- Token authentication is indexed, allowlists are bounded sets, backup scans use rowid keyset batches ≤128.

## Files and interfaces

| File | Responsibility |
|---|---|
| `hub/domain/integration_identity.py` | Frozen validation, canonical private snapshot/digest, scoped client token namespace; no I/O or clock |
| `hub/application/integration_identity.py` | Compatibility exports and `IntegrationIdentityStore.authenticate(token)` protocol |
| `hub/infrastructure/integration_identity_schema.py` | Independent schema v1, Workflow reader 4, gate-independent recognition |
| `hub/infrastructure/integration_identity_repository.py` | `init()`, `register(identity, now=...)`, `authenticate(token)`, `revoke(owner, integration_id, expected_digest=..., now=...)`, `require_active(connection, identity, scope=...)`, bounded backup validation |
| `hub/application/workflow_operator.py`, `hub/infrastructure/workflow_operator_repository.py` | Pass identity to original create/idempotent/detail transaction and recheck current authorization |
| `hub/infrastructure/workflow_bootstrap.py`, `hub/bootstrap.py`, `hub/http/fleet_mcp_routes.py` | Default-off persistent assembly and indexed authentication |
| `hub/infrastructure/workflow_schema.py`, `hub/infrastructure/platform_backup.py` | Gate-off recognition, complete snapshot validation and restored credential disable |
| `tests/test_fleet_mcp_boundary.py`, `tests/test_integration_identity.py`, `tests/test_mcp_http.py` | Domain, race, process, HTTP and backup evidence |

## Task 1: Deterministic identity and failing admission evidence

- [x] Add `test_revocation_during_create_authorization_blocks_new_workflow`: revoke during input authorization, assert HTTP 401 and unchanged Workflow count.
- [x] Run `PYTHONPATH=.:tests python3 -m pytest tests/test_fleet_mcp_boundary.py::test_revocation_during_create_authorization_blocks_new_workflow -q --junitxml=/tmp/agent-fleet-identity-before.xml` and retain red evidence.
- [x] Move identity into the domain as a frozen slotted dataclass; validate direct construction and register, bounded reference iterables, exact integers and token digests. Canonical snapshot excludes loaded revoked state, and parsing rejects unknown fields/noncanonical bytes.
- [x] Test malformed IDs/digests, bool/float coercion, iterable bounds, empty scopes, immutable state and stable namespaces. Preserve the existing application import.

## Task 2: Durable registration and atomic authorization

- [x] Add the two identity tables atomically. Persist owner/id, unique token digest, canonical snapshot/digest, registration time, nullable revocation time and restore-disable flag. Never persist bearer plaintext.
- [x] Implement exact idempotent registration, immutable conflicts, owner/digest-scoped irreversible revoke and one-row indexed authentication. Re-registering disabled credentials returns disabled state.
- [x] Pass `integration` through the original operator store create/detail signatures. In both prior-receipt and admission transactions verify current identity, scope, owner, namespace, template/workspace and total budget. Use owned I/O at async boundaries.
- [x] Under the existing gate, seed trusted registrations once and authenticate from the repository. Startup without injected identities still authenticates persistent active identities.
- [x] Test revocation before write/detail, stale identity snapshots, failed inserts rolling back, real process restart/revocation, cross-owner/token conflicts and absence of private metadata in DTOs.

## Task 3: Recovery and verified release evidence

- [x] Add independent reader 4 recognition while preserving base/capability/external readers; reject unknown/partial identity metadata with gates off.
- [x] Validate all historical identity rows in batches ≤128, including corrupt final batches. Plain/encrypted restore freezes Workflow and disables only restored credentials. Exact startup registration cannot reactivate them.
- [x] Reproduce and close runtime acceptance of a newer Workflow reader and the startup history scan's writer lock; authenticate/authorize against the complete schema and validate history in a WAL read transaction.
- [x] Extend the real HTTPS client fixture for repository reconstruction and durable revocation.
- [x] Register implemented selectors in WF-06; run relevant tests on Python 3.10.21 and 3.13.7, compile both, check fixture syntax, BOM, links and `git diff --check`.
- [x] Run the full browser strict journey gate after runtime edits; record exact results and Linux/external limitations, then update progress, roadmap and acceptance.

## Review checklist

- [x] No live request authenticates from startup tuples or mutable identity state.
- [x] Revocation before admission's write transaction blocks creation; reads linearize at their authorization transaction snapshot.
- [x] Conflicts cannot overwrite history, namespace, revocation or restore-disable.
- [x] Public responses contain no token digest or private snapshot. Cancellation joins owned I/O before returning.
- [x] Persistent connector catalog/UI, structured external output and real MCP/Muse pilots remain separately open.

## Verified local evidence

The six failure chains, immutable domain/transaction guards, process conflict/revocation, restore protection and reader/writer boundaries are recorded in [implementation progress](../../platform/workflow-platform/implementation-progress.md#f5-fleet-集成身份持久化与跨进程撤销2026-10-25). Original admission revocation: `/tmp/agent-fleet-identity-before.xml` → `/tmp/agent-fleet-identity-after.xml`. Edge/index and runtime-reader/startup-writer red evidence remain preserved separately.

- Related 13-module sets: Python 3.10.21 **381 passed**, Python 3.13.7 **381 passed**, no skips; `/tmp/agent-fleet-identity-verified-python310.xml` and `...-python313.xml`.
- Final browser gate: **4906 passed, 5 platform skips, 195 subtests passed**, no failures/errors, 598.62s. WF-06 **154 selectors / 331 cases**, all passed; 41/42 journeys. Strict exit 1 is preserved for Linux RELEASE-01, all seven external checks `not_run`.
- Evidence: `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-integration-identities-full-20261025-d7o01hqy/`; 819 frozen Git file digests unchanged; matrix `946557e483f3b05009bdaf98c6ca3d5ff401ad8bf1ff601da9ca17efc12b190d`. The earlier 4904-pass run predates the final two review repairs and is retained as historical evidence.
- Python 3.10/3.13 compileall, 578 Python AST files, UTF-8 without BOM, frontend syntax, local links/anchors and diff checks are recorded separately. No configured static type checker; compileall is not a type proof.

Local identity persistence is implemented and verified. The full platform and live F5 acceptance remain open; all gates stay default-off.
