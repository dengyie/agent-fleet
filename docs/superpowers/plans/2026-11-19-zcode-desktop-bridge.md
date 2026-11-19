# ZCode desktop session messaging implementation plan

**Goal:** Deliver a message to an existing ZCode desktop conversation and verify its original-session receipt.

**Architecture:** A locally installed ZCode extension calls the official `ctx.sessions.sendToSession` interface. A loopback bridge exposes explicit session selection, stable request IDs and receipt observation to the Fleet client. ZCode remains the owner of admission, queueing and execution; the bridge never launches a conversation CLI, writes ZCode databases or injects process stdin.

**Tech stack:** Node built-ins, ZCode extension API 1.3+, Python standard library.

## Evidence and boundaries

- Existing Fleet command `cmd_3a78bf1b1108075d7ec80eddd680a4a9` was rejected with `unsupported_action`: a desktop process adoption is not a conversation transport.
- Official source `zai-org/ZCode` at `fec975905a2abd4c07e35c40d51fa097a2258ec9` matches installed desktop 3.18.2. `docs/plugins/extension-host.md` sections 17.3–17.8 define the supported same-Host interface.
- An ACK can mean queued. Completion requires a receipt or projected row tied to its original `operation.commandId`.
- Use UTF-8 without BOM; preserve existing worktrees, running conversations and user drafts.
- No package downloads or alternate model process are needed.

## Implementation and validation

- [x] Add `tests/fixtures/zcode_desktop_bridge.test.mjs`: use a fake extension context to verify original `sessionId`, `requestId`, concurrency deduplication, mismatched retries, uncertain delivery, read-only receipt observation, authentication and lifecycle disposal. Run `node --test tests/fixtures/zcode_desktop_bridge.test.mjs`; capture the initial missing-module failure.
- [x] Add `integrations/zcode/plugins/agent-fleet-bridge/bridge.mjs` and `extension.mjs`. `startBridge(ctx)` returns `{ endpoint, close }`; POST `/send` accepts `{ session_id, request_id, text }`, calls `sendToSession` once, persists the receipt without prompt text, and GET `/receipts/:id` calls `waitSessions` with that operation. GET `/visible` reads the desktop-selected sessions; `/sessions` and `/messages` use official read APIs. Authentication is private local bearer capability; no arbitrary RPC forwarding.
- [x] Add local marketplace and extension manifests declaring `sessions.write` and `sessions: 1`, and automatic activation. Add `tools/zcode_desktop.py` with explicit `visible`, `sessions`, `send`, `receipt`, `messages` commands. A send requires an existing session ID, stable request ID and text; it never retries mutation automatically.
- [x] Run Node regression tests through a pytest wrapper; test the Python client against a local bridge fixture and run existing ZCode/session capability regressions.
- [x] Install through ZCode's local marketplace CLI, activate/reload the extension through supported app controls, and grant the already-authorized local session access. Do not restart the desktop or any active agent.
- [x] Read current visible session identity, submit one marked non-destructive acknowledgement request, then query the original receipt/rows. Verify the same session and source command ID, one user message, and native reply or actual queue state.
- [x] Record verified results and remaining deployment boundaries in the integration README; preserve all test evidence outside the repository.

## Verified outcome

- Original desktop conversation: the existing focused `vm` session; its exact native identity is retained in the private verification evidence.
- Request: `fleet-zcode-20261119-current-01`; its original operation reached `completed`.
- Three native message pages (536 messages) contained one marked user input and the reply `已收到 AF-ZCODE-20261119-01`.
- Initial missing-module test failed as expected. Final suite: 22 pytest cases plus 9 Node contract tests passed; manifest validation and `git diff --check` passed.
- Real long-conversation `readRows` returned an upstream ZodError. Removed the unused raw-row surface and use official `readSession(view: messages)` with native cursor pagination.
- This task completes direct local desktop delivery. It does not claim that the online Fleet assistant or Windows has been connected to this new local transport.
