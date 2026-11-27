# Desktop Session Checkpoint Durability Plan

> **For agentic workers:** Execute inline in this worktree. Keep the change limited to the existing ZCode desktop sync checkpoint.

**Goal:** Prevent interrupted ZCode desktop checkpoint updates from corrupting the only recoverable cursor, and give legacy corruption a bounded fail-closed outcome.

**Architecture:** Preserve `sync_desktop`'s existing checkpoint fields and stable message event IDs. Publish the small checkpoint as a private, fsynced sibling file followed by atomic replacement, reusing the node's existing private JSON record writer. If an existing checkpoint is malformed, fail closed and retain it: old clients may have emitted unstable event IDs, so replaying the entire history cannot be proven duplicate-safe.

**Tech Stack:** Python, existing `tools.runner_source.atomic_private_json`, pytest, temporary encrypted spool and Flask/SQLite Hub fixtures.

## Global Constraints

- Modify backend/node capture only; do not change `frontend/` product code or deployment logic.
- Do not run a real Agent CLI, external model, or OAuth flow.
- Preserve the current desktop API, Hub event identity, encrypted spool, and native read-only source boundaries.
- Add a reproducible failure before implementation; use UTF-8 without BOM and run `git diff --check`.

## Task 1: Reproduce interrupted checkpoint corruption

**Files:**
- Modify: `tests/test_review_node_sync.py`
- Inspect: `tools/session/sync.py`

**Interfaces:**
- Keep `sync_desktop(cfg, desktop, row, native_path, send=post, *, budget_s=1, should_stop=...)` unchanged.
- Use the existing test Hub and message bridge fixture; the test must observe one durable user/assistant event after a checkpoint write is interrupted and sync is retried.

- [x] Add deterministic reproductions: malformed old checkpoint state raises an unbounded parser/type error, and replace-failure injection at each cursor publication phase leaves the direct-write design without an atomic publication boundary.
- [x] Run `python3 -m pytest tests/test_review_node_sync.py -k 'damaged_desktop_checkpoint or checkpoint_publish_failure' -q --tb=line`; 11 cases failed against baseline.
- [x] Record the root cause: direct `Path.write_text` truncates the only cursor copy before serialization completes, while the next sync assumes valid JSON.

## Task 2: Publish and recover checkpoints safely

**Files:**
- Modify: `tools/session/sync.py`
- Modify: `tools/runner_source.py`
- Modify: `tests/test_review_node_sync.py`
- Modify: `tests/test_workflow_runner_source.py`

**Interfaces:**
- Add one private `_write_desktop_checkpoint(path: Path, state: dict) -> None` helper that delegates to `tools.runner_source.atomic_private_json`.
- Keep persisted checkpoint keys `cursor`, `seen`, `metadata`, `desktop_bridge`, and `native_cursor` compatible with existing records.

- [x] Replace every direct `.desktop-cursor` `write_text` call in `sync_desktop` and `_sync_zcode_native_tool_events` with `_write_desktop_checkpoint`.
- [x] Treat an absent checkpoint as `{"cursor": null, "seen": []}`. For truncated, invalid JSON, invalid UTF-8, oversized, or structurally invalid existing state, retain the bytes and return the bounded `desktop_checkpoint_invalid` error before querying or overwriting the source cursor.
- [x] Inject an `os.replace` failure at each metadata/message/page/native-cursor publication point after corrupting the private staging file; assert the complete prior checkpoint remains readable, then retry and assert exactly one copy of each message and tool event.
- [x] Verify `atomic_private_json` removes its unique staging file after replace failure while preserving the prior published record.
- [x] Run `python3 -m pytest tests/test_review_node_sync.py tests/test_native_agent_storage.py tests/test_native_transcript_detail.py tests/test_zcode_session_binding.py tests/test_session_bridge.py -q --tb=short`; 95 passed.
- [x] Run the expanded session/capture and private record regression command; 568 passed, 29 skipped, 55 subtests passed.

## Task 3: Review and verification

**Files:**
- Modify: `docs/native-session-api.md` to state the fail-closed local checkpoint recovery behavior.
- Modify: `docs/testing/README.md` to list atomic source-record publication cleanup regression.
- Modify: `.github/workflows/ci.yml` to run the atomic publication regression on Windows and macOS.

- [x] Run targeted Ruff, `git diff --check`, CI YAML parsing, Python compilation, and a UTF-8/no-BOM check for every changed text file.
- [x] Review the complete diff for API/schema/deployment/frontend scope and confirm no new database field or migration was introduced.
- [ ] Rebase onto latest `origin/main` before publication; wait for Linux, Windows, and macOS CI and the production deployment result.
