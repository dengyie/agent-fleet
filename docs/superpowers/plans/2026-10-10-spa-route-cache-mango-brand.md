# SPA Route Cache and Mango Brand Implementation Plan

> **For agentic workers:** Execute this plan in order. Each step ends with its own focused verification.

**Goal:** Keep route changes inside the existing document, reuse a bounded set of mounted views, preserve same-route history state, and use the canonical Mango brand artwork.

**Architecture:** `app.js` owns route identity, a five-entry LRU view cache, and each view's suspend/resume/dispose lifecycle. Views pause polling and store subscriptions while hidden, then refresh from shared state when resumed; eviction and authentication teardown dispose resources. Static Mango assets use content-fingerprinted filenames so the current no-cache deployment contract remains valid.

**Tech Stack:** Vanilla JavaScript ES modules, existing Flask/pytest acceptance harness, Playwright Chromium, static frontend release packaging.

## Global Constraints

- Session validation remains on the existing one-minute schedule and is not run for route changes.
- Route cache capacity is fixed at five mounted route instances and includes the active route.
- Existing authorization, route mapping, API contracts, and self-hosted sidebar footer behavior remain unchanged.
- Mango artwork comes from the canonical Obsidian brand-icon note and its checked-in copy must match the source asset digest.
- Keep deployment cache headers unchanged; asset filenames carry content fingerprints.

---

### Task 1: Reproduce navigation failures in Chromium

**Files:**
- Modify: `tests/fixtures/frontend/assistant_auth_browser.cjs`

**Interfaces:**
- Consumes: the existing authenticated assistant browser fixture and local Hub test app.
- Produces: assertions for same-route navigation, hash-history draft retention, and route-view reuse.

- [x] **Step 1: Add browser assertions for same-route reload and view preservation**
- [x] **Step 2: Run the focused Chromium test and record the current failure**

Run: `FLEET_PLAYWRIGHT_MODULE=/tmp/agent-fleet-playwright/node_modules/playwright FLEET_BROWSER_CHANNEL=chrome PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /Users/mango/project/codex/agent-fleet/.venv/bin/python -m pytest tests/test_frontend_browser.py::test_assistant_auth_in_real_browser -q --tb=short`

Expected before implementation: the same-route click changes `performance.timeOrigin` and fails the assertion.

### Task 2: Add bounded SPA route-view lifecycle

**Files:**
- Modify: `frontend/assets/app.js`
- Modify: `frontend/assets/views/assistant.js`
- Modify: `frontend/assets/views/fleet.js`
- Modify: `frontend/assets/views/machine.js`
- Modify: `frontend/assets/views/task.js`
- Modify: `frontend/assets/views/session.js`
- Modify: `frontend/assets/views/monitoring.js`

**Interfaces:**
- Consumes: each mount function's existing teardown function.
- Produces: optional `suspend()`, `resume()`, and required idempotent teardown behavior; a five-entry route LRU in the application shell.

- [x] **Step 1: Prevent identical URL reloads and avoid remounting when only the hash/history entry changes**
- [x] **Step 2: Cache views by resolved route and dispose the least-recently-used entry above the five-entry limit**
- [x] **Step 3: Pause assistant timers and shared-store subscriptions while a route is cached but inactive**
- [x] **Step 4: Resume the active view after browser back/forward and page-cache restoration; clear all entries on logout or eviction**
- [x] **Step 5: Scope machine form lookup to its route root so cached machine instances do not collide through document-global IDs**
- [x] **Step 6: Run the focused browser test and frontend contract tests**

### Task 3: Use canonical Mango logo and favicon

**Files:**
- Create: `frontend/assets/brand/mango-ddf462d0.png`
- Create: `frontend/assets/brand/mango-cdd3ec60.ico`
- Modify: `frontend/index.html`
- Modify: `frontend/assets/styles/shell.css`
- Modify: `tests/test_frontend_contracts.py`

**Interfaces:**
- Consumes: Obsidian `Note/Infra/brand-icon.md` assets `logo-192.png` and `favicon.ico`.
- Produces: local, fingerprinted brand references for the login page, console navigation, and browser favicon.

- [x] **Step 1: Copy the canonical source files and verify their MD5 digests**
- [x] **Step 2: Replace icon placeholders with accessible image markup and stable responsive sizing**
- [x] **Step 3: Assert the referenced local artwork is served and matches the canonical digest**
- [x] **Step 4: Run frontend contract tests and the Chromium login/console checks**

### Task 4: Full verification

**Files:**
- Verify: all files above plus the packaged frontend release.

- [x] **Step 1: Run JavaScript syntax checks and the frontend contract/browser suites**
- [x] **Step 2: Run the complete project test suite with journey requirements enabled**
- [x] **Step 3: Package a fresh frontend release and verify the manifest contains both fingerprinted assets**
- [x] **Step 4: Inspect desktop/mobile browser rendering and check for console errors**

Full-suite result: 3146 tests passed, 5 platform skips, and 160 subtests passed. Pytest exits non-zero because the required journey report is 28/29 on macOS: RELEASE-01's three pidfd selectors require Linux. No functional test failed; Linux CI remains the required zero-skip release gate.
