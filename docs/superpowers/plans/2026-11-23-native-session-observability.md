# Native Session Observability Implementation Plan

**Goal:** Make the computer Agent directory and chat page reflect the native session name, observed client/model metadata, useful timing, and the full safe transcript event stream.

**Architecture:** Keep vendor parsing in `tools/session/normalization.py` and native storage adapters. Extend the shared session event contract, project metadata into the existing SQLite session repository, expose only the bounded public DTO, then render it through the existing native chat modules. Hub ingestion cursors remain the only event identity.

**Safety boundary:** Display user and assistant-visible text, structured tool calls/results, capture gaps, and lifecycle state. Never surface `thinking`, `reasoning`, system prompts, credentials, raw paths, or unredacted source records. Do not synthesize streaming; preserve actual snapshots/deltas when the source provides them and otherwise display the completed event with truthful capture status.

## Tasks

### 1. Native metadata and persistence

- [x] Extend `session_metadata` and message payload allowlists with bounded native title, client version, provider/model, native timestamps, and message identity/update mode.
- [x] Extract metadata from supported transcript rows (Codex, Claude Code, Pi, and ZCode fields explicitly present in model I/O) without treating unknown fields as public data.
- [x] Add an atomic SQLite migration for public metadata columns and project updates monotonically; keep old databases readable and fail closed on damaged/future schemas.
- [x] Expose title/model/client/timing through the existing session DTO and directory API only.

### 2. Detailed transcript and native chat UI

- [x] Preserve real assistant message updates/deltas from structured sources, keyed by native message identity; retain final complete text and tool evidence without duplication.
- [x] Keep hidden reasoning and raw source records out of the chat projection.
- [x] Prefer native session title in the directory and chat heading; use a visibly grounded first-user-message fallback when the native source has no name.
- [x] Show observed client/version, provider/model, session start, elapsed duration, capture source/quality, and current state in the inspector.
- [x] Render lifecycle activity and precise tool details from the public event stream while keeping Hub-cursor pagination and live updates unchanged.

### 3. Contracts, documentation, and verification

- [x] Add fixture-backed schema, normalization, repository migration, HTTP DTO redaction, API contract, state projection, and browser regressions.
- [x] Update the authoritative session API/conversation docs and `docs/testing/README.md` with source coverage and the streaming/reasoning boundary.
- [x] Run focused session/backend/frontend tests and relevant regression suites; inspect `git diff --check`, encoding, migration compatibility, and changed-file scope. Session suite: 466 passed, 17 skipped, 55 subtests; native-chat Chrome: 18 passed; Ruff and JavaScript syntax checks passed. Full matrix: 8546 passed, 75 skipped, 204 subtests, with one unrelated assistant-auth browser timeout; that exact test passed on three immediate isolated reruns. No cause was reproduced and no assistant-auth code changed.
- [ ] Rebase on the latest `main` before merge and verify the resulting commit through CI/deployment evidence before claiming production availability.

## Acceptance boundaries

- Native titles are used only when the native source actually provides one. The fallback is the first observed user message; the UI does not invent a native title.
- The model shown is the latest model identifier explicitly present in the native transcript, not the Fleet platform model catalog or inferred provider configuration.
- Start/duration values distinguish native source time from first Hub observation when source timestamps are absent.
- Streaming is source-dependent. The UI only shows actual source updates; completed-message-only agents remain completed-message-only.
- No vendor CLI, external model, VM filesystem, or production database is used by automated tests.
