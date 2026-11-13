# MCP Catalog Administration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification checkpoints. Execute inline in the existing isolated checkout; no delegation or Git publication.

**Goal:** Give authenticated administrators a usable, secret-free MCP catalog review and revocation interface without introducing transport registration or remote execution authority.

**Architecture:** Add pure command/response DTOs, reuse the existing catalog SQLite I/O port for bounded keyset reads and epoch CAS, and compose a concrete management service behind a distinct admin blueprint. Mount the vanilla JS panel inside account settings through a server-advertised capability. Existing runtime catalog and pinned Run path remain authoritative.

**Tech Stack:** Python 3.10+, Flask, SQLite WAL, static ES modules/CSS, pytest and installed Playwright/Chrome. No new dependencies.

## Global Constraints

- SSOT: artifact/capability/integration contract §7.1, §7.5, §7.8 and §7.9.
- Gates remain default-off; no remote nodes/models/MCP/Muse, Git publication, deployment, Obsidian writes or deletion.
- Preserve both primary and isolated checkout work; all changes are UTF-8 without BOM.
- Cookie admin only; no legacy operator fallback. Exact Origin/JSON; 2 KiB command body, 8-row pages, 128 historical registrations, original snapshot limits.
- No URL/SecretRef registration, network refresh, secret resolution, transport attachment, automatic write retry or Run mutation through this UI.
- Reads and updates validate existing independent schema 1/platform reader 4; no schema migration or duplicate state store.

## Task 1: Pure boundary DTOs and durable CAS

**Files:** create `hub/domain/mcp_administration.py`; modify `hub/infrastructure/mcp_catalog_repository.py`, `hub/application/connector_catalog.py`; test `tests/test_mcp_catalog_admin.py`.

**Interfaces:** `MCPAdminCommand.parse(action: str, payload: object) -> MCPAdminCommand`; frozen summary/detail/discovery/tool DTOs with explicit public fields; `MCPCatalogStore.list_page(after: str | None, limit: int) -> tuple[tuple[MCPCatalogRecord, ...], str | None]`; optional `expected_epoch: int | None` on original admit/revoke. Existing callers retain original contracts.

- [x] Write failing tests for command exactness, redaction, all lifecycle states, 8-row pagination, stale admit/revoke and unchanged original snapshot after failed CAS.
- [x] Record red evidence, then implement complete DTOs, paged reads and atomic expected_epoch checks. Do not change original digest/revision requirements.

```python
def test_epoch_cas_preserves_pending_record(tmp_path):
    store = seeded_store(tmp_path)
    before = store.get_many(("weather",))[0]
    with pytest.raises(MCPCatalogError, match="connector_revision_changed"):
        store.admit("weather", expected_revision=2, expected_epoch=before.epoch - 1, now=110)
    assert store.get_many(("weather",))[0] == before
```

Run `PYTHONPATH=. python3 -m pytest tests/test_mcp_catalog_admin.py tests/test_mcp_catalog_persistence.py -q`; expect all implemented cases to pass after the red run.

## Task 2: Management service, role boundary and feature advertisement

**Files:** create `hub/application/mcp_catalog_admin.py`, `hub/http/mcp_admin_routes.py`; modify `hub/bootstrap.py`, `hub/accounts/http.py`; same test module.

**Interfaces:** concrete `MCPCatalogAdminService(catalog)` with `list_page`, `detail`, `manage`; `ConnectorCatalog.attached_ids() -> frozenset[str]`; new blueprint `/api/platform/v1/integrations/mcp`, with exact `connector_id` query selectors on `/record`, `/admit`, `/revoke` so original dot-only IDs remain addressable. `me` adds `features.mcp_catalog_management=true` only for admin plus wired persistent management service.

- [x] Add read/write tests for anonymous, ordinary user, revoked/demoted/foreign-domain identities, no-account legacy identity, gate off and ephemeral catalog; assert no catalog I/O before authorization.
- [x] Add strict Origin/body/query/error/limit tests; inject repository errors and retain cause without leaking sensitive strings.
- [x] Implement public mapping and admin-only HTTP. Bind success response to original transaction record and success log to original request; read/write performs no network or secret access.

```python
def test_ordinary_account_cannot_inspect_catalog(admin_setup):
    app, admin, ordinary, catalog = admin_setup
    original = catalog.store.get_many(("weather",))[0]
    assert ordinary.get(PATH).status_code == 403
    assert ordinary.post(PATH + "/revoke?connector_id=weather", json={"expected_epoch": original.epoch},
                         headers={"Origin": ORIGIN}).status_code == 403
    assert catalog.store.get_many(("weather",))[0] == original
```

