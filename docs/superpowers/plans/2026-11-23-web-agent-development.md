# Web Agent Development Implementation Plan

> Historical implementation checkpoint for the earlier print-based release.
> Agy execution and continuation are superseded by the
> [native provider runtime plan](2026-11-23-native-provider-runtime.md) and
> [ACP development contract](../../native-provider-runtime.md). The earlier
> stdout reader remains for retained records; its launch syntax below is not the
> current execution contract.

**Goal:** Support Google Antigravity CLI (`agy`) through the existing native task pipeline, preserve detailed native transcripts, and expose retained task results beside web chat.

**Architecture:** Reuse the Agent profile registry, Runner/Supervisor, normalized events, SQLite repositories and public HTTP APIs. Antigravity stream shaping belongs in a small node-only module. Browser result inspection reads existing task snapshots; it cannot execute commands or browse the VM filesystem.

**Tech Stack:** Python 3.10+, Flask, SQLite, native CLI NDJSON, JavaScript modules, pytest and existing Chromium fixtures.

## Constraints and evidence

- Start from main `1e3f6b53`, then rebase onto `22576a3a` (PR116); preserve native control/profile/authority/handoff behavior, other worktrees and user configuration.
- Local `agy --version` is `1.5.14`; `--help` confirms `--print`, `--output-format stream-json`, and exact-ID `--conversation`. Official protocol: https://antigravity.google/docs/cli/headless . Do not use latest-conversation `--continue`.
- No Agent/model calls in automated tests. Native stdout fixtures and actual disposable subprocesses prove the local pipeline; real vendor acceptance remains separately reported.
- No new middleware, reverse Hub connections, credentials in public DTOs, or invented model/usage/title fields. Keep internal reasoning outside the chat projection.
- The signed node closure changes. A Hub-only deployment is insufficient: verify installed node versions and preserve rollback before claiming the new collector is live.

## Task 1: Antigravity native execution and capture

Files: `agent_profiles.py`, `tools/adapters/agy.py`, adapter registry, `connectors/agy.py`, connector registry, `tools/probe/discovery.py`, `tools/adapters/native_resume.py`, `tools/supervisor/native_resume.py`, `tools/session/agy.py`, `tools/session/bridge.py`, relevant frontend family labels and tests.

- [x] Add fixtures for documented init/step_update/result records and tests that fail before the family exists: exact executable detection, original-ID continuation after Supervisor restart, multi-turn output, visible deltas, tools/errors, final-result fallback, malformed records and reasoning exclusion.
- [x] Register `agy` with `("agy", "--output-format", "stream-json", "--print", "{instruction}")`; permit native continuation only for verified Runner-owned sessions. Preserve explicit model/project/mode/effort/settings options. Keep native IDs and paths node-private.
- [x] Normalize each step by its conversation/turn/step identity, append actual `text_delta`, retain tool arguments/results, and avoid repeating the final aggregate response after streamed text. Do not infer support for arbitrary desktop/old conversations.
- [x] Run `python3 -m pytest tests/test_agy.py tests/test_agent_profiles.py tests/test_cross_platform_agents.py tests/test_agent_capability_layers.py tests/test_discovery.py -q`.

## Task 2: Native transcript correctness

Files: `tools/session/normalization.py`, `tools/session/bridge.py`, `tools/session/sync.py`, `tests/test_native_transcript_detail.py`.

- [x] First reproduce Codex turn-context model loss, Pi explicit session-info title loss, and interleaved text/tool/text snapshot replacement with executable tests.
- [x] Preserve metadata from recognized metadata-only records; scope message identities by native content-block identity so a later text block cannot overwrite earlier text. Keep Claude stream block IDs consistent with completed snapshots.
- [x] Preserve desktop metadata checkpoints across page advances, preventing repeated metadata events on each scan.
- [x] Run new tests plus `tests/test_session_bridge.py`, `tests/test_review_node_sync.py`, `tests/test_session_query_api.py`, and `tests/test_native_chat_frontend.py`.

## Task 3: Web result inspection

Files: `frontend/assets/views/agent-chat/results.js`, chat/inspector/service modules, HTTP client options, frontend release allowlist and browser tests.

- [x] Consume `getTask`, `getTaskDiff`, `listTaskFiles`, `getTaskFile` with abort signals. Resolve a task only from authoritative session task metadata, verify its session binding, and read snapshots only on explicit operator action.
- [x] Add a result panel with original task status, retained diff and changed-file snapshots. Mark initial-task snapshots as such; continuation turns do not refresh old task snapshots. Render bounded plain text and truncation/redaction state.
- [x] Cover mismatched identities, unavailable results, XSS text, cancellation/suspend/dispose and desktop/narrow layouts with actual HTTP/Chromium fixtures. No new filesystem API.

## Task 4: Verification and release

- [x] Update native API/frontend/acceptance/testing documents with actual Antigravity support and remaining iteration list: complete native metadata backfill, richer source-dependent streaming, model selection, test reports, and per-session usage.
- [x] Run focused regressions, browser tests, Ruff, JS syntax, package checks, `git diff --check`, and UTF-8/no-BOM checks.
- [ ] Rebase latest main before merge, create/attach the PR, follow exact-head three-platform CI, merge and follow exact main deployment and public resource verification.
- [ ] Verify signed node manifest, install through existing drain/update mechanisms, compare running client closure and preserve previous versions. Record unavailable installations and real-model boundaries truthfully.


## Local evidence before publication

- Original agy/metadata/block regressions: 9 expected failures before implementation.
- macOS native gate plus node sync/discovery: 462 passed, 2 POSIX/runtime-specific skips, 103 subtests passed.
- Browser native chat + package + capability: 34 passed using existing Chrome/Playwright; native chat covers 1440/390px.
- Frontend contracts/XSS/release layout/package direction/native HTTP: 215 passed, 2 subtests passed.
- Related native HTTP, continuation and snapshot suite: 357 passed, 3 subtests passed (overlapping suites are not summed).
- Ruff production selectors, JS syntax, static smoke, diff whitespace and changed-file UTF-8/no-BOM checks passed.
- No Hub database schema migration, no added runtime dependency, and no real vendor/model call.
- Integrated PR116 verification: native chat/package/capability Chrome suite **40 passed** at 1440/390px; agy/transcript/continuation/capture/architecture/frontend/package checks **272 passed, 2 subtests passed**. Ruff and static frontend smoke passed.
- Rebase integration preserves PR116 native controls, local profiles, stale-authority guards and context handoff. Original PR head Windows timed out after 555 passing tests; no assertion failures. Keep this evidence and verify the integrated head separately.
- Signed-client publication and production deployment remain pending until exact main CI succeeds.

- Physical Windows integrated full gate: 825 passed, 10 skipped with UTF-8 mode disabled. One existing Node-test capture decoded UTF-8 as GBK; warning-as-error reproduced a failure. Explicit UTF-8 capture plus thread-warning error guard passed all 6 bridge tests on Windows and macOS. This fixture-only fix does not change the signed client closure.
