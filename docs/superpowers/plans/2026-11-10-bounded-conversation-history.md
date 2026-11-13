# Bounded Conversation History Implementation Plan

> **For agentic workers:** Use executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Bound provider conversation input and HTTP/UI history while preserving durable messages, exact idempotency and complete Workflow inputs.

**Architecture:** Reuse indexed `turn_sequence` for recent contiguous turn suffixes and keyset pages. Provider projection limits 32 user turns and 65536 UTF-8 content bytes, retaining the current trigger and complete earlier user/reply groups. UI loads recent 20-turn pages and can prepend earlier pages; immutable Workflow system inputs remain outside this conversation-history budget.

**Tech Stack:** Python 3.10+, Flask, SQLite, static JavaScript, pytest and installed Chromium.

## Global Constraints

- Existing `codex/functional-root-fixes` worktree; preserve concurrent Workflow, provider, node and retention changes.
- Owner-scoped queries, original turn tokens and successful Run reply projection remain authoritative.
- No automatic summary, truncation of message content, deletion, deployment, downloads, merge or push.
- UTF-8 without BOM; tests use `PYTHONPATH=. /usr/local/bin/python3`.
- This slice links to [development completion](../../platform/development-completion.md); it does not complete the project.

## Task 1: Prove unbounded history and retry coupling

**Files:** Create `tests/test_platform_conversation_history.py`.

- [x] Seed 60 real SQLite turns with CJK replies; assert trigger projection is a <=64KiB recent complete-turn suffix and excludes future turns.
- [x] Assert default HTTP detail returns 20 turns, keyset pages cover all turns exactly once, and every page contains only its own Run/request metadata.
- [x] Retry an old token beyond the visible page after model disable; require the original immutable Run and reject changed content.
- [x] Run the tests before implementation and retain red XML.

## Task 2: Bound repository and service inputs

**Files:** Modify `hub/infrastructure/platform_db_conversations.py`, `hub/application/conversation_service.py`, `hub/http/conversation_routes.py`.

- [x] Add metadata-only turn lookup and indexed prior-token lookup; remove full conversation reads from submission.
- [x] Bound trigger projection by recent count and UTF-8 byte sizes before reading content; keep complete contiguous groups.
- [x] Add `before_sequence` and `limit` to conversation detail; page Run IDs constrain request metadata reads through the existing helper's `run_ids` argument.
- [x] Return `history` cursor/has_more and context budget metadata; leave stored content and Workflow injection intact.
- [x] Rerun repository/HTTP/idempotency/worker/native-Workflow regression groups.

## Task 3: Load earlier messages in the existing assistant

**Files:** Modify `frontend/assets/api/platform.js`, `frontend/assets/views/assistant.js`; create browser fixture and real HTTP browser test.

- [x] Add bounded GET query arguments to the existing wrapper.
- [x] Prepend earlier pages with deduplication, preserve loaded history when latest messages refresh, and fence disposed requests.
- [x] Show loading/retry state, history exhaustion and explicit model-history budget without a fabricated summary.
- [x] Verify actual Chromium loading, reload recovery, request cursor behavior and existing assistant contracts.

## Task 4: Record evidence

- [x] Run focused fresh regression and source/import/UTF-8 checks.
- [x] Update this plan and report exact evidence to parent for canonical ledger/journeys update.

## Fresh target-worktree evidence

- Red: `/tmp/agent-fleet-conversation-history-red.xml`: 6 failed / 1 passed. The 60-turn fixture sent 263533 UTF-8 bytes through the original trigger path, returned the complete history and ignored pagination boundaries.
- Original associated conversation/worker/scheduler/native Memory/text/recovery/frontend contracts: `/tmp/agent-fleet-history-regressions.xml`, 382 passed / 33.18s.
- Persistence/request-summary/runtime/HTTP group: `/tmp/agent-fleet-history-persistence.xml`, 79 passed / 1 optional-browser skip / 7.52s.
- Final installed-Chrome group: `/tmp/agent-fleet-conversation-history-final.xml`, 207 passed / 0 skipped / 121.51s. Includes all 10 new history tests, actual earlier-page UI/reload/new-terminal merge, existing assistant/account/Workflow browser scenarios, complete native Workflow inputs and original request-summary retention.
- An earlier browser run had 27 passed / 1 failure because its old metadata fixture defaulted to an absent Playwright Chromium executable. Explicit `FLEET_BROWSER_CHANNEL=chrome` used the already installed browser; the final group includes that exact scenario. No browser download or changed skip policy.
- Syntax, selected Ruff fatal/undefined/unused checks, UTF-8/no-BOM and `git diff --check` passed.
- On the same 60-turn fixture, the bounded trigger input is 64761 UTF-8 bytes / 29 messages, selected as complete recent turn pairs plus the current trigger. One local read measured 0.841ms and 76763B Python traced peak; default HTTP detail contains 20 user turns / 166563 UTF-8 JSON bytes. These are scoped local measurements, not overall provider throughput or capacity claims.

The original messages, replies and private Workflow input payloads remain durable and complete. The response's context metadata describes only the conversation-history budget, without inventing a summary. The parent integrates this slice into the overall ledger/journeys and final full-repository evidence.
