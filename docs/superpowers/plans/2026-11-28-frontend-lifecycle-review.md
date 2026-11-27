# Frontend Lifecycle Review Implementation Plan

> **For agentic workers:** Execute inline with evidence-driven-bugfix and verification-before-completion. Keep the existing frontend/service/API boundaries.

**Goal:** Review all frontend feature surfaces and fix reproduced state/lifecycle bugs, then deploy and independently verify the exact release.

**Architecture:** Route composition owns cached DOM attachment; views own drafts, selection and disclosure state; transport owns connection lifetimes. Late results must belong to the current request before publishing. Reuse existing awesome-ui components without adding dependencies.

**Tech Stack:** Static ES modules, native Web Components, Flask loopback fixtures, installed Chrome/Playwright, Node contract fixtures.

## Global Constraints

- Frontend product changes only; backend API, schema and client execution remain unchanged.
- UTF-8 without BOM. Preserve unrelated worktrees, user browser tabs, login and cache.
- Base on latest main; rebase before merge. Required CI and independent public resource verification precede deployment claims.
- Expose only public execution information supplied by the backend.

## Review scope

Inspect composition/routing/login, HTTP/SSE/store, assistant/native conversations and inspector results, node/target/account pickers, settings catalogs/runtime/Google accounts, integrations/identities/workflows/plugins, directory/search/usage/monitoring/Muse, shared components, mobile CSS, and packaging/cache contracts. Report only reachable defects with reproducible evidence; file size alone does not justify restructuring.

### Task 1: Preserve target selection across refresh

Files: `frontend/assets/views/conversation-target.js`, `tests/fixtures/frontend/workspace_browser.cjs`, `tests/test_frontend_browser.py`.

- [x] Add a browser scenario selecting a non-first project and Agent, refresh targets, and assert both selections remain. Remove the selected project and assert submission remains blocked until explicit reselection.
- [x] Run the scenario against the baseline and preserve the failure.
- [x] Keep a machine-bound selection independent of temporary empty selects. Restore that selection after loading; represent missing selections with an empty option instead of choosing another target.
- [x] Re-run the scenario and existing native target/account picker scenarios.

### Task 2: Preserve conversation reading position through cached routes

Files: `frontend/assets/app.js`, the workspace browser fixture and test above.

- [x] Add desktop/mobile browser navigation away from a long conversation and back, asserting the same scroll offset and disabled bottom-follow when reading history.
- [x] Record baseline failure.
- [x] Capture `[data-chat-scroll]` offsets before hiding/removing a cached route. Restore after attachment, before resuming polling, and notify the existing AutoScrollAnchor through the scroll event.
- [x] Re-run with existing modal, streaming, cached-route and native chat tests.
- [x] Extend the same regression to a valid identity recheck; reproduce offsets lost when the shell is hidden first, then capture before hiding and restore layout after successful verification before resuming the view.

### Task 3: Keep artifact disclosure owned by the user

Files: `frontend/assets/views/assistant.js`, workspace fixture and test.

- [x] Hold preview responses, close/reopen while loading, and assert one request. Close before successful or failed resolution and assert the preview stays closed.
- [x] Record baseline failures.
- [x] Update only data/loading/error on completion, retain the current open flag, reuse pending/cached previews, and ignore responses whose workspace/request was replaced or disposed.
- [x] Re-run the scenario and artifact contracts.

### Task 4: Reject stale connection callbacks

Files: `frontend/assets/realtime/sse.js`, `tests/test_frontend_contracts.py`.

- [x] Retain callbacks/promises from a replaced EventSource or stopped fallback poll. Deliver old events, status and 401 responses; assert they cannot modify the store, trigger authentication, or stop the replacement poll.
- [x] Record baseline failures.
- [x] Check source ownership before event handling and poll controller ownership/abort before response handling and publication.
- [x] Run all executable frontend contracts.

### Task 5: Restore login destinations for every supported page

Files: `frontend/assets/shell/operator.js`, `tests/fixtures/frontend/assistant_auth_browser.cjs`.

- [x] Extend anonymous-navigation browser coverage to usage, Muse, plugins and session search; record that `/usage` incorrectly becomes `/assistant`.
- [x] Include existing supported routes in the same-origin login return allowlist; retain rejection of external URLs and `/login` loops.
- [x] Re-run the full browser authentication/identity/draft recovery scenario.

### Task 6: Review, release and production verification

- [x] Finish scope review, document confirmed issues and concrete test coverage in `docs/native-web-frontend.md`; delete only verified obsolete code in scope.
- [x] Run the integrated frontend/browser/settings/account/release tests, syntax, UTF-8 and diff checks.
- [ ] Rebase latest main, push a focused PR, pass all required checks, merge, and follow main CI through deployment.
- [ ] Download same-run artifacts under 10 MB, verify every public release resource, deployed runtime, authenticated desktop/mobile UI, and normal reload with retained old cache. No real model/OAuth writes required for this lifecycle change.
- [ ] Update canonical ops evidence with exact release SHA, test results and verification boundaries.

Local final validation: 345 integrated frontend/browser/settings/account/release tests passed; 113 targeted authentication/scroll/contracts tests passed (overlapping coverage, not additive). JavaScript syntax, Python syntax, UTF-8 without BOM and git diff checks passed.
