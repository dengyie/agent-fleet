# Visible Agent Execution Details Implementation Plan

> **For agentic workers:** Execute inline using executing-plans. No delegation is required.

**Goal:** Display actual public tool inputs/results and readable execution updates in Agent conversations, preserving live interactions.

**Architecture:** Reuse the existing ToolCallBadge and the awesome-ui ThinkingBlock disclosure pattern. Keep public event projection pure, preserve stream/cursor identity and source order, and never extract private reasoning or source records. Main assistant phases remain public status; public narration stays visible.

**Tech Stack:** Vanilla JS/Web Components, project CSS tokens, Flask/SQLite and Chrome/Playwright browser fixtures.

## Global Constraints

- Base: latest main `2a1e7f559f7b71a83482330960978335d45ce0ed`.
- Frontend-only product changes; no API/schema, provider requests, OAuth, new dependencies or fabricated thought text.
- Maintain backend-declared permissions, receipt semantics, message identity, input focus and expansion state.
- UTF-8 without BOM. Verify/rebase latest main before merge; deploy and independently inspect actual production resources and runtime.

### Task 1: Readable tool details

**Files:** `frontend/assets/ui/ToolCallBadge.js`, `styles/components.css`, `views/agent-chat/messages.js`, `views/assistant/progress.js`; `tests/fixtures/frontend/native_chat_browser.cjs`, `live_feedback_browser.cjs`.

**Interface:** Existing `name`, `label`, `status`, `args`, `output`, `error`; optional `default-open` attribute expands transcript cards on first mount. Inspector cards retain their existing default. Data updates reuse button/pre nodes and never overwrite a user toggle.

- [x] Record failing Chrome evidence: a native tool with public arguments/output must expose its details immediately; streaming updates retain the same button, focus, selected text and manual collapsed state.
- [x] Implement stable DOM rendering, show input/output/error sections and explicit waiting/absent evidence, and readable wrapping/scrolling at 390/1440px.
- [x] Verify pending→success, failure, empty result, literal HTML, late results and manual toggles using actual public fixtures.

### Task 2: Public activity disclosures

**Files:** `frontend/assets/ui/ThinkingBlock.js`, `views/agent-chat/state.js`, `messages.js`, `views/assistant/progress.js`, `styles/components.css`, `assets/app.js`; tests above and `tests/test_native_chat_frontend.py`.

**Interface:** `<thinking-block content title is-thinking>` is a stable, keyboard-accessible disclosure. Consecutive native activity rows group only within one stream and never cross message/tool boundaries. Main assistant current public phase and retry metadata use the same component; raw private reasoning remains unavailable.

- [x] Test native grouping chronology, independent streams, absent private reasoning and completion/unknown semantics.
- [x] Display public activity summaries and native reason labels in source order. Default open and preserve manual state during content updates; stable button focus and body scroll.
- [x] Verify real native and main assistant streaming fixtures at desktop/mobile widths, including failed/unknown outcomes and reload.

### Task 3: Review and release

**Files:** `docs/native-web-frontend.md`, this plan; existing frontend release scripts remain unchanged.

- [ ] Run native chat, assistant browser/live feedback, account-picker, settings, frontend separation and packaging regressions. Review actual diff; validate syntax/encoding.
- [ ] Commit focused frontend changes, rebase latest main, pass PR CI, merge and follow main deployment.
- [ ] Compare same-run public resources, exact runtime SHA and authenticated production pages. Record external model/OAuth acceptance as not run.

Local evidence: `/tmp/fleet-tool-details-red.log` records the original visibility failure. Targeted native/main streaming checks passed. The broad regression first found a Chrome CSS serialization assertion and then an account-recovery fixture that read counters before the recovery response; both test assertions now verify actual behavior/response boundaries. Final integrated verification is required after rebase.

Integration base rebased to main `5f10797fa0facd7fdb3988814d8434f51b73ba65` (AGY native archive collection; no changed browser API). The first PR macOS run exposed an existing ACP fixture race: pending.json becomes readable before publish releases its lock. The fixture now waits for publish to return before answering; lifecycle/interaction/transport regressions: 74 passed. A local page navigation exceeded the fixture-wide 5s action timeout; navigation now has its own 30s budget while element/action waits retain 5s. No backend product code was changed.
