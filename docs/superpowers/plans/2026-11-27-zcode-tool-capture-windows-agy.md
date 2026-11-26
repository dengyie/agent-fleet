# ZCode Tool Capture and Windows AGY Deployment Plan

> **For agentic workers:** Execute each task in order and keep its verification result with the task evidence.

**Goal:** Persist bounded ZCode desktop tool calls/results in native session detail and install the official AGY CLI on the registered Windows Agent Fleet node.

**Architecture:** Extend the existing ZCode desktop archive sync to read tool evidence from the exact session's read-only SQLite rows through `tools.session.native_storage.read_events`; retain its Hub event and node spool boundaries. Use the existing Windows Fleet node and Google's official AGY PowerShell installer with the release manifest's SHA-512 verification. Internal reasoning remains excluded by the public session contract.

**Tech Stack:** Python 3.10+, Flask test client, SQLite read-only URI, Windows PowerShell, official AGY CLI installer.

## Global Constraints

- Do not change `frontend/` product files.
- Do not upload private reasoning/thinking, system prompts, credentials, arbitrary native fields, or database paths.
- Keep exact native session identity, bounded SQLite reads, redaction, encrypted spool, Hub ingestion cursors, and existing operator authorization.
- Install AGY on registered Windows node `home-win` only; do not log in, create accounts, or call an external model.
- Keep all source files UTF-8 without BOM and preserve unrelated work.

---

### Task 1: Reproduce and test the ZCode desktop tool-event omission

**Files:**
- Modify: `tests/test_review_node_sync.py`
- Read: `tools/session/native_storage.py`
- Read: `tools/session/sync.py`

**Interfaces:**
- Uses `sync_desktop(cfg, desktop, row, native_path, send)` and the existing bounded `read_events('zcode', db_path, native_session_id, cursor)` adapter.
- Test fixture supplies the exact ZCode `session`, `message`, and `part` SQLite tables and a desktop messages page containing only public user/assistant text.

- [x] Add a real Flask/SQLite regression that stores a completed ZCode tool part for the bound session, runs `sync_desktop`, then reads `/api/sessions/<id>/events?include_source=0` and asserts tool calls/results with matching call IDs.
- [x] Assert the fixture's reasoning part and any source path are absent from the public events.
- [x] Re-run the new test before implementation; it failed because the persisted tool-event count was zero.

Run: `python3 -m pytest tests/test_review_node_sync.py -k zcode_desktop_tool_capture -q`

Expected before the fix: failure reporting missing `tool_call` and `tool_result`.

### Task 2: Capture native ZCode tool parts with replay-safe identity

**Files:**
- Modify: `tools/session/sync.py`
- Modify: `tests/test_review_node_sync.py`

**Interfaces:**
- Add a private helper `_sync_zcode_native_tool_events(capture, state, checkpoint, native_id) -> None`.
- Extend `read_events(..., *, limit=MAX_ROWS)` with a bounded row-page option while preserving the existing default overflow behavior for managed captures.
- The helper reads from `database_path('zcode', os.environ)` and requests at most 256 native rows after `state.get('native_cursor', 0)` per idle scan.
- Only `tool_call` and `tool_result` are forwarded; ordinary assistant text remains owned by the desktop message API.

- [x] Compute each forwarded event ID from SHA-256 over the family, exact native session ID, stable SQLite row identity, event position, and canonical projected event; persist the `native_cursor` after page events are accepted into the encrypted spool.
- [x] Call the helper only after the desktop session reports `idle`; on an optional native-store read failure, retain the prior cursor and still flush already captured desktop messages.
- [x] Add a replay test that resets the saved native cursor and verifies Hub storage still contains each tool call/result pair once.
- [x] Add a later-event test that inserts another SQLite tool part after the saved cursor and verifies the next sync captures it once.
- [x] Add a native-storage page test showing `limit=1` returns the first row and cursor, then the next call returns the next row without overlap.

Run: `python3 -m pytest tests/test_review_node_sync.py tests/test_native_agent_storage.py -q`

Expected: all ZCode desktop sync and native-storage tests pass; repeated synchronization does not duplicate persisted tool events.

### Task 3: Update the capture contract and test entry point

**Files:**
- Modify: `docs/agent-observability.md`
- Modify: `docs/native-session-api.md`
- Modify: `docs/testing/README.md`
- Modify: `docs/testing/agent-platform-acceptance.md`

**Interfaces:**
- Public API DTO and event kinds remain unchanged.
- Document ZCode desktop tool capture as exact-session, bounded, idle-snapshot SQLite evidence; keep hidden reasoning marked unsupported.

- [x] Record the ZCode desktop native-tool capture source, cursor/replay behavior, read-only boundary, and the fact that partial/unsupported store versions do not imply complete history.
- [x] Add `tests/test_review_node_sync.py` and `tests/test_native_agent_storage.py` to the session capture test entry.
- [x] Record Windows AGY installation evidence only after the node reported the installed version and executable integrity check; redacted receipt: `/Users/mango/.cache/agent-fleet/verification/windows-agy-20261127/installation.json`.

Run: `git diff --check` and verify all modified files decode as UTF-8 without BOM.

### Task 4: Install and verify Windows AGY

**Files:**
- Remote node only: `%LOCALAPPDATA%\agy\bin\agy.exe` and the user PATH managed by the official installer.
- Record evidence in: `/Users/mango/.cache/agent-fleet/verification/windows-agy-20261127/`.

**Interfaces:**
- Target: `home-win`, confirmed to have the Fleet client task set and no existing `agy.exe`.
- Official release manifest: `https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/windows_amd64.json`.
- Official installer: `https://antigravity.google/cli/install.ps1`.

- [x] Inspect the official installer (SHA-256 `51c2cb4f…805344`), confirm Windows AMD64 and manifest version `1.5.18`, then verify the downloaded binary against its advertised SHA-512 before installation.
- [x] Install under `%LOCALAPPDATA%\agy\bin` without authenticating or invoking an Agent task; the binary SHA-512 matches and `agy.exe --version` returns `1.5.18`.
- [ ] Confirm the Fleet node observes `agy` as installed after its normal process refresh; the current long-lived Fleet tasks were left running, so their process environment has not yet refreshed.
- [x] Store redacted installation evidence locally without recording login state or credentials.

Expected: Windows reports AGY installed at the manifest version, hash matches the official manifest, and no provider account/login/model request is created.

### Task 5: Run final regressions and release gates

**Files:**
- All files above.

**Interfaces:**
- No database schema change and no frontend source change.
- Existing signed client release and Hub deployment pipeline remain authoritative.

- [ ] Run `python3 -m pytest tests/test_review_node_sync.py tests/test_native_agent_storage.py tests/test_native_transcript_detail.py tests/test_zcode_session_binding.py tests/test_session_bridge.py tests/test_session_query_api.py tests/test_native_session_api.py -q`.
- [ ] Run the repository's configured lint and packaging/test gates for the changed node code.
- [ ] Rebase onto the latest `main`, merge through the existing GitHub release workflow, and verify the deployed Hub and signed Windows node version before reporting completion.
- [ ] Keep the install and tool-capture verification bounded to local fixtures and official binary identity checks; do not call real Agent/LLM sessions.
