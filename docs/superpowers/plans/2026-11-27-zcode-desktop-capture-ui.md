# ZCode Desktop Capture UI Implementation Plan

> **For agentic workers:** Execute inline using the executing-plans workflow. Keep all changes in the existing isolated frontend worktree.

**Goal:** Reflect the latest ZCode desktop capture contract accurately in the native chat UI and verify its tool records end to end.

**Architecture:** Consume the existing public `agent_evidence.protocol` and tool-event DTOs. Keep transport and backend behavior unchanged; reuse `ToolCallBadge` for tool arguments, results and status. A pure presentation helper describes idle snapshot capture only for a confirmed ZCode desktop bridge.

**Tech Stack:** Vanilla JavaScript, existing Web Components, pytest, Playwright with installed Chrome.

## Global Constraints

- Frontend responsibility only; no backend product changes.
- UTF-8 without BOM; no new dependencies or downloads.
- Preserve the primary worktree and the user's Chrome 9222 session.
- No real Google authorization or model invocation during acceptance.

### Task 1: Expose the actual desktop synchronization behavior

**Files:** `frontend/assets/views/agent-chat/state.js`, `frontend/assets/views/agent-chat.js`, `frontend/assets/styles/agent-chat.css`, `tests/test_native_chat_frontend.py`, `tests/fixtures/frontend/native_chat_browser.cjs`.

**Interfaces:** `nativeCaptureNotice(session)` returns display text or an empty string. It consumes the public session family and observed desktop protocol; it grants no capability.

- [x] Add assertions that confirmed ZCode desktop sessions disclose idle synchronization, while CLI, unknown and other-family sessions do not.
- [x] Add a browser scenario whose tool events come from `read_events('zcode', ...)` against a local SQLite fixture; verify successful Read arguments/results, delayed failing Bash results, retained disclosure state, no duplicates, literal output and no hidden reasoning on desktop/mobile.
- [x] Run the new tests before implementation and record their failure.
- [x] Implement `nativeCaptureNotice` using `session.agent_family === 'zcode'` and an observed `agent_evidence.protocol.name === 'desktop_bridge'`. Render its text in an accessible composer notice so it stays visible when the transcript scrolls, hidden otherwise.
- [x] Re-run the new tests and the native chat/account/settings/frontend release suites.

### Task 2: Review and release the frontend adaptation

**Files:** The files above; existing deployment evidence outside the repository.

- [x] Review the diff and changed call paths, run UTF-8 and `git diff --check`, and verify backend product files are untouched.
- [ ] Rebase onto fresh `origin/main`, submit the frontend PR, verify required checks, and merge.
- [ ] Follow the main production deployment to its final receipt. Compare public resources with that run's artifact, then verify production in the browser without clearing user state.
- [ ] Report exact deployed SHA, checks, frontend changes and remaining real-provider acceptance boundaries.
