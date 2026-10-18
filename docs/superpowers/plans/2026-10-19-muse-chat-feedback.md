# Muse Chat Feedback Implementation Plan

> **For agentic workers:** Use inline execution in the existing `codex/muse` worktree. Each task ends with a fresh verification command.

**Goal:** Make the Muse workspace feel like the main Agent chat by showing sent user messages immediately, exposing delivery state, and keeping users aware while Muse is thinking or producing a response.

**Architecture:** Keep the current Muse HTTP/history contract and idempotent receipt flow. Add a small view-local delivery state machine in `frontend/assets/views/muse.js`; render transient user and assistant status bubbles through the existing DOM helpers and `UiIcon`, then reconcile them against authoritative history events. No new backend endpoint or browser transport is introduced.

**Tech Stack:** Vanilla ES modules, existing `StreamMarkdown`, `ChatPromptInput`, `AutoScrollAnchor`, `UiIcon`, Playwright fixture, pytest harness.

## Global Constraints

- Preserve `confirmed`, `retryable`, `dispatching`, `unknown`, and `rejected` delivery semantics.
- Never duplicate an authoritative Muse event when a transient bubble is reconciled.
- Keep user input and request text bounded by the existing 30,000 character limit.
- Keep transient status text safe DOM text; do not inject model HTML.
- Do not add a new dependency or change the Muse backend protocol.

---

### Task 1: Add view-local delivery state and transient message renderers

**Files:**
- Modify: `frontend/assets/views/muse.js`
- Modify: `frontend/assets/views/muse/cards.js`
- Modify: `frontend/assets/styles/muse.css`

**Interfaces:**
- `renderMusePendingMessage(state)` returns a safe DOM article with a user bubble, spinner, and delivery label.
- `renderMuseThinkingMessage(state)` returns a safe DOM article with a Muse role label, spinner, and phase text.
- Each pane stores `delivery: {phase, requestId, text, replyTo, startedAt}` and `awaitingReply` independently.

- [x] **Step 1: Add transient render helpers and pane state fields.**
  Use `uiIcon('loader', {size: 15, className: 'animate-spin'})`, text nodes, `data-transient-message`, `data-delivery-phase`, and `aria-live="polite"`; never use `innerHTML` for message text.

- [x] **Step 2: Reconcile transient nodes during `loadHistory`.**
  Keep the optimistic user bubble until a matching user history event is present. Keep the thinking bubble until a later assistant event is present; update its label to `正在输出…` when a streaming/delta event is observed and to `正在执行任务…` when the latest public browser task is running.

- [x] **Step 3: Add CSS for right-aligned user bubbles, status spinner, phase copy, and reduced-motion behavior.**
  Existing message/card layout and mobile breakpoints must remain intact.

### Task 2: Wire send lifecycle to the visible chat state

**Files:**
- Modify: `frontend/assets/views/muse.js`

**Interfaces:**
- `sendMessage(pane)` sets `sending` before the network request, `thinking` after `confirmed`, and terminal state after authoritative history or an explicit receipt outcome.
- Existing uncertain-delivery recovery remains available and must not be treated as success.

- [x] **Step 1: Render the transient user bubble before `api.museSend`.**
- [x] **Step 2: Keep the composer status, send/stop controls, and live region synchronized with the delivery phase.**
- [x] **Step 3: Poll active panes more frequently while `awaitingReply` exists, without starting duplicate requests or leaving timers after disposal/suspension.**
- [x] **Step 4: Clear the composer after confirmed delivery and unlock it after an authoritative assistant response, while preserving the existing retry/unknown recovery paths.**

### Task 3: Add browser regression coverage

**Files:**
- Modify: `tests/fixtures/browser/muse_workspace.cjs`
- Modify: `tests/test_muse_workspace.py` only if the fixture contract needs a backend assertion

**Interfaces:**
- Browser fixture must cover transient user bubble, thinking status, authoritative reconciliation, and the existing unknown-delivery recovery.

- [x] **Step 1: Hold one history response after a confirmed send and assert the user bubble plus `Muse 正在思考…` are visible with a spinner and `aria-live`.**
- [x] **Step 2: Release history and assert the transient nodes disappear, the real user/assistant messages remain, and the composer returns to editable state.**
- [x] **Step 3: Run the focused Muse browser fixture and the existing Muse Python tests.**

### Task 4: Validate source and release artifacts

**Files:**
- No additional source files.

- [x] **Step 1: Run `node --check` on all changed browser fixture files and `git diff --check`.**
- [x] **Step 2: Run focused Muse tests and the frontend contract tests.**
- [x] **Step 3: Inspect the diff for accidental protocol changes, duplicate rendering, or sensitive data exposure.**
