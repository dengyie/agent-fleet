# Native capture reliability implementation plan

> **For agentic workers:** Use `executing-plans` inline, task by task. No delegation is needed.

**Goal:** Prevent native desktop checkpoints from acknowledging tool/message evidence before it is terminal and durably captured.

**Architecture:** Keep the existing desktop bridge, read-only SQLite projector, encrypted spool and Hub event identity boundary. Make source acknowledgement conditional on persistence, retaining stable opaque IDs for desktop retries. Preserve the integer native row cursor and bounded reads.

**Tech Stack:** Python, SQLite, Flask, pytest, existing cross-platform CI.

## Global constraints

- Backend/node capture only; no `frontend/` product changes or deployment-logic changes.
- No real Agent/model execution or OAuth login.
- UTF-8 without BOM; no vendor-store writes in production code.
- Start with failing regressions; preserve default live-capture failure isolation.
- Rebase onto latest main before merging; require CI and deployment evidence.

## Task 1: Desktop persistence acknowledgement

**Files:** `tools/session/bridge.py` (persistence outcome), `tools/session/sync.py` (checkpoint policy), `tests/test_review_node_sync.py`, `tests/test_session_bridge.py`.

**Interfaces:** Existing `SessionBridge.ingest_one(raw, *, source=None)` stays compatible; an opt-in strict persistence check must detect rejected projected events even when another event from the same input was accepted. Desktop messages carry a stable opaque event ID bound to native session, message ID and projected body; the Hub remains the public identity authority.

- [x] Reproduce using the real encrypted spool and Flask/SQLite Hub, injecting `SpoolError` only after source-record persistence. Assert failed metadata is retried; a failed message cannot enter `seen`; accepted prefix and retry produce one public message each.
- [x] Run `python3 -m pytest tests/test_review_node_sync.py -k persistence -q`; all nine baseline cases failed, including checkpoint-loss duplication.
- [x] Add strict acknowledgement at the bridge persistence boundary, counting quota-gap outcomes as rejection while preserving default capture failure isolation.
- [x] Use strict ingestion before desktop checkpoint mutation and derive a stable opaque ID from native session, message ID and projected content.
- [x] Re-run the same failures and bridge regressions: 63 passed; the full related suite passed 156 tests and 3 subtests.

## Task 2: Mutable native tool rows

**Files:** `tools/session/native_storage.py` (snapshot cursor), `tests/test_native_agent_storage.py`, `tests/test_review_node_sync.py`.

**Interfaces:** Preserve `read_events(family, path, token, cursor, *, limit=MAX_ROWS) -> (events, int)`, row/byte bounds and deterministic IDs. A visible pending/running tool is a nonterminal boundary: do not acknowledge it or later rows until its in-place completion. Hidden/synthetic rows remain excluded and do not pin progress.

- [x] Create a real SQLite store with a visible running tool and later completed tool; baseline returned the later tool and cursor 2 while the first remained unobserved.
- [x] Verify the SQLite regression failed before the cursor barrier was added.
- [x] Stop before advancing the cursor on visible pending/running tools; keep other families unchanged and bounded failures intact.
- [x] Verify completed/error outcomes and model-only exclusion with real SQLite. Document that an indefinitely nonterminal row holds tool history without guessing its outcome.

## Task 3: Contract, verification and publication

**Files:** `docs/native-session-api.md`, `docs/testing/README.md`; CI test membership only if a relevant platform suite is missing.

- [x] Document durable acknowledgement and nonterminal barriers. No public API or database migration.
- [x] Run the related suite: `156 passed, 3 subtests passed`.
- [x] Run Ruff (passed), `git diff --check` (passed), and a UTF-8/BOM check (9 changed paths passed).
- [ ] Inspect the complete diff for scope, commit, fetch/rebase latest main, push and create a PR. Wait for Linux/Windows/macOS checks; fix any failure before merge.
- [ ] Merge authorized changes, wait for main deployment, and verify receiver SHA/readiness and signed client delivery without running an Agent. Record any unresolved live-machine boundary separately.
