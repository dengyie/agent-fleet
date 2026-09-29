# Frontend Console Implementation Plan

**Goal:** Replace the dual SSR/static UI with one independent, accessible Chinese console using awesome-ui components and the existing public APIs.

**Architecture:** One static `frontend/index.html`; all application modules under `frontend/assets/`. Flask optionally serves bytes for local development and otherwise exposes only APIs. Business policies and credentials remain on the backend. Existing bounded client, store and SSE contracts remain authoritative.

**Tech Stack:** Native ES modules, CSS, adapted awesome-ui Web Components, Flask, pytest, Chromium.

## Constraints

- Work on `codex/frontend-console` in the existing isolated worktree; no production deployment or push.
- Reuse awesome-ui Vanilla components and UiIcon; repair unsafe dynamic HTML at the component boundary and record source provenance.
- Preserve real task/session/conversation/approval flows. No fabricated usage, monitoring values, model responses or working controls without behavior.
- No CDN runtime, framework migration, duplicate HTTP transport, browser secrets or inferred approvals.

## 1. Static boundary

- [x] Add behavior tests for byte-identical HTML, nested routes, isolated `/assets/`, API JSON failures, API-only boot and path confinement.
- [x] Move modules to `frontend/assets/`; migrate imports, release paths and existing tests. Keep `config.js` at the release root.
- [x] Replace `hub/http/pages.py` with static-only responses. Keep `serve_frontend=True` for local hosting; use `serve_frontend=False` / `--no-serve-frontend` for API-only operation. Remove Flask templates/static renderers and their obsolete tests. Use `--no-serve-frontend` in production launchers and `/api/status` for guardian health checks; keep `--serve-frontend` for optional local hosting.
- [x] Make the separate frontend release work behind a same-origin Nginx root plus `/api/` proxy, with no HTML transformation or asset/API overlap.

## 2. Application shell and visual system

- [x] Extract index inline JS into `assets/app.js`, `shell/navigation.js` and `shell/theme.js`. Application entry owns client/store/SSE and view teardown.
- [x] Build the sidebar, Chinese page headings, responsive drawer, skip link, keyboard focus, connection/error states and theme persistence. Use uiIcon for all interface icons.
- [x] Replace layered CSS with tokens, shell and feature styles; cover all six existing views and mobile widths. Keep useful action/API logic in the view controllers.

## 3. Assistant components

- [x] Vendor the selected awesome-ui files with upstream revision/provenance. Reuse ChatPromptInput and ToolCallBadge; sanitize text, bound values and preserve IME / uncertain-submit behavior.
- [x] Extract assistant rendering into focused panels and separate conversation from execution, artifacts and optional memory/legacy-task controls.
- [x] Keep durable answers as the display source, paired tool calls/results as execution evidence, and cancellation/unknown/approval semantics intact.

## 4. Verification and delivery

- [x] Run migrated contracts and behavior tests. Remove only tests of intentionally removed SSR/duplicate presentation; retain security, state and API assertions.
- [x] Exercise a disposable real Flask API plus Chromium: fleet, node/task/session, assistant submission/restoration, service pages, failures, mobile drawer, themes, keyboard and XSS strings.
- [x] Check no resource/module/console errors, syntax, full pytest, static release packaging and local smoke. Save desktop/mobile screenshots.
- [x] Update architecture/development documentation and commit locally with exact verification results and deployment limits.


## Verification evidence (2026-09-29)

- Full local suite with real Chrome enabled: **2038 passed, 2 skipped, 154 subtests passed** in 75.21s. One existing invalid-escape docstring warning remains in `tools/probe/discovery.py`; it is unrelated to this frontend change.
- Final focused frontend/API/release/session contracts: **210 passed, 2 subtests passed**. Guardian/API-only regression: **45 passed, 2 subtests passed**.
- Real-browser acceptance covers all six views at desktop and 390px, persisted assistant submission and reload, IME composition, sanitized model/tool strings, themes, mobile focus trapping/inert state/Escape, HTTP failure rendering, and no JavaScript or failed-resource responses. HTTP 304 revalidation is accepted.
- `deploy/test-static-frontend.sh`: **STATIC OK**. `deploy/e2e-smoke.sh`: **SMOKE OK**, including real local runner subprocess, result/diff, SSE and backend boot without frontend files.
- Python AST, all frontend JavaScript, changed Bash syntax and `git diff --check` pass. Browser screenshots are saved outside the repository as task artifacts.
- Failing evidence was captured before repairing checkbox sizing, visible empty alerts, message-anchor scroll behavior, mobile hidden-navigation focus, and guardians probing the removed homepage. The corresponding regressions now pass.
- Deleted: `hub/templates/`, `hub/static/`, `tests/fixtures/frontend/ssr_sse.mjs`, obsolete SSR-only rendering assertions, and the cutover toggle/catch-all/shared-SSR asset path. Retained task/session/API/security/state-machine tests; source-location expectations follow the extracted modules.
- Delivery is a local commit on `codex/frontend-console`. Backend packaging reads committed HEAD; run the committed archive extraction/API-only boot and independent frontend dependency-closure checks immediately after committing. No merge, push, deployment, external provider call or production state mutation is included.
