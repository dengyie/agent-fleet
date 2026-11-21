# Native Session HTTP API Implementation Plan

> **For agentic workers:** Implement inline in this session. The repository's isolated worktree is already active.

**Goal:** Add durable ordinary-message idempotency, bounded transcript tail/reverse pagination, and stable session-directory pagination while preserving existing operator and Agent control boundaries.

**Architecture:** Keep HTTP parsing in `hub/http/session_routes.py`, admission and projection in `SessionService`, metadata and transcript queries behind their existing repository ports, and command/token atomicity inside the existing Supervisor SQLite outbox transaction. Use the existing Hub ingestion cursor for transcript identity and an immutable per-session directory cursor for session pages. Interaction answers continue using `interaction_requests` unchanged.

**Tech Stack:** Flask, application/domain/repository layers, SQLite WAL transactions, pytest integration tests, existing public session DTOs.

## Global Constraints

- Do not modify `frontend/` product code, publishing/deployment logic, or run Agent CLIs or external models.
- Keep browser authorization on the existing operator/account administrator identity; machine, runner, and ingest credentials do not authorize browser operators.
- Keep all responses bounded and secret-free; use existing application errors and public DTO projections.
- Additive SQLite migrations must be transactional, idempotent, compatible with old stores, and fail closed on damaged stores.
- Update the authoritative session API documentation and `docs/testing/README.md`.
- Rebase onto the latest `main` before merge.

## File Responsibilities

- `hub/application/session_service.py`: validate optional ordinary-message tokens, normalize and hash the binding, recover the original command/status, and orchestrate tail/before/after page use cases.
- `hub/application/supervisor_service.py` and `hub/application/adoption_service.py`: carry the ordinary-message binding through existing authorization and command enqueue paths.
- `hub/infrastructure/supervisor_command_repository.py`: atomically persist the token binding with the signed command and read the bounded current operation status.
- `hub/infrastructure/transcript_repository.py`: provide bounded ingestion-cursor tail and before-cursor reads without source-sequence identity.
- `hub/infrastructure/session_repository.py` and `hub/domain/session_ports.py`: add an immutable, migrated session-directory cursor and stable snapshot/keyset page query.
- `hub/http/session_routes.py`: preserve the existing request shapes while parsing mutually exclusive bounded event pagination parameters and optional directory cursor.
- `tests/test_native_session_api.py`: exercise HTTP, operator identity, command queue, race, migration, cursor, and redaction behavior with real temporary SQLite repositories.
- `docs/session-conversations.md`: document session message and paging contracts; `docs/testing/README.md`: add the focused test entry point.

## Tasks

### Task 1: Ordinary message idempotency

- [x] Add HTTP tests for same-token replay, concurrent replay, changed operator/session/body conflicts, invalid/oversized token and body, response-loss recovery, revoked-session behavior, terminal status recovery, and SQLite failure.
- [x] Add the Supervisor outbox table and an atomic `insert(..., message_binding=...)` path. Store only a token digest, operator/session/action binding, normalized-body digest, and command ID. Return a conflict before exposing any original command when a binding differs.
- [x] Thread the binding through the existing managed-session and adopted-session enqueue paths; preserve `answer_interaction` and its request/token reservation protocol unchanged.
- [x] Make exact replay read the existing command's bounded current status without enqueueing. Keep the legacy `{text}` request accepted when `client_token` is absent.
- [x] Run the focused message tests and existing interaction/adoption/supervisor suites.

### Task 2: Transcript event paging

- [x] Add tests for latest tail, repeated before-cursor pages, after-cursor live continuation, equal source sequences, empty history, malformed/negative/overflow/future/unknown cursors, concurrent ingestion, and bounded limits.
- [x] Extend the transcript repository's existing cursor query with tail and exclusive before reads. Tail pages are returned oldest-to-newest; `next_cursor` remains the live after-cursor. Missing/deleted cursor positions are deterministic boundaries, while cursors beyond the global high-water mark fail with `invalid_cursor`.
- [x] Extend `SessionService` and the Flask adapter with mutually exclusive `tail`, `before_cursor`, and `after_cursor` modes; preserve `at_sequence` deep links and old after-only calls.
- [x] Run transcript, HTTP session, and assistant tool cursor tests.

