# Muse Local Browser Bridge Implementation Plan

**Goal:** Let Agent Fleet delegate tasks to the user's logged-in Muse account through the local Chrome DevTools endpoint.

**Architecture:** Assistant tools use the existing signed Hub → polling Node → durable journal → receipt path. An explicitly enabled Muse-only node provides `web_agent.status`, `web_agent.submit`, and `web_agent.read`. Python maintains durable task correlation; a fixed Node.js helper snapshots the main chat or fills its textarea and clicks its send control. Generic `browser.*` networking remains unchanged.

**Environment:** macOS/Linux, Python 3.10+, Node.js 22+, an existing logged-in `https://muse.ai/` tab on `127.0.0.1:9222`. Implementation branch: `codex/muse-browser-bridge`, based on `98ffcca` after the requested rebase onto `origin/main`.

## Boundaries

- Fixed loopback endpoint and exact main-chat URL; reject ambiguous initial tab selection. Bind subsequent reads to the original target.
- No caller-supplied URL, selector, JavaScript, cookie, storage or private API access.
- Explicit local opt-in, signed commands, node capabilities and workspace allowlist.
- One outstanding task per account/state file. Persist dispatch intent before touching the UI; never replay an uncertain send.
- Preserve existing drafts and reject streaming conversations. Recheck conversation and composer immediately before clicking.
- Correlate the exact normalized sent text, then a non-streaming reply carrying a random task-specific terminal marker. Completion is agent-reported.
- No automatic approval, remote cancellation, unknown-task unlocking or file uploads.

## Implementation checklist

- [x] `tools/browser_bridge/cdp_muse.cjs`: fixed discovery, snapshot and send operations; bounded request/response, origin and cursor checks.
- [x] `tools/browser_bridge/muse.py`: SQLite state, single-writer lock, task/content/scope deduplication, restart recovery, bounded transcript and reply.
- [x] `tools/browser_bridge/contract.py`: shared tool schemas, exact argument validation and workspace authorization.
- [x] `tools/browser_bridge/muse_cli.py`: explicit `status`, `submit TASK_ID` via stdin, and `read TASK_ID`.
- [x] `tools/browser_bridge/node.py`: foreground polling node, private state directory, explicit config and manifest output.
- [x] Node runtime/executor/client: opt-in capability assembly, signed execution and rejection through legacy unsigned paths.
- [x] Remote broker and Hub worker: expose only the selected enabled owner's declared tools; no Hub-local execution.
- [x] Tests: durable dedup, crashes, drafts, streaming, human interleaving, approval continuation, Unicode/newlines, scope/signature gates and signed Hub/Node round trip.
- [x] Real Chromium fixture: intercepted local Muse DOM, fixed CDP operations, safe composer handling and exact text round trip.
- [x] README, operator guide and `WEB-AGENT-01` journey; real-account check recorded separately as `MUSE-LIVE`.

## Verification record — 2026-10-13

- Final affected-chain regression: **121 passed**, including the real Chromium fixture, Muse bridge, signed Fleet integration, node/runtime/HTTP and existing browser-policy/submit tests.
- Node.js syntax checks, Python compileall, Python 3.10 AST compatibility, UTF-8 without BOM checks and `git diff --check` passed. Both CLI entry points load successfully.
- Earlier full-suite run: **3336 passed, 5 skipped, 165 subtests passed**. Journey gate: **34/35**, exit 1 because `RELEASE-01` requires Linux `pidfd`/`/proc` tests skipped on macOS. This run preceded the final approval/streaming/Unicode/private-directory fixes; those have the fresh 121-test regression above. No release-gate pass is claimed.
- Read-only live status after the fixes: connected, ready, no draft and no outstanding task. The user's browser was not restarted or navigated.
- `MUSE-LIVE`: **not_run** pending explicit permission for the harmless test message. The live send-button selector and marker round trip remain unverified.
- No Hub registration, background service, deployment, commit or push was performed.

## Remaining external acceptance

- [ ] With explicit permission, submit the agreed harmless prompt once, retain the task ID/state file, and read its matching reply after restarting the bridge process. An unknown send must be investigated without replay.
- [ ] Run the full release journey gate on Linux when preparing a release.
