# Fleet Identity Administration Implementation Plan

> **For agentic workers:** Use `executing-plans` inline in the existing isolated checkout. No additional agents, commits, publication or external integration calls.

**Goal:** Let current cookie administrators inspect secret-free persistent Fleet identities and revoke an explicitly reviewed active registration.

**Architecture:** Keep immutable identity authority and the original digest-checked revoke transaction. Add bounded public DTOs, one administration storage port, composite-key queries and one optional active-state precondition. Reuse the account lifecycle, native dialogs and shared HTTP body cancellation; no schema or credential authority changes.

**Tech Stack:** Python 3.10+, Flask, SQLite WAL, static ES modules, pytest, existing Node/Playwright/Chrome; no dependencies or downloads.

## Global constraints

- SSOT: [integration contract §7.10](../specs/2026-10-20-artifact-capability-integration-contract.md#710-fleet-身份管理界面与撤销确认), together with §7.7 and §7.9.
- Work only in `codex/workflow-platform-docs`, UTF-8 without BOM; preserve every existing file and the protected primary checkout.
- Platform/Workflow/Fleet MCP gates must all be enabled; defaults remain off. No role inferred from CF/DEV/Fleet credentials.
- No token issuance, registration, update, delete, enable, secret lookup, task cancellation, new tool or automatic POST retry.
- At most 8 public records/9 queried records per page; exact UTF-8 composite identity, BINARY keyset and no N+1. Each stored snapshot ≤64 KiB, allowlists ≤128.

## File map and contracts

| File | Responsibility |
|---|---|
| `hub/domain/integration_administration.py` | Frozen key, private durable record, public summary/detail/page/revocation and exact command validation |
| `hub/infrastructure/integration_identity_repository.py` | `list_page(after,limit)`, `get_record(owner,id)`, optional `expected_state` in original revoke transaction |
| `hub/application/integration_identity_admin.py` | `IntegrationIdentityAdminStore` I/O port and stateless projection/revoke orchestration |
| `hub/http/integration_admin_boundary.py` | Shared cookie-admin check and strict 2 KiB Origin/JSON reader for both management boundaries |
| `hub/http/integration_admin_routes.py` | Fleet identity list/detail/revoke, whitelisted request errors and safe action context |
| `hub/http/mcp_admin_routes.py` | Reuse shared HTTP validation without changing the MCP contract |
| `hub/infrastructure/workflow_bootstrap.py`, `hub/bootstrap.py`, `hub/accounts/http.py` | Gate-scoped service/blueprint and admin-only feature advertisement |
| `frontend/assets/api/integration-identities.js` | Exact DTO parsing, UTF-8 ordering and bounded existing-client requests |
| `frontend/assets/views/account/identities.js`, `frontend/assets/views/account.js`, `frontend/assets/styles/integrations.css` | Fleet identity inspector, confirmation, readback and coordinated lifecycle |
| `tests/test_integration_identity_admin.py`, `tests/test_integration_identity_admin_browser.py`, `tests/fixtures/frontend/integration_identities_browser.cjs` | Durable HTTP/transaction and real desktop/mobile/account evidence |
| `tests/test_release_layout.py`, `docs/testing/journeys.json` | Explicit two-module release inventory and required WF-06 selectors |

## Task 1: Deterministic contracts and durable queries

- [x] Add failing HTTP/DTO/store evidence for the missing boundary before implementation; retain JUnit outside the repo.
- [x] Validate exact `{expected_state:"active"}`, paired cursor fields, references and page size. Preserve original immutable identity parsing.
- [x] Query `WHERE (owner_id,integration_id)>(?,?) ORDER BY owner_id,integration_id LIMIT ?`, with `limit+1≤9`; map fully validated rows and exclude lookahead from public output.
- [x] Add `expected_state` to the original `revoke`; after loading and digest-checking the original record, reject disabled/non-active state before UPDATE. Existing trusted repeat revoke must still preserve its first timestamp.
- [x] Verify query plan, UTF-8 IDs, corrupt rows/schema, paging and independent-repository races.

## Task 2: Application and HTTP management boundary

- [x] Project only explicit public DTOs; state precedence is restore_disabled, revoked, active. Public mutation returns owner/id/revoked from the original transaction, without a misleading detail read.
- [x] Reuse one cookie-admin check and strict JSON reader in MCP/Fleet routes; no request validation in domain is weakened. Request ID/cause/no-store stay intact.
- [x] Enforce exact queries, Origin, ≤2048 bytes, duplicate/unknown fields and safe fixed error details; unauthorized requests cannot access identity storage.
- [x] Wire only to the durable gate-enabled identity store; `/me` independently advertises both available management features and omits unavailable ones.
- [x] Verify anonymous/ordinary/machine/Bearer/CF/DEV isolation, current role/session checks, secret-free DTO/log/error and no Workflow mutations.

## Task 3: Account inspector and recovery

- [x] Parse actual HTTP DTOs with exact keys, safe integers, valid UTC, full reference/allowlist bounds, UTF-8 composite ordering and no private fields.
- [x] Render list/detail as text, require permission review confirmation, and use native dialog with Escape/focus. Reuse styles without a generic UI framework.
- [x] Read back success and conflict; uncertain responses block writes until explicit readback. Abort local fetch/dialog on navigation/destruction/account changes and reread on resume.
- [x] Run real Chrome desktop/mobile/recovery/lifecycle/empty/gate-off scenarios; capture PNGs and verify no horizontal overflow or executable metadata.
- [x] Add both JS modules to required, allowed and credential release inventories.

## Task 4: Self-review and final evidence

- [x] Run focused Python 3.10.21/3.13.7 identity, Fleet MCP, MCP administration, account, release and frontend sets with installed browser fixtures.
- [x] Self-review auth, exact transaction, recovery, DTO, body lifecycle and release boundaries; retain red evidence for defects and fix before acceptance.
- [x] Register every new implemented selector in WF-06; count collected cases rather than extrapolate selectors.
- [x] Run both compileall, AST, JS/CJS syntax, UTF-8/BOM, Markdown links/anchors and `git diff --check`; report absent static type checker honestly.
- [x] Freeze source digests and run the full browser-enabled strict journey gate without stop signals; retain actual failures/skips and external `not_run`.
- [x] Update progress, navigation, roadmap and acceptance from verified JUnit/journey/source evidence. Mark only this subset complete; the whole platform stays active.

## Reproducible verification commands

```bash
PYTHONPATH=.:tests python3 -m pytest tests/test_integration_identity_admin.py -q --tb=short
PYTHONPATH=.:tests /tmp/agent-fleet-py310-workflow-20261021/bin/python -m pytest tests/test_integration_identity_admin.py tests/test_integration_identity.py tests/test_fleet_mcp_boundary.py tests/test_mcp_catalog_admin.py tests/test_account_http.py tests/test_release_layout.py -q
PYTHONPATH=. FLEET_PLAYWRIGHT_MODULE=/tmp/agent-fleet-f4-1-browser-deps/node_modules/playwright FLEET_BROWSER_CHANNEL=chrome python3 -m pytest tests/test_integration_identity_admin_browser.py tests/test_mcp_catalog_admin_browser.py -q
python3 -m compileall -q hub tools deploy tests
git diff --check
```

The implementation report records the full frozen-source command, dual-version JUnit, actual browser screenshots and any Linux/external gaps. No verification command installs dependencies or contacts a real integration.

Verified local evidence: serial Python 3.10.21/3.13.7 eleven-module sets each 323 passed, no skips and two passing subtests. Full installed-browser gate 5108 passed、0 failures/errors、5 skipped、195 subtests passed; WF-06 243 selectors / 533 cases, 41/42 journeys, strict exit 1 for Linux RELEASE-01; all seven external checks not_run. Frozen evidence: `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-identity-admin-full-20261025-8_4rbeyu`. No publication, deployment or real integration action.
