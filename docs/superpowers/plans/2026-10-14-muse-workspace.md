# Muse Workspace Implementation Plan

> **For agentic workers:** Use executing-plans to implement this plan task-by-task in the existing isolated worktree.

**Goal:** Import Muse login state and use native Muse conversations, side chats, approval decisions and rich content inside Agent Fleet without an installed or running browser.

**Architecture:** Owner-scoped encrypted credentials feed an independent HTTPS + Noise XX WebSocket client. A Muse application service exposes bounded operations through authenticated Fleet routes. The Muse workspace reuses existing awesome-ui components with multiple independent panes and native session IDs.

**Tech Stack:** Python 3.10+, Flask, cryptography, websocket-client, vanilla ES modules.

## Global Constraints

- Work only in `/Users/mango/.codex/worktrees/muse-browser-bridge/agent-fleet` on `codex/muse-browser-bridge`.
- Do not commit, deploy or alter primary checkout.
- Login import accepts Muse Cookie JSON / Playwright storage state; never persist plaintext credentials, echo credentials or send cookies to VM hosts.
- No browser dependency in runtime. Probe standard VM handshake; reject confidential VM challenges requiring additional keys rather than bypassing attestation.
- Existing browser tool boundary stays unchanged. Unknown card types remain visible as inert data.
- Explicit automatic approval setting is persisted with an audit record; no replay of uncertain writes. Only operate on the importing owner's account.
- User explicitly authorized one dedicated native test side chat and the exact fixed prompt; completed once, with FLEET_OK read from a fresh client process.

### Task 1: Native transport and login import

**Files:** `hub/integrations/muse/{protocol,client}.py`, `tests/test_muse_native.py`, `requirements.txt`.

**Interfaces:** `MuseClient(cookies).request(method, path, params=None, service=0, subscription=False)`; `MuseError(code, detail)`; fixed Muse auth and gateway origins.

- [x] Verify existing cookies in a standalone HTTP client with session and token endpoints.
- [x] Implement bounded protobuf envelopes and standard Noise XX (X25519/AESGCM/SHA256), encrypted HTTP request/response framing; demonstrate `GET /api/session/list` without a browser connection.
- [x] Add independent responder tests for encryption, chunking, malformed frames, confidential challenge rejection, login domain/expiry checks, redirects and redacted errors.
- [x] Run `PYTHONPATH=/tmp/fleet-muse-deps:. python -m pytest tests/test_muse_native.py -q`.

### Task 2: Owner-scoped service, conversations and approvals

**Files:** `hub/infrastructure/muse_repository.py`, `hub/application/muse_service.py`, `hub/http/muse_routes.py`, `hub/bootstrap.py`, `hub/accounts/http.py`, `tests/test_muse_workspace.py`.

**Interfaces:** `MuseService.status/import_login/disconnect/sessions/history/send/approvals/decide/settings`, first argument owner string. Repository encrypts login state, stores settings and mutation receipt keyed by owner+request_id, while the frontend keeps pane drafts in memory separately from native sessions.

- [x] Implement authenticated `/api/platform/v1/muse` routes, no-store responses, JSON size bounds and existing CSRF protection.
- [x] Native session list + history with cursors, new session UUID on first send, native replies and cancellation, single-use approval decision with optional automatic mode.
- [x] Retain pending/unknown mutation receipt before network send, never redispatch the same request after an uncertain result.
- [x] Assert cross-owner isolation, disconnected/expired states, approval audit and concurrent duplicate request handling.

### Task 3: Muse workspace and card rendering

**Files:** `frontend/assets/views/muse.js`, `frontend/assets/views/muse/cards.js`, `frontend/assets/api/muse.js`, `frontend/assets/styles/muse.css`, `frontend/assets/{app.js,shell/navigation.js}`, `frontend/index.html`, `hub/http/pages.py`.

**Interfaces:** `mountMuse(target)` returns dispose/suspend/resume lifecycle; `renderMuseMessage(message)` returns safe DOM. Native session ID is stable per pane; drafts and pending request IDs remain in memory per pane.

- [x] Add Muse subnavigation and account access, login import form and connection status.
- [x] Add session list, new side chat, one or multiple panes, close/select panes, independent input and pagination.
- [x] Reuse StreamMarkdown, ChatPromptInput, AutoScrollAnchor and ToolCallBadge. Render observed text, tool, file/image, link, task and approval card data; unrecognized data renders safely as expandable JSON.
- [x] Add manual approval/rejection, opt-in auto approval, visible result log, resilient polling and teardown.

### Task 4: Verification and documentation

**Files:** `tests/fixtures/browser/muse_workspace.cjs`, `tests/test_muse_workspace.py`, `docs/platform/muse-workspace.md`, `README.md`.

- [x] Exercise UI with fixture transport: login import, multiple panes, native subchat creation, live updates, rich cards, XSS, approval, disconnect, suspended route and narrow viewport.
- [x] Probe real read-only sessions/history/cards through native client using imported login only.
- [x] If authorized, send one exact agreed message into a dedicated test session and read response; otherwise state that live writes remain unverified.
- [x] Run targeted frontend/auth/platform regressions and `git diff --check`. Record limitations and exact commands.

## Execution evidence

- Native transport + workspace acceptance: 28 tests passed, including a real Chromium fixture. Live native send/read passed once with FLEET_OK; real approval writes were not exercised.
- Before fixes, deterministic regression tests reproduced partial-key reads during concurrent initialization and stale automatic decisions after disconnect/reimport/settings changes (4 failures). Atomic key publication and a fresh pre-decision account/settings check made all 4 pass.
- Account, frontend separation and XSS slice passed 57 tests before the final resource/concurrency additions; final broader gate results are recorded below when available.
- Desktop split view and 390px screenshots were visually checked using fixture data only. Production credentials/transcripts are outside the repository.
- Runtime uses history polling, standard VMs only, no attachment upload or proprietary interactive widget execution; see docs/platform/muse-workspace.md for the supported boundary.

- Refetched origin/main: HEAD and origin/main both resolve to 98ffcca, with 0 ahead / 0 behind. Primary checkout remains clean.
- Latest code independently reread the one approved real test prompt and one FLEET_OK reply without sending anything.
- Python 3.10 grammar validation passed for all 25 changed Python files; Node syntax and git diff --check passed. Static frontend smoke and a 54-file preview package passed, including all Muse assets.

- First complete gate: 3366 passed, 2 failed, 5 Darwin skips, 165 subtests. Both failures were reproduced separately: DOM-dependent imports in view modules and missing explicit frontend release entries. Shared components now register only in app.js; Muse files were added to the exact release allowlist and credential scan. Focused reruns: 21 passed (module import + Muse workspace); 37 passed / 2 subtests (release layout). Final complete gate follows.

最终完整本地回归（2026-10-14）：**3368 passed、5 skipped、165 subtests passed**，JUnit 零 failures/errors。36 条 journey 中 35 条通过，MUSE-WORKSPACE 与 WEB-AGENT-01 均通过；唯一未完成的是 RELEASE-01 的 Linux 专用检查，因此 required gate 退出 1，不能视为发布验收通过。证据：`/tmp/fleet-muse-final2-20261014/{journeys.json,results.xml,browser/}`。工作树未提交、未部署，报告记录 dirty checkout；真实 MUSE-LIVE 的单次往返另见上文。
