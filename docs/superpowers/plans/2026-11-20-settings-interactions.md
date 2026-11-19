# Settings interaction polish implementation plan

> Execute inline in the existing isolated worktree, with a failing browser checkpoint before each product fix. Preserve other worktrees and use the latest main as the base.

**Goal:** Make settings dismissal, keyboard navigation, focus return and scrolling behave consistently on desktop and mobile.

**Architecture:** Keep the existing native dialog, route cache and close animation. Pointer gestures and category/search interaction belong to the settings view; the application shell owns the focus target on the background page. Reuse the installed awesome-ui UiIcon implementation; add no dependencies or backend behavior.

**Tech stack:** Native JavaScript/CSS, Flask/SQLite fixture, pytest and installed Playwright/Chrome.

## Task 1: Dismiss the backdrop without accidental dismissal

Files: `frontend/assets/views/settings.js`, `tests/test_settings_browser.py`, `tests/fixtures/frontend/settings_interactions_browser.cjs`.

- [x] Reproduce a real outside click leaving the dialog open on desktop and mobile.
- [x] Record primary pointer-down position, pointer ID and pointer-up position. Close through the existing `requestClose()` only when both ends are outside the current dialog bounds and movement is within click tolerance. Clear pending gestures on cancellation/suspend/dispose.
- [x] Verify content clicks, dialog padding clicks, inside-to-outside selection, outside-to-inside drags, backdrop drags and secondary clicks stay open; normal outside click/tap, Escape and close button dismiss once and preserve the assistant draft.

## Task 2: Preserve keyboard context and return focus

Files: `frontend/assets/app.js`, `frontend/assets/views/settings.js`, same browser fixture.

- [x] Reproduce closing settings opened from the account menu focusing the unrelated sidebar settings link; reproduce category activation moving focus to close.
- [x] Capture the active element when entering settings from a background page. After close/navigation restore it with `preventScroll`, or use the visible navigation toggle/settings entry when the original opener is unavailable.
- [x] On category changes focus the selected category link on desktop and the category select on mobile; preserve ordinary opening and exit animations.
- [x] Verify modal Tab confinement, mobile hidden-opener fallback, cached reentry, direct-link close and cancellation of a pending dismissal when navigating elsewhere.

## Task 3: Search and scroll affordances

Files: `frontend/assets/views/settings.js`, `frontend/assets/styles/settings.css`, same browser fixture.

- [x] Verify and then improve search: visible UiIcon clear button, live empty/result feedback, Enter opens the first match, Escape clears a nonempty query before dismissing, arrows/Home/End move among visible category links. Ignore IME composition and modified key combinations. Keep the clear button separate from the input label.
- [x] Contain overscroll in both scrollable settings regions and provide 44px mobile close/category targets. Check keyboard focus indicators, dark theme, reduced motion and 390px layout.
- [x] Register the new browser selector under SETTINGS-01 and update its acceptance description.

## Task 4: Consistent conversation action menus

Files: `frontend/assets/shell/navigation.js`, same browser fixture.

- [x] Reproduce two conversation menus remaining expanded on desktop and mobile.
- [x] Keep only one menu open, dismiss on outside pointer/focus and before collapsing the sidebar. Escape closes the menu first and restores its trigger without closing the mobile drawer.
- [x] Support ArrowUp/Down from the trigger and Arrow/Home/End inside actions. Tab dismisses the menu. Remove delegated listeners on shell disposal.
- [x] Verify these operations in a temporary real Flask app with two persisted conversations and no submitted model request.

## Verification

- [x] Run the new browser scenarios against original product code and preserve the expected failures.
- [x] Run the same scenarios after fixes, then existing settings save/navigation, motion, lifecycle, account/navigation and frontend separation/contract regressions.
- [x] Run `git diff --check`, inspect UTF-8/no-BOM and review the complete diff. Record actual results and deployment status accurately.

Failure evidence: `/tmp/settings-interactions-before.log` records the original backdrop, focus, search and scroll failures. Its two keyboard-safety failures were overstrict test assumptions about native Tab reaching browser chrome, and were corrected without attributing those failures to product defects. `/tmp/history-menu-interactions-before.log` records both desktop and mobile retaining two expanded conversation menus.

Final verification after separating the clear button from the input label: **235 passed in 166.91s**, including all 15 new interaction scenarios. The run covers `tests/test_settings_browser.py`, `tests/test_settings.py`, `tests/test_workspace_node_setup.py`, `tests/test_account_http.py`, `tests/test_navigation_sessions.py`, `tests/test_account_browser.py::test_account_switch_clears_previous_identity`, `tests/test_frontend_contracts.py`, `tests/test_frontend_separation.py` and `tests/test_journey_gate.py`. Output: `/tmp/settings-interactions-final.log`.

`git diff --check`, Node syntax checks for the three changed JavaScript modules and new browser fixture, and UTF-8/no-BOM checks passed. Screenshots are generated under `/Users/mango/.cache/agent-fleet/verification/settings-interactions-20261120/browser/`.

Publication is authorized by the user's standing project instruction: after implementation and verification, submit to GitHub, merge into the latest main and deploy without waiting for another reminder. Preserve other worktrees, rebase before merging and use the existing CI deployment transaction. Publication and live acceptance evidence will be recorded in the canonical operations runbook after release.

## Full-CI nested-dialog correction

The first PR CI identified a missed parent/child modal boundary: on desktop, a nested registration dialog extends past the settings rectangle, and its bubbled pointer events were mistaken for settings-backdrop clicks. This interrupted MCP and identity registration; nested Escape also needs to stay scoped to its own dialog. Restrict backdrop gestures and cancellation to events targeted at the settings dialog itself, retaining the original geometry/drag checks. Existing MCP registration scenarios now explicitly assert that settings remains open after form interaction, Escape and child close.

The four targeted failures were reproduced locally, then passed after the minimal fix (4 passed in 8.79s). The same run also found the existing view-source contract disallows template interpolation even for safe `textContent` assignments; the search count now uses ordinary string concatenation without changing that contract. Evidence is retained in `nested-before.log`, `nested-after.log` and the first PR CI logs under the private verification directory. The first PR CI result remains failed; a new complete CI run must validate the corrected code before merging.
