# Workspace UI Refresh Implementation Plan

**Goal:** Simplify navigation and the overview, move node management into the assistant work panel, and present settings in an accessible modal without losing the underlying conversation.

**Architecture:** Keep the existing Vanilla JS views, route cache and backend contracts. Reuse Awesome UI Kit icons and the settings catalog form. Settings are a native dialog with explicit suspend/resume lifecycle; the background route stays mounted.

**Tech Stack:** ES modules, CSS, native dialog, Flask, Playwright.

## Constraints

- Work only in `codex/workspace-ui-refresh`; preserve other worktrees.
- UTF-8 without BOM; no additional production dependencies.
- Preserve authentication, account restrictions, deep links, active runs and unsent text.

## Tasks

- [x] Simplify `frontend/index.html` and `assets/shell/navigation.js`: move theme controls above branding, remove node navigation and resizing, add accessible settings/account/logout footer icons. Update sidebar browser checks.
- [x] Add modal layout/lifecycle in `assets/views/settings.js` and `assets/styles/settings.css`; update `assets/app.js` to preserve the previous route behind settings and return to it on close. Verify save/reload, deep links, Escape, focus and mobile layout.
- [x] Add `assets/views/assistant/nodes.js` using the existing `mountCatalog(root, 'nodes')` form; include an admin-only node tab and simplify inspector styling. Verify node configuration remains available and ordinary accounts cannot see it.
- [x] Simplify `assets/views/fleet.js`: compact health summary, node status rows, collapsed session/event details, with live status retained. Scope styles to the overview.
- [x] Run settings desktop/mobile acceptance, assistant/auth browser acceptance, console browser acceptance and frontend contracts; inspect screenshots and run `git diff --check`.

Execution remains in this chat, as already requested by the user.