### Task 3: Stable session-directory pages

- [x] Add repository and HTTP tests for old-store migration, equal timestamps, stable key order, concurrent inserts/updates, machine-filter binding, invalid/foreign cursors, and pagination across more than one page.
- [x] Add a `session_directory` mapping with an autoincrement cursor, backfill existing session IDs in deterministic timestamp/session-ID order inside the schema migration transaction, and add the mapping atomically for new sessions.
- [x] Add a bounded keyset page query frozen at the first page's maximum directory cursor. Bind the opaque cursor to the machine filter and reject mismatched or malformed cursors. Newly inserted sessions appear after restarting the first page; updates never reorder an active traversal.
- [x] Preserve the existing unpaginated assistant/domain list call while making the HTTP list response expose `has_more` and `next_cursor`.
- [x] Run session repository, service, and HTTP listing tests.

### Task 4: Documentation and verification

- [x] Document exact request/response examples, conflict/status semantics, the optional-token compatibility path, event cursor rules, directory snapshot rules, and clipping behavior.
- [x] Add the new focused pytest command to `docs/testing/README.md`.
- [x] Run focused new and existing tests, relevant broader session/control tests, `git diff --check`, UTF-8-without-BOM checks, and an additive-migration audit.
- [ ] Rebase onto the latest `origin/main`, rerun the required gates on the rebased tree, then publish and merge through the repository's existing checks.

## Verification

- Focused HTTP/SQLite/control suite: `PYTHONPATH=. python3 -m pytest tests/test_native_session_api.py tests/test_agent_lifecycle.py tests/test_session_repository.py tests/test_transcript_repository.py tests/test_transcript_cursors.py tests/test_session_query_api.py tests/test_session_interactions.py tests/test_adoption_control_http.py tests/test_adoption_control_guard.py tests/test_supervisor_control.py -q` — 283 passed, 10 subtests passed.
- Task API compatibility: `PYTHONPATH=. python3 -m pytest tests/test_native_task_api.py tests/test_task_api.py -q` — 13 passed.
- Full local suite with the existing Playwright module and installed Chrome: `FLEET_PLAYWRIGHT_MODULE=/tmp/fleet-browser/node_modules/playwright FLEET_BROWSER_CHANNEL=chrome PYTHONPATH=. python3 -m pytest tests -q` — 8517 passed, 75 platform/environment skips, 204 subtests passed. This run preceded the final `at_sequence` conflict guard; the focused suite above includes that final route behavior.
- Static and repository checks: Ruff CI lint command passed; `git diff --check` passed; no UTF-8 BOM found in changed files. SQLite migration tests cover transactional rollback, old-store preservation, damaged metadata, and future-version refusal.
- PR #109 compatibility was checked against its current HTTP client: legacy text sends, interaction-answer fields, zero `after_cursor` deep links, and after-cursor polling remain supported. The frontend does not yet consume normal-message token recovery, tail/before paging, or later session-directory pages.

## Compatibility Findings

- PR #110's `GET /api/machines/<machine>/task-targets` and `POST /api/tasks` contracts are already on `main` and match PR #109's calls; do not reimplement them.
- PR #109 currently sends `{text}` for ordinary messages, so the new token remains optional and legacy calls keep working. Its current client does not persist or send a normal-message token, so it will not use response-loss recovery until a later frontend integration.
- PR #109 currently sends `{request_id, client_token, selected/text}` for interaction answers; this protocol remains separate and unchanged.
- PR #109's event parser accepts the existing `after_cursor`/`next_cursor` response and will keep reading incrementally. It does not yet issue tail/before requests.
- PR #109's session-list parser currently drops pagination metadata; existing first-page behavior remains compatible, while consuming later directory pages requires a frontend parser update.
