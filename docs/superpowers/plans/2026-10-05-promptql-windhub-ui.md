# PromptQL / Windhub Workspace Implementation Plan

> **For agentic workers:** Execute this plan task-by-task in the current isolated worktree. Steps use checkbox syntax for tracking.

**Goal:** Turn Agent Fleet into a focused chat workspace using the layout and interaction patterns observed in PromptQL and Windhub.

**Architecture:** Keep the static ES-module frontend and current API/state ownership. Reuse the existing awesome-ui primitives. Separate navigation, conversation scrolling, composer controls, and an optional inspector; keep all privileged panels behind existing account checks.

**Tech Stack:** HTML, CSS custom properties, native JavaScript modules, awesome-ui Web Components, pytest and Playwright.

## Global Constraints

- Develop on `codex/promptql-windhub-ui`, based on `origin/main` at `0862e3b`; protect the primary checkout.
- No backend protocol changes, fake feature buttons, new build system, or runtime CDN dependencies.
- Keep safe Markdown, IME input, pending submission recovery, workspace binding, cancellation and account permissions.
- Support desktop, 390px mobile, light/dark themes, keyboard navigation and reduced motion.
- Reference deployed DOM/CSS/JS only. Do not commit third-party bundles, conversation contents or account data.

## Reference evidence (2026-10-05)

- PromptQL: requested bot in the authenticated 9222 browser; Mantine AppShell, CSS-module room rows, ProseMirror composer, collapsible sidebar and independent artifact panel. Inspected deployed `index-BywK82SV.js` and `index-wWJ_K0Fw.css`. Sidebar rows use flex, 8px gaps and a narrow selection indicator; panels have subtle borders and independent scrolling.
- Windhub: authenticated `/chat` in the same browser, plus deployed Next.js chunks and CSS. Actual tokens include background `#f8f8f6`, sidebar `#f7f7f5`, primary `#c96442`; sidebar width is `17.96875rem`. Conversation content is centered, user messages have warm muted backgrounds, controls sit beneath the textarea, and history/search stay in the sidebar.
- Adapt these patterns to Fleet. Do not transplant the reference applications or introduce their unrelated product features.

## Task 1: Shell and history navigation

Files: `frontend/index.html`, `frontend/assets/shell/navigation.js`, `frontend/assets/styles/{tokens,shell}.css`.

- [x] Replace blue dashboard chrome with warm neutral surfaces, quieter borders and a dark primary action.
- [x] Add a desktop collapse control preserving the mobile drawer and keyboard focus trap.
- [x] Add accessible history search that filters the loaded 50 conversations locally; retain query across history refreshes and expose loading/empty/error states.
- [x] Persist only sidebar layout preference. Use `try { localStorage.setItem(...) } catch {}` so blocked storage cannot break navigation.
- [x] Validate desktop collapse/reload, mobile focus loop, filtering and SSE node updates with Playwright.

## Task 2: Conversation workspace

Files: `frontend/assets/views/assistant.js`, new `frontend/assets/views/assistant/workspace.js`, `frontend/assets/styles/assistant.css`, `frontend/assets/ui/AutoScrollAnchor.js`.

- [x] Move model/workspace selectors into the existing composer; preserve their DOM identity and request ownership.
- [x] Make messages scroll independently inside a `data-chat-scroll` container, with the composer always reachable.
- [x] Reuse AutoScrollAnchor with an optional nearest scroll container; scroll only the conversation and preserve the user's position when reading history.
- [x] Build inspector tabs for artifacts, execution and context using `role=tablist`, linked tab/panel IDs, roving tabindex, arrows/Home/End, and a close/reopen control.
- [x] Keep existing renderers and permission checks; reveal execution details for failed/unknown runs.
- [x] Refine welcome prompts, message typography, artifact cards and empty states; show the actual conversation title after recovery.
- [x] Validate pending submission recovery, fixed composer, long messages, tab keyboard navigation, mobile inspector, errors and artifacts.

## Task 3: Verification and evidence

Files: `tests/fixtures/frontend/console_browser.cjs`, this plan, `frontend/THIRD_PARTY.md`.

- [x] Extend the existing real browser journey with behavior checks for search, collapse, tabs, independent scroll and mobile overflow.
- [x] Run `FLEET_PLAYWRIGHT_MODULE=/tmp/pr6-review-browser/node_modules/playwright /opt/homebrew/opt/python@3.14/bin/python3.14 -m pytest tests/test_frontend*.py tests/test_review_assistant_submission.py tests/test_session_frontend_contracts.py -q`.
- [x] Inspect fresh desktop/mobile/light/dark screenshots and review `git diff --check`.
- [x] Record results and provide a local preview with synthetic fixture data only.

Baseline: 126 tests passed, including both real Chromium acceptance journeys, before edits.

## Verification result

2026-10-05: **188 passed in 29.83s** using the command in Task 3, including real Chromium acceptance and the pending submission recovery scenarios. New browser assertions cover sidebar persistence/search, keyboard tab navigation, mobile dialog focus, independent message scrolling, first-message titles, and actual artifact preview/download through the temporary Hub. All modified JavaScript modules pass `node --check`; `git diff --check` passes.

Visual inspection completed for fleet overview, assistant welcome/conversation/artifact preview, focused chat, dark theme, 390px mobile chat and mobile inspector. A mobile accessible-name failure was reproduced and fixed with an explicit label on the icon-only inspector toggle; the same browser journey now passes.

Local preview: `http://127.0.0.1:8796/assistant`, bound to loopback with isolated temporary fixture data and a deterministic provider. No production service or remote repository is changed.
