# Native Runtime Frontend Adaptation Implementation Plan

> **For agentic workers:** Execute inline with executing-plans; no delegation is required.

**Goal:** Reflect the confirmed public stream and process-cleanup outcomes from backend PR137/138 without adding backend policy to the browser.

**Architecture:** Keep event projection pure and consume existing public Hub events and control receipts. Reuse StreamMarkdown, ToolCallBadge and the current inspector. Only explicit stream termination stops unfinished output; missing tool results remain unknown.

**Tech Stack:** Vanilla JavaScript, Python pytest, Flask/SQLite, installed Chrome through Playwright.

## Global Constraints

- Base: latest main `59e5ef8`; public APIs/schema are unchanged.
- No backend product changes, new dependencies, provider calls or automatic command replay.
- Preserve Hub cursor identity, per-stream message/tool identity and backend-declared permissions.
- All files use UTF-8 without BOM.

### Task 1: Project terminal stream evidence

**Files:** `frontend/assets/views/agent-chat/state.js`, `messages.js`, `tests/test_native_chat_frontend.py`, `tests/fixtures/frontend/native_chat_browser.cjs`.

**Interfaces:** `nativeMessages(events)` consumes validated public events and produces keyed view rows. Interrupted text remains available; unfinished tools become `unknown`. Late results may replace unknown tool status.

- [x] Add failing projection regression: incomplete text plus a tool call, an independent stream and a failed state; assert only the failed stream stops waiting.
- [x] Close incomplete rows only for their own stream on `failed`, `stopped`, `completed` or `session_close`; preserve text and show interruption feedback for unsuccessful termination.
- [x] Exercise incremental text/tool arrival, terminal failure, retained disclosure and late tool result through real Hub HTTP/SQLite and Chrome at desktop/mobile widths.
- [x] Run `PYTHONPATH=. python3 -m pytest tests/test_native_chat_frontend.py -q` with the configured installed Chrome runtime.

### Task 2: Explain confirmed process-control failures

**Files:** `frontend/assets/views/agent-chat/state.js`, `dom.js`, `frontend/assets/views/agent-chat.js`, browser fixtures above, `docs/native-web-frontend.md`.

**Interfaces:** `operationFailure(reason)` translates known public receipt reason codes while preserving the original code. Unknown reasons retain their existing display. Current `allowed_actions` remains the only authority for controls.

- [x] Explain `escape_unverified` and `liveness_unknown` in delivery cards and operation feedback, retain diagnostic codes and avoid any automatic retry.
- [x] Record a real signed failed receipt via the existing fixture and assert the browser displays the explanation on both widths, with exactly one business POST.
- [x] Run native chat, frontend separation, packaging/release and account-picker regressions; review actual diff and update the frontend contract documentation.
- [ ] Rebase latest main before merge, pass CI, merge, follow deployment and verify exact production SHA plus same-run public resources and runtime health.
