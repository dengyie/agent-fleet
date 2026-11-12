# Fleet Identity Registration UI Implementation Plan

> **For agentic workers:** Use `executing-plans` inline in the existing isolated feature checkout, task by task. Preserve the reviewed public candidate; no production merge or deployment is authorized by this plan.

**Goal:** Register a usable Fleet MCP identity from the account page, using the existing durable identity authority and a browser-generated bearer token.

**Architecture:** Add an exact registration command and cookie-admin endpoint over the existing immutable identity repository. Validate availability of the referenced workspaces inside the original registration transaction for this new entry point; retain trusted CLI/bootstrap behavior. Reuse the existing account identity panel, HTTP deadlines, lifecycle cancellation and secret-free detail DTO. The browser keeps a newly generated token only in the open dialog and sends its SHA-256 digest.

**Tech Stack:** Python 3.10+, Flask, SQLite, static JavaScript, existing pytest/Playwright/Chrome; no new packages or services.

## Global Constraints

- Work only in the existing feature branch; UTF-8 without BOM.
- Exact request: `{"identity": <the existing canonical nine-field identity definition>}`. No raw token, owner substitution, unknown field, duplicate JSON key or implicit scope expansion.
- Only cookie-admin with the existing exact Origin proof may register. Existing revoke body remains limited to 2048 bytes; registration accepts at most 65536 bytes for the original 128-entry allowlists.
- Same identity key and same definition are idempotent. Changed definitions and token collisions fail. Revoked or restore-disabled identities are never reactivated.
- New registrations require open Workflow admission and all selected owner/workspaces enabled in the same write transaction. Exact repeats preserve their original facts even if subsequent availability changes.
- No provider/network calls, Workflow creation, model execution, dispatch or production configuration changes are triggered by registration.
- Browser generation uses `crypto.getRandomValues` and `crypto.subtle.digest`; no local/session storage or raw-token registration HTTP transmission. An uncertain response retains the same locked definition/token for explicit idempotent retry, with no automatic POST retry.
- Closing the dialog, leaving the page or changing account cancels local observation and prevents stale response application. Completed server mutations retain their actual result.

## Task 1: Prove the missing registration path

**Files:** create `tests/test_integration_identity_registration.py`.

- [x] Add actual Flask registration tests for usable original MCP authentication, immutable repeat across a fresh repository, definition/token conflicts, revoked/restore-disabled repeat, disabled or foreign workspace, frozen admission, admin/Origin/JSON boundaries, maximum allowlists and rollback after an insert failure.
- [x] Add pure command and actual JavaScript parser tests for exact fields, byte/reference/count/budget bounds, canonical UTF-8 ordering and response identity correlation.
- [x] Run `PYTHONPATH=.:tests /Users/mango/project/codex/agent-fleet/.venv/bin/python -m pytest tests/test_integration_identity_registration.py -q --junitxml=<private evidence>/red.xml`; retain failing output before product changes.

## Task 2: Add registration to the original authority

**Files:** modify `hub/domain/integration_administration.py`, `hub/application/integration_identity_admin.py`, `hub/infrastructure/integration_identity_repository.py`, `hub/http/integration_admin_boundary.py`, `hub/http/integration_admin_routes.py`.

**Interfaces:** `IntegrationRegisterCommand.parse(payload)` returns a frozen command containing the existing `IntegrationIdentity`; `IntegrationIdentityAdminService.register(command)` returns the existing `IntegrationIdentityDetail`. `IntegrationIdentityRepository.register(identity, *, now, require_available_workspaces=False)` retains its original default and performs current workspace/admission checks before new insertion when requested. `POST /api/platform/v1/integrations/fleet/register` returns `{"ok":true,"identity":<existing detail>}`.

- [x] Parse canonical definitions with the existing domain parser and require a valid platform owner before any store mutation.
- [x] Add transaction-local availability validation for new registrations, preserving existing immutable-repeat and conflict checks. Map malformed input to 400, unavailable/current-state conflicts to 409, and storage failures to 503.
- [x] Keep existing no-store response and body/Origin boundaries; allow the larger bounded body only on registration. Log actor/key/action/state without tokens or digests.
- [x] Rerun the failing registration tests plus identity administration, identity authority, CLI setup, Fleet MCP and backup regressions; inspect actual output.

## Task 3: Complete account-page registration

**Files:** modify `frontend/assets/api/integration-identities.js`, `frontend/assets/views/account/identities.js`, `frontend/assets/styles/integrations.css`; create `frontend/assets/styles/identity-registration.css` and `tests/fixtures/frontend/identity_registration_browser.cjs`; extend `tests/test_integration_identity_admin_browser.py`.

**Interfaces:** `parseIdentityRegistrationCommand(payload)` validates the exact outgoing definition. `registerIntegrationIdentity(payload, options)` uses the original `platformRequest` and verifies all returned public policy fields against the submitted identity. The existing panel adds a registration dialog with owner/identity, newline-delimited template/workspace allowlists, explicit three-tool scopes and the original bounded budgets.