Run `PYTHONPATH=. python3 -m pytest tests/test_mcp_catalog_admin.py tests/test_account_http.py tests/test_mcp_run_pinning.py -q`; expect passing and zero external transport calls.

## Task 3: Account panel and real browser journeys

**Files:** create `frontend/assets/api/integrations.js`, `frontend/assets/views/account/integrations.js`, `frontend/assets/styles/integrations.css`, `tests/test_mcp_catalog_admin_browser.py`, `tests/fixtures/frontend/integrations_browser.cjs`; modify `frontend/assets/views/account.js`, `frontend/assets/styles/app.css`, shared `frontend/assets/api/client.js` and `tests/test_release_layout.py` for body-lifetime cancellation and explicit module inventory.

**Interfaces:** strict `parseMCPPage`/`parseMCPDetail`; API functions `listMCPConnectors`, `getMCPConnector`, `manageMCPConnector` reuse `platformRequest` and `apiPath`. `mountMCPIntegrations(root)` returns teardown with `suspend` and `resume`; cancellation is bound to the mounted view. No browser storage.

- [x] Add browser tests using real local Flask/SQLite plus fake external transport: admin list/paging, full text-only review, exact confirmation, approval, stale conflict/readback, revoked/restored/unattached states and unknown-response recovery without mutation replay.
- [x] Implement keyboard-accessible native dialog and visible live status, bounded responsive list/detail, explicit reload, and role/feature-guarded mounting. Abort fetch and close dialogs on suspend/dispose; re-read on resume.
- [x] Exercise desktop and narrow viewport, Escape/focus, hostile metadata, real account switching, gate-off absence and in-memory catalog absence. Retain screenshot evidence via existing gate output directory.

```bash
FLEET_PLAYWRIGHT_MODULE=/tmp/agent-fleet-f4-1-browser-deps/node_modules/playwright FLEET_BROWSER_CHANNEL=chrome PYTHONPATH=. python3 -m pytest tests/test_mcp_catalog_admin_browser.py -q
```

## Task 4: Regression gates and implementation record

**Files:** `docs/testing/journeys.json`, `docs/testing/workflow-platform-acceptance.md`, `docs/platform/workflow-platform/README.md`, `docs/platform/workflow-platform/implementation-progress.md`, parent roadmap and current navigation summaries.

- [x] Register every new required selector in WF-06; keep external checks `not_run` and Linux release requirements intact.
- [x] Run focused modules on Python 3.10.21 and 3.13.7; compile both, check module syntax, UTF-8/no-BOM, Markdown links and diff whitespace.
- [x] Freeze source digests, run the full installed-browser gate, inspect actual JUnit/journeys/screenshots and record exact results. Repair introduced failures and rerun relevant checks.
- [x] Update completion checkboxes and implementation record honestly; do not mark F5/platform complete while local/external gaps remain.

```bash
PYTHONPATH=. /tmp/agent-fleet-py310-workflow-20261021/bin/python -m pytest tests/test_mcp_catalog_admin.py tests/test_mcp_catalog_admin_browser.py tests/test_mcp_catalog_persistence.py tests/test_mcp_connector.py tests/test_mcp_run_pinning.py tests/test_account_http.py -q
python3 -m compileall -q hub tools tests
/tmp/agent-fleet-py310-workflow-20261021/bin/python -m compileall -q hub tools tests
git diff --check
```

Full gate follows `docs/testing/workflow-platform-acceptance.md` and installed dependency paths; a strict nonzero exit caused by unrun Linux requirements stays failed, never relabeled as production approval.

Review continuation: retain cancellation/timer until response JSON settles; preserve cause and public error codes. Four deterministic body regressions first fail, then must pass. Add the two actual modules to the explicit release inventory; keep generated-artifact and credential checks intact. The first full run received two stop signals and is not final evidence; one real release-inventory failure and two signal-triggered DeployError failures remain recorded.

Final verified evidence: eight-module sets Python 3.10.21 and 3.13.7 each 381 passed, no skips; full installed-browser gate 5045 passed、0 failures/errors、5 skipped、195 subtests passed; WF-06 217 selectors / 470 cases, 41/42 journeys. Strict exit 1 / ci_status=failed remains for Linux RELEASE-01, all seven external checks not_run. Evidence: `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-mcp-admin-full-20261025-b_v944e0`. No publication, deployment or real external action.
