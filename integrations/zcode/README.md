# ZCode desktop conversation bridge

This extension sends a message to an **existing desktop conversation** using
ZCode's official `ctx.sessions.sendToSession` API. Busy conversations receive the
message in ZCode's own input queue. It does not start another CLI, inject stdin,
write ZCode databases, or take ownership of an entire desktop process.

## Install on the desktop machine

Requires ZCode desktop 3.18.2 (verified) with extension API 1.3 or newer, and Python
3.10 or newer for the local client. No npm dependencies are required.

From this repository, use ZCode's bundled CLI (or the equivalent installed CLI):

```sh
node /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs plugins marketplace add "$PWD/integrations/zcode"
node /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs plugins install agent-fleet-bridge@agent-fleet
```

In ZCode's command palette, run **Reload extensions / 重新加载扩展**. In the plugin's
settings, grant **read/write sessions / 读写会话** once. Reloading extensions does
not restart the desktop Host or its running conversations. Windows installation
must use that installation's CLI path. Windows desktop installation and direct
delivery were verified on 2026-11-20 using the existing user account. Run the
control client with that user's HOME and shared runner configuration so it can
read the desktop bridge descriptor and the same continuation manifests.

The extension publishes a private, rotating loopback credential at:

```text
~/.zcode/cli/plugins/data/agent-fleet-bridge@agent-fleet/bridge.json
```

The Python client reads this file locally. Do not copy its token to the Hub or
expose the bridge's loopback port. A stopped extension marks the descriptor
inactive and closes its listener. Receipt files deliberately survive restart;
they contain identifiers, a digest and acknowledgements, never the prompt text.

## Send and verify

```sh
python3 tools/zcode_desktop.py health
python3 tools/zcode_desktop.py visible
python3 tools/zcode_desktop.py sessions
python3 tools/zcode_desktop.py send \
  --session-id 'sess_ID_FROM_VISIBLE_OR_SESSIONS' \
  --request-id 'unique-fleet-command-id' \
  --text 'Please acknowledge this message.'
python3 tools/zcode_desktop.py receipt --request-id 'unique-fleet-command-id'
python3 tools/zcode_desktop.py messages --session-id 'sess_ID_FROM_VISIBLE_OR_SESSIONS'
```

Select the explicit `sessionId` returned by ZCode. `visible` identifies focused
and draft panes; a draft is not an existing conversation. `messages` returns
`nextCursor`; pass it unchanged as `--cursor` to read subsequent pages.

- `accepted` plus `delivery: queued` means ZCode admitted the message to the
  original conversation's queue. It does **not** mean the model has answered.
- `receipt` observes the original `operation.commandId` via `waitSessions`; it
  never calls the send API. `completed` is the original command's terminal fact.
- A repeated send with the same request ID and payload returns the stored
  result. A different session/text under that ID fails with `idempotency_conflict`.
- If a disconnect leaves `unknown`, query the original request and read the
  conversation. Do not generate another ID to hide an uncertain delivery.
- CLI exit codes are 0 for accepted/read success, 1 for transport/API errors,
  2 for rejected admission and 3 for uncertain admission.

The local HTTP surface is intentionally fixed: `/health`, `/visible`, `/sessions`,
`/send`, `/receipts/:id`, `/messages`, `/request-grants`. It is not an arbitrary
RPC proxy. Native `readSession(view: messages)` supplies message reads; raw
`readRows` was excluded after an upstream ZodError was observed on a long real
conversation in 3.18.2.

## Verification and integration boundary

On 2026-11-19, a single marked message was delivered to an already-running `vm`
desktop conversation. Its original operation progressed from `queued` to
`completed`; native message pagination found exactly one matching user message
and the assistant reply `已收到 AF-ZCODE-20261119-01`. The extension was reloaded
while that input was queued, and the same operation survived. No second model
process was launched by this bridge.

The node's native archive sync now binds each supported local desktop session
by its exact `sessionId`. Existing rollout files provide incremental capture;
sessions without a rollout use the official paginated message API, importing
completed messages when the desktop session is idle. These sessions expose
`message_delivery.mechanism=zcode_desktop_bridge` through Fleet. Signed Fleet
commands retain their command ID through the bridge; the control client reports
success only after the original ZCode operation completes. Queued or unknown
admission remains pending. The legacy PID adoption does not gain desktop control
because one shared desktop process cannot identify a single conversation.

Runner-launched Codex and Claude Code sessions use persisted native CLI resume
when idle. Their original directory, explicit model/provider settings, native ID
and event sequence survive a runner/control-client restart. Running CLI sessions
are reported busy; no second CLI writer is spawned into an active conversation.
Desktop history capture yields between bounded slices and retains its current
page and accepted message IDs. Newly changed conversations take priority over
unchanged history; slow initial imports do not monopolize the archive worker.
Node-specific provider credentials can be supplied by `agents.<family>.env_file`
in runner configuration (private JSON mapping, mode 0600). Resumable task
worktrees and continuation checkpoints are retained with their sessions; normal
non-resumable tasks still use the existing cleanup policy.

Run regression tests:

```sh
node --test tests/fixtures/zcode_desktop_bridge.test.mjs
python3 -m pytest tests/test_zcode_desktop_bridge.py tests/test_zcode_session_binding.py -q
```

Official source reference: `zai-org/ZCode`, commit
`fec975905a2abd4c07e35c40d51fa097a2258ec9`,
`docs/plugins/extension-host.md` sections 17.3–17.8. See also the
[implementation plan](../../docs/superpowers/plans/2026-11-19-zcode-desktop-bridge.md).