- [x] Generate one bearer token per submission draft, send only its digest, show the token for saving and lock the original policy after the first POST. A retry explicitly reuses the same definition and token.
- [x] Render successful active/revoked/restore-disabled states accurately, update the original list/detail, and preserve the token until dialog close. Do not claim an uncertain response succeeded or perform automatic retry.
- [x] Add real desktop/mobile creation, actual original MCP credential use, reload, lost-response exact retry, local hash/POST lifecycle cancellation and account-change isolation coverage. Save screenshots privately and inspect them.

## Task 4: Register evidence and close this implementation

**Files:** modify `docs/testing/journeys.json`, `docs/platform/workflow-platform/integration-setup.md`, `docs/platform/workflow-platform/README.md`, `docs/platform/development-completion.md`, `docs/superpowers/plans/2026-10-20-workflow-platform-roadmap.md`.

- [x] Register actual new selectors under WF-06, link this plan, document UI token handling and original CLI compatibility, and distinguish local implementation from the remaining external pilots/configuration UI.
- [x] Run related Python/browser/gate tests, CI-required Ruff rules, JavaScript/Python syntax, UTF-8/no-BOM, diff and full selector collection checks. Commit only the verified feature; do not alter the ready PR42 candidate or mark all TODO complete. Feature committed as `f43ef43`; subsequent status recording changes documentation only.

## Task 5: Publish an independent candidate and run its exact CI

**Files:** modify `.github/workflows/ci.yml`; use the existing isolated publication checkout.

- [x] Add the actual Native setup and Fleet registration modules and an existing-Node version check to the Windows CI selection without adding runtime installation. Preserve all prior selectors and the main-only package/deploy conditions; actual Windows execution remains part of the terminal CI check below.
- [ ] Export only the current tree delta onto a separate publication branch rooted at the reviewed PR42 head, retain the pre-push hook, and verify its complete Git tree equals the development candidate before publishing.
- [ ] Create and attach a draft PR with the reviewed publication branch as its base. Dispatch the existing CI on this new feature branch; branch dispatch executes tests and skips package/deploy. Inspect the actual run state and leave full CI/production acceptance open until terminal evidence proves them.
- [ ] Inspect new-source Linux/browser and Windows terminal evidence; retain actual failures and rerun only after evidence-backed fixes. Production merge/deploy and actual external client acceptance still require their own evidence and authorization.

## Local verification — 2026-11-13

- Initial registration regressions: 29 failed. Missing browser button: one failed. All original failing output is retained outside the repository.
- Actual browser failures exposed label/helper ambiguity, the native dialog close event's asynchronous window, and exact UTF-8 identifier loss from `trim()`. The last identifier repro submitted a different valid owner, so it was fixed by preserving the original strings rather than rejecting that owner.
- Screenshot review and a real hit-test exposed the generated token below the modal viewport. Generation now brings the token into view and selects it; browser assertions verify the token is visible and accessible on desktop/mobile before it is saved or the dialog is closed.
- Seven backend/authority/CLI/MCP/plain and encrypted backup modules: 247 passed, zero failures/errors/skips. Latest dedicated registration module: 31 passed. Eight related administration/auth/frontend/boundary/package/gate modules: 351 passed. CI/package/boundary recheck after adding the Windows selection: 164 passed. These independent scopes are not aggregated into a full-suite claim.
- Latest two integration browser modules: 20 passed, zero failures/errors/skips. Includes actual original MCP credential authentication, exact immutable registration retries, mobile two-tool discovery, cancellation/lifecycle and exact UTF-8 identifiers. Credential authentication after normal Hub restart is verified separately by the backend registration test.
- Full collection resolves 2529 selectors across 46 journeys and 7541 cases. All 26 latest PNGs passed header/chunk CRC/decompression/row checks; mobile configuration and token delivery screenshots were visually inspected, with generated token fields masked in evidence.
- CI-required Ruff rules, JavaScript syntax, Python 3.10 grammar, UTF-8/no-BOM, YAML selection/main-only release conditions and `git diff --check` passed. Intermediate test-fixture failures (workspace name limit, missing browser window shim, and expecting three tools after selecting two permissions) remain in the original reports.
- Private raw evidence: verification root `identity-registration-20261113/`; authoritative completed reports are `backend-verified.xml`, `registration-exact-final.xml`, `browser-delivery-final.xml`, `regression-final.xml`, `ci-contract-final.xml`, `collection-final.log` and `local-verification.json`. This is local evidence; new full Linux/Windows CI, publication, production and global TODO states stay distinct.

Return to [operator instructions](../../platform/workflow-platform/integration-setup.md#从账号页面登记), [Workflow entry](../../platform/workflow-platform/README.md), and [development ledger](../../platform/development-completion.md#fleet-身份页面登记开发增量2026-11-13独立于公开-pr42).
