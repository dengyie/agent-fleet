# Settings lifecycle fixes implementation plan

> Execute inline in the existing isolated worktree. User authorization covers all three verified defects; each task has its own failing browser evidence and verification checkpoint.

**Goal:** Deliver issued VM enrollment files across settings navigation, preserve newer runtime drafts, and make failed integration loading recoverable.

**Architecture:** Keep identity lifetime in the authenticated application shell, editor lifetime in each settings view, and integration loading state in its existing dialog. Enrollment delivery follows identity lifetime; ordinary UI updates follow editor generation. No backend enrollment replay or additional persistence is introduced.

**Tech stack:** Native JavaScript modules, Flask/SQLite, pytest, Playwright with installed Chrome.

## Constraints and evidence

- Work in `codex/settings-lifecycle-fixes`; preserve primary and unrelated worktrees.
- Use UTF-8 without BOM and existing dependencies. No production mutation.
- Existing replay: `/tmp/agent-fleet-review-settings-repro.py` showed zero downloads after credential rotation, overwritten hosts draft, and one failed integration GET across reentry.
- Test entry point: [SETTINGS-01](../../testing/README.md#集中设置流程-settings-01).

## Task 1: Enrollment delivery follows the authenticated identity

Files: `frontend/assets/app.js`, `frontend/assets/views/settings.js`, `frontend/assets/views/settings/catalogs.js`, `frontend/assets/views/assistant.js`, `frontend/assets/views/assistant/nodes.js`; test in `tests/test_settings_browser.py` and `tests/fixtures/frontend/settings_lifecycle_browser.cjs`.

- [x] Add a real Flask browser test that holds the committed enrollment response, then switches categories, closes settings, replaces the editor, switches configuration owner, or evicts the cached route. Require exactly one POST and one valid downloaded file containing the initiating owner's workspaces. Check credential authentication in Python after download.
- [x] Run the scenarios against the unchanged implementation; expect missing-download failures.
- [x] Create an identity lifetime `AbortController` in the shell; pass its signal to the settings catalog and the assistant node panel. Abort only when the authenticated shell is disposed. Do not tie this signal to route eviction.
- [x] Deliver the enrollment file after a successful response unless the initiating identity signal is aborted. Keep `disposed`, generation and connected-form checks around UI text updates only. Never repeat the enrollment request to recover a result.
- [x] Verify all navigation cases and logout/identity invalidation; ensure no credentials appear in page HTML.

## Task 2: Fence runtime save receipts with editor generation

Files: `frontend/assets/views/settings/runtime.js`; same browser test files.

- [x] Hold success/failure PUT receipts for both runtime and hosts. Reload, type a newer draft, release the receipt, and require the draft and current status to remain intact.
- [x] Run before changes; expect late-success draft loss and late-error stale status failures.
- [x] Use the existing `ticket` captured by `load()` plus `form.isConnected` to fence success and failure before updating UI or reloading. Existing disposed checks remain.
- [x] Verify all four cases and the original real save/reload acceptance flow.

## Task 3: Retry integration discovery without duplicate mounts

Files: `frontend/assets/views/settings.js`; same browser test files.

- [x] Inject an initial `/accounts/me` 503. Verify explicit retry and cached category reentry each recover; hold a retry across repeated navigation and require only one in-flight discovery and one child mount.
- [x] Run before changes; expect missing retry/recovery failures.
- [x] Track discovery state (`idle`, `loading`, `failed`, `loaded`), reuse one status element, expose a retry button on failure, and retry failed discovery on resume. Guard disposal/epoch on success and error. Do not remount healthy integrations.
- [x] Verify recovery, in-flight deduplication, healthy cached state preservation and existing motion/scope tests.

## Final verification

- [x] Register the new test selectors and acceptance statements in `docs/testing/journeys.json`; update the linked SETTINGS-01 description.
- [x] Run the new browser regressions plus `tests/test_settings_browser.py`, `tests/test_settings.py`, `tests/test_workspace_node_setup.py`, `tests/test_account_http.py`, `tests/test_navigation_sessions.py`, and journey gate tests using the existing Python environment and Playwright module.
- [x] Inspect the complete diff, confirm no whitespace errors or BOMs, record exact results and remaining production validation limits. Do not infer deployment from local tests.

## Verified result

- Original production-code paths failed the enrollment navigation, late runtime/hosts receipt and integration retry/reentry regressions before fixes.
- New browser scenarios: 7 enrollment lifetime cases, 4 runtime/hosts receipt cases and 3 integration recovery cases. All 14 pass. Enrollment files are additionally installed by the real Node CLI configuration loader and authenticated against SQLite.
- Combined fresh regression run: **220 passed in 103.29s**, zero skips. Scope: settings/browser, workspace enrollment, account HTTP, navigation, cross-account identity clearing, frontend contracts/static separation and journey gate tests.
- Command: `FLEET_PLAYWRIGHT_MODULE=<installed-playwright> PYTHONPATH=. <existing-venv>/bin/python -m pytest tests/test_settings_browser.py tests/test_settings.py tests/test_workspace_node_setup.py tests/test_account_http.py tests/test_navigation_sessions.py tests/test_account_browser.py::test_account_switch_clears_previous_identity tests/test_frontend_contracts.py tests/test_frontend_separation.py tests/test_journey_gate.py -q --tb=short --show-capture=no`.
- `git diff --check` and UTF-8/no-BOM validation pass. This is local targeted regression evidence, not a complete release-gate or production-deployment result.
