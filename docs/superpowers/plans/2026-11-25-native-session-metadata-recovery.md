# Native Session Metadata Recovery Implementation Plan

> **For agentic workers:** Execute this plan inline with `executing-plans`; each change requires a reproduced failure and fresh verification before release.

**Goal:** Recover explicitly observed native session names, models, providers, client versions and native start times without replaying conversation messages or starting an Agent.

**Architecture:** Keep vendor field parsing in the existing normalization/native SQLite adapters. A focused node-side metadata reader takes bounded JSONL snapshots or one exact SQLite session row, verifies retained session/cwd bindings, and publishes through the existing RunnerCapture spool/uploader. The existing Hub repository projects historical prefix facts only into missing fields; current tail/database facts retain the existing ingestion-order update semantics.

**Tech Stack:** Python 3.10+, existing Flask HTTP, SQLite repositories, encrypted local spool and signed client releases.

## Global constraints

- Backend only; do not edit frontend product code, deployment logic or vendor settings/history.
- No real Agent CLI, version/help probe or external model call.
- Browser authorization and public DTOs remain unchanged; no new HTTP endpoint or database schema.
- Reuse Hub ingestion cursors. Metadata recovery never advances or rewinds a native message byte/row cursor.
- All new/modified files use UTF-8 without a BOM.
- Rebase on latest main before merge, follow the actual deployed CI run, then update the affected signed Mac/Windows clients and verify production.

## Evidence and failure boundary

The fixed Mac/Windows acceptance histories were read without executing an Agent. Codex headers contain `cli_version`/`model_provider` and turn contexts contain models; Claude user/assistant rows contain string `version`; Pi's session `version` is an integer storage format; ZCode SQLite has `title`, string `version`, millisecond `time_created`; Hermes SQLite has `title`, `model`, second-based `started_at`. Their old Hub session DTOs lack those already available facts. Current source normalization also drops Claude row versions, and native SQLite reading projects conversation parts only. Existing source EOF checkpoints deliberately prevent old content replay.

### Task 1: Correct observed field mapping

**Files:** `tools/session/normalization.py`, `tools/session/native_storage.py`, `tests/test_native_metadata_recovery.py`.

**Interfaces:** Preserve `normalize_native_row(raw)`; add `read_metadata(family, path, token, *, cwd=None) -> dict` for one exact native SQLite session.

- [x] Add and run failing source-shaped tests. Claude fixture: `{'type':'assistant','sessionId':'native_fixture','version':'2.1.319','message':{'model':'observed-model','content':'answer'}}` must produce `client_version='2.1.319'`; Pi `{'type':'session','version':3}` must not produce a client version.
- [x] Map Claude versions only on recognized native user/assistant envelopes. SQLite readers select explicit optional columns, bound values, normalize the documented timestamp units, and verify the exact id/cwd before returning facts.
- [x] Re-run the focused tests; old/minimal and damaged vendor databases remain readable or fail closed without being written.

### Task 2: Recover retained metadata without content replay

**Files:** `tools/session/native_metadata.py`, `tools/session/runner_capture.py`, `tools/session/sync.py`, `session_schema.py`, `tools/session/bridge.py`, `hub/infrastructure/session_repository.py`, `tests/test_native_metadata_recovery.py`.

**Interfaces:** JSONL metadata snapshots return only display metadata plus `fill_missing=true` for historical prefix facts. Current bounded EOF-tail/SQLite facts use ordinary metadata projection. A digest-only versioned checkpoint in the existing capture recovery index suppresses unchanged snapshots; the existing spool owner lock serializes writers.

- [x] Reproduce an old EOF checkpoint through actual RunnerCapture → spool → HTTP → SQLite: old messages stay byte-for-byte/event-id unchanged while source-backed metadata becomes available on the same session id.
- [x] Read at most a 256 KiB prefix and 2 MiB tail, with at most 4096 rows per region; skip incomplete/oversized/malformed rows. Reject changed files, unsafe file types, wrong native id/cwd and ambiguous source matches.
- [x] Add the boolean `session_metadata.fill_missing` contract. Historical fields update only NULL/empty projections; ordinary later metadata keeps existing field-cursor semantics. Do not grant control capabilities through recovery.
- [x] Refresh metadata after each archive slice and at retained capture completion/replay. Publish before atomically saving the digest checkpoint; failed storage/upload stays recoverable and does not change a source content cursor or rerun a command.
- [x] Test unchanged restarts, live writer exclusion, response loss, checkpoint failure, corrupt/future checkpoint refusal, old indexes, bounded reads, and current source updates. Missing unsupported facts stay missing.

### Task 3: Document, verify and release

**Files:** `docs/native-session-api.md`, `docs/agent-observability.md`, `docs/testing/README.md`, this plan; add the focused suite to the existing platform test selectors only if required for execution coverage.

- [x] Document per-source fields, snapshot bounds, historical fill-only semantics, timestamp units, failure/recovery behavior and the source/hidden-reasoning boundary.
- [x] Run `python3 -m pytest tests/test_native_metadata_recovery.py tests/test_native_transcript_detail.py tests/test_review_node_sync.py tests/test_native_agent_storage.py tests/test_session_bridge.py tests/test_session_repository.py tests/test_session_schema.py tests/test_agent_evidence.py -q` and related capture/continuation/HTTP/boundary regressions.
- [x] Check `git diff --check`, UTF-8/no BOM, changed-file scope and unchanged database schemas; retain red/green evidence.
- [ ] Commit, rebase latest main, open/merge the concrete PR, follow all CI gates and the actual deploy receipt; update signed clients through existing installers and validate the fixed dedicated histories through operator HTTP.

The local and online verification evidence must identify actual commits, exact commands/counts and remaining unsupported fields. Existing historical source contents, settings, task results and required rollback releases must remain intact.
