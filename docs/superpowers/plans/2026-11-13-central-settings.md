# Central Settings Implementation Plan

> **For agentic workers:** Execute inline with writing-plans and verification-before-completion; use the existing feature worktree. No delegation.

**Goal:** Provide one detailed settings page for model endpoints/credentials, VM execution nodes, workspaces, defaults, integrations, account management and runtime configuration.

**Architecture:** Keep PlatformRepository and existing integration/account services as the configuration owners. Add an administrator settings API with atomic optimistic writes and an immutable file credential broker; reuse existing account and integration views inside a sectioned settings shell. Persist deployment switches to the existing fleet-gates.conf and distinguish saved settings from effective process configuration.

**Tech Stack:** Flask, SQLite, vanilla JavaScript ES modules, existing CSS/icon components, pytest and Playwright Chrome.

## Global Constraints

- UTF-8 without BOM; keep primary main clean.
- Existing owners and authentication boundaries remain authoritative.
- No new dependencies, no provider calls on settings load/save, no VM lifecycle controls without a provider implementation.
- Secrets never returned; unchanged credentials preserved; rotated credentials retain immutable references for queued Runs.
- Catalog edits apply to future resolved Runs; existing workflow registration snapshots remain pinned.
- Runtime file edits require controlled restart and never restart the server automatically.

### Task 1: Persist editable catalogs and model credentials

Files: create hub/application/settings_service.py and hub/infrastructure/model_secrets.py; extend hub/infrastructure/platform_db_catalog.py; add hub/http/settings_routes.py and register it in hub/composition/routes.py and extensions.py. Wire the credential broker in hub/composition/platform.py. Tests: tests/test_settings.py.

- [x] Add tests that GET settings exposes endpoint but not credential/ref; PUT saves endpoint/model/capabilities, preserves an unchanged secret, rotates credentials, rejects stale edits and cross-owner access.
- [x] Reuse validate_model_profile/validate_workspace/validate_node and serialize a digest of the persisted configuration; compare digest under BEGIN IMMEDIATE before upsert.
- [x] Save API keys to immutable files through exclusive 0600 creation; resolve managed refs through a broker that delegates env refs to EnvironmentSecretBroker.
- [x] Verify a newly composed worker resolves the saved key and old snapshots keep the previous key; no network request occurs.

Interfaces: SettingsService.catalog(owner) -> dict; SettingsService.save(owner, kind, id, body) -> dict. API GET /api/platform/v1/settings and PUT /settings/<kind>/<id>, body {expected_digest, value, api_key?}. Catalog rows include digest and model provider_config, without secret_ref.

### Task 2: Runtime settings and host metadata

Files: create hub/application/runtime_settings.py; use the existing FleetConfig and fleet-gates.conf file. Tests: tests/test_settings.py.

- [x] Define explicit supported restart-time switches/registration paths; GET returns effective and persisted values and environment override markers, never tokens.
- [x] Persist validated changes atomically under a file lock and SHA precondition; keep an exact previous-file backup. Preserve unrelated lines and reject unknown keys.
- [x] Expose editable hosts YAML with structural validation and digest conflict detection; display runtime paths separately as read-only information.
- [x] Verify invalid/conflicting edits leave file bytes unchanged and saving never changes active app.config.

Interfaces: RuntimeSettings.read() -> dict; RuntimeSettings.save(body) -> dict. API GET/PUT /api/platform/v1/settings/runtime, saving file plus digest.

### Task 3: Unified responsive settings UI

Files: create frontend/assets/views/settings.js, settings/catalogs.js, settings/runtime.js, settings/forms.js, frontend/assets/api/settings.js and styles/settings.css. Modify app.js, navigation.js, index.html, pages.py and account.js.

- [x] Add /settings and /settings/<section>; route old /account to the account section without duplicate management UI.
- [x] Add searchable section navigation, model forms (URL/protocol/model/key/timeouts/retries/stream/headers/pricing/capabilities), nodes (capabilities/status/enrollment credential), workspaces and defaults.
- [x] Move MCP/identity/summary administration from account.js into the integration section, mount lazily and dispose/suspend correctly.
- [x] Include account administration, appearance and plugin settings access; daily assistant retains per-run model/workspace selection.
- [x] Show save/loading/errors/conflict recovery and effective/restart semantics. Never retry uncertain mutations automatically.

### Task 4: Verification and documentation

Files: tests/test_settings_browser.py, tests/fixtures/frontend/settings_browser.cjs; update existing integration/browser navigation to new routes and document settings in docs/account-management.md plus canonical project index.

- [x] Run focused backend settings/defaults/provider/account suites.
- [x] Run real desktop/mobile settings save/reload, credential preservation, stale conflict and navigation tests.
- [x] Run moved integration/account browser regressions and frontend structural contracts; inspect screenshots and no-overflow/pageerror evidence.
- [x] Review diff and run fresh verification before reporting scope and limitations.

## Verification record

- Backend: settings/defaults/accounts/provider focused run 45 passed; additional host/startup/concurrent-write tests added afterwards.
- UI: settings desktop/mobile save-reload/conflict verified, original management browser checks 39 passed with 3 old-navigation fixture failures; corrected lifecycle cases 5 passed.
- Fresh focused run: 220 cases passed (including subtests) before final validation additions; startup/import/MCP/Muse regressions 161 passed + 5 subtests; settings desktop/mobile and backend run 16 passed. Management browser run 40 passed with one account-label fixture failure, corrected by keeping IDs in a separate element and rerun independently. Full regression remains in progress; original interrupted run retained with its startup-empty-value failure evidence.
- No cloud provider lifecycle adapter introduced. No deployment, real provider request or production settings write is part of local verification.

- Full local Chrome run (first complete run): 7733 passed, 204 subtests, 12 platform skips, 5 failures. Four failures were release allowlist/minimal-DOM fixture omissions and were corrected; the held-feature-401 account-switch case passed its independent rerun. This is not recorded as a full green run. Evidence: pytest-all-final.xml/log.
- Settings login return paths now preserve /settings/<section>, verified with a fresh expired-cookie/login browser flow. The settings journey is part of the required CI matrix as SETTINGS-01.
- Final persistence review reproduced three failures: rejected provider headers left a credential file, and managed references did not fence old readers with or without an existing MCP catalog. Provider validation now precedes immutable-file creation; model persistence upgrades the reader in the same transaction. MCP initialization preserves the higher reader and catalog-presence marker. Fresh settings/MCP/backup/deploy rollback regression: 105 passed; database restore reopens successfully and simulated version-4 readers refuse the new binding.
- Documentation governance and stale-document checks returned RESULT=OK; 81 documentation-governance unit tests passed.
- Final local candidate verification: 216 passed + 2 subtests (settings desktop/mobile, accounts, frontend contracts, assistant submission, release layout and workflow startup/setup). Production lint, changed-source JavaScript/Python 3.10 syntax, UTF-8/BOM and diff checks passed. Full lint of the older MCP repository also reported its pre-existing import-order/Iterator modernization rules; required production lint passed. Public same-revision CI remains the release gate.
