# Native History Discovery Review Implementation Plan

> **For agentic workers:** Execute the bounded tasks in this session using the existing isolated worktree, with evidence before each repair.

**Goal:** Find and repair native-history omissions across the registered Agent families, including the actual Windows Agy layout.

**Architecture:** Keep native discovery, vendor projection and recoverable encrypted publication separate. Reuse RunnerCapture, the existing spool owner and HTTP ingestion; imports confer no control authority. Keep the existing Agy checkpoint format readable while sharing its append-receipt mechanism with Hermes.

**Tech Stack:** Python stdlib, SQLite read-only snapshots, existing encrypted spool, Flask and pytest.

## Global Constraints

- Backend/node only; no frontend product or deployment implementation changes.
- Preserve native databases, settings, credentials and running Agent processes; no model, prompt or OAuth calls.
- UTF-8 without BOM; preserve main worktree and publish only the sanitized source tree.
- Native store data is allowlisted before publication; system/reasoning/auth/private metadata are excluded.
- No inferred Google account, unmanaged resume authority or guessed model names.

### Task 1: Source discovery and Windows Agy

Files: `tools/session/sync.py`, `tests/test_native_history_discovery.py`, `tests/test_agy_native_history.py`.

- [x] Reproduce omission of `~/.gemini/antigravity/conversations` using a real-shaped SQLite fixture and default worker.
- [x] Test the already-supported native environment roots: CODEX_HOME, CLAUDE_CONFIG_DIR, PI_CODING_AGENT_DIR, ZCODE_DATA_BASE_DIR and GEMINI_HOME. Explicit roots replace the corresponding default; do not scan unrelated home directories.
- [x] Support both observed Agy namespaces under the selected Gemini home, resolve/deduplicate sources, and preserve existing native identities and checkpoints.
- [x] Read the three real Windows Agy databases through the same bounded reader, compare schema/field projection, and verify no native bytes changed.

### Task 2: Hermes visible history

Files: `tools/session/hermes_archive.py`, shared SQLite publication module, `tools/session/sync.py`, `tools/session/runner_capture.py`, `tests/test_hermes_native_history.py`.

- [x] Reproduce missing default Hermes history with an actual SQLite/Flask/encrypted-spool fixture.
- [x] Enumerate sessions by indexed immutable session ID in finite pages; read each exact session's messages by indexed row ID in bounded pages.
- [x] Project user/assistant/text/tool fields and explicit native title/model/start time. Exclude system, reasoning, multimodal binary data, rewind-only rows and compressed summary carriers. Preserve compacted visible history and native logical message/tool identity across copied generations.
- [x] Reuse durable pending append receipts before replay/ACK, atomic source checkpoints, stop checks and spool ownership. Corrupt/future checkpoints and unsupported native schemas defer without resetting.
- [x] Test response loss, checkpoint failure after append, concurrent scanners, database/WAL changes, compressed copies, equal repeated user text, large fields, bounds, old stores and SQLite failure.

### Task 3: Review, platform verification and rollout

Files: `docs/native-provider-runtime.md`, `docs/reviews/2026-11-28-native-history-discovery.md`, `docs/testing/README.md`, `docs/testing/journeys.json`, `.github/workflows/ci.yml`.

- [x] Review Codex, Claude, Pi, ZCode, Agy and Hermes source ownership, discovery, normalizer, cursor, upload and metadata behavior; record confirmed fixes and remaining explicit support boundaries with evidence.
- [x] Run targeted/new and related existing tests, actual Windows tests, import/package/architecture gates, Ruff, UTF-8 and `git diff --check`.
- [ ] Rebase latest main in sanitized publication checkout, publish PR, wait for PR/main CI and the existing deployment, then upgrade signed node closures with state preservation and rollback.
- [ ] Verify the original Mac and Windows Agy sessions via production operator HTTP and check relevant Hermes histories against bounded native projections; update existing canonical operational documentation.

## Observed baseline

- Windows Agy 1.5.18 stores three SQLite conversations under `.gemini/antigravity/conversations`; the current source registry only covers `antigravity-cli/conversations`.
- Mac Hermes `state.db` contains 217 sessions / 116125 message rows, while its profile has no independent archive source. Its current native client distinguishes compacted display history from rewind-only rows and stores structured content with a NUL-prefixed JSON marker.
- Environment roots are honored by exact managed continuation resolvers but ignored by independent default archive discovery.
- Codex archived-session moves require identity/cursor compatibility before importing an additional directory; do not blindly create a second identity from a moved file.

## Prepublication evidence

- Initial discovery regressions: 8 confirmed failing path variants; Hermes new fixture: 6 failed; Codex archive move: 1 failed.
- Fresh targeted and related suite: 413 passed, 2 subtests; real Windows: 192 passed, zero skipped/errors. Windows first fixture rename failure is retained separately; direct native-layout setup resolved it.
- All 217 real Hermes sessions passed bounded first-page projection (4157 rows, zero failures). Largest native session traversed 794 pages through temporary encrypted spool/Flask/SQLite HTTP; three sessions total 39771 events, native hashes unchanged. Final-code rerun is retained privately.
- Windows installed Agy 1.5.18: all three actual databases read, native title present and hashes unchanged, 6 user / 5 assistant events, zero Agent calls.
- Ruff critical/import checks, diff whitespace and UTF-8 without BOM passed.

## Production acceptance findings added to this repair

- Live Mac archive quota was occupied by 499 fully ACKed sealed tails (14712586 bytes) plus retained unacknowledged ZCode frames. Failing close/reopen regression proved the tail was sealed after the last compaction. Close now compacts only after a durable checkpoint and sealing; uncertain ACKs retain their frames.
- Older ZCode stable event retries appeared at a new source sequence (original1237/retry1490), leaving the ACK permanently at1489 although later events existed. Four fail-first repository/HTTP tests prove the gap. Additive atomic delivery_receipts_schema1 records verified identical delivery aliases; payload/machine/stream conflicts fail closed and public event/cursor identity is unchanged.
- Final related regression including receipt migration: 452 passed, 6 subtests; actual Windows receipt and source suites are rerun on this exact code.
