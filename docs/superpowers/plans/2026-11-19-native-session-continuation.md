# Native Session Continuation Implementation Plan

> For agentic workers: execute this plan inline, one regression and one root-cause fix at a time. Existing user authorization includes deployment and live acceptance.

**Goal:** Deliver follow-up messages through production Fleet into the same persisted native Agent session, with observable replies and truthful capabilities.

**Architecture:** The node owns durable private continuation identity and the original event spool. Hub queues signed commands and projects node-reported readiness. The control client executes an idle continuation and captures its output; running processes are never concurrently resumed. ZCode desktop delivery uses the installed official extension bridge and an exact native conversation identity.

**Tech Stack:** Python, SQLite, encrypted SessionBridge spool, existing runner/control-client, Codex/Claude CLI, ZCode extension.

## Global Constraints

- Work on the isolated feature branch; preserve unrelated changes and rebase onto current main before release.
- UTF-8 without BOM. No credentials or prompts in public manifests or committed fixtures.
- No successful delivery claim from queue acceptance alone. Reuse command IDs on uncertain outcomes.
- Preserve resumable working directories and event sequence/checkpoint state.

## Evidence

`~/.cache/agent-fleet/verification/live-agent-messaging-20261119/acceptance-summary.json` records two real failed tasks and two signed `scope_mismatch` receipts. Both disk manifests contain `reason=completed`; deserialization drops it. Codex ignores configured provider and calls OpenAI with incompatible auth; both machine commands disable persistence.

## Tasks

- [x] Restore `ManagedSessionManifest.reason`; test disk roundtrip and cross-instance completed-session gate, while explicitly terminated/quarantined sessions remain blocked.
- [x] Set persistent structured defaults in `agent_profiles.py` and adapters; remove duplicated defaults, update deployment examples and generated client configuration. Verify environment/provider configuration reaches the CLI without exposing runner credentials.
- [x] Persist continuation identity in a node-private 0600 record, restore it across runner/control-client processes, preserve token/cwd/executable/provider environment, and reject concurrent resume. Verify Codex rollout identity and Claude stream `session_id` extraction with actual-format records.
- [x] Retain the original worktree and capture checkpoint only for resumable sessions. Resume through the original signed-command path, capture user/assistant/events into the original session stream, and expose truthful `native_resume` readiness. Test two successive follow-ups, restart, no duplicate sequence/messages, failed spawn and busy sessions.
- [x] Connect exact ZCode desktop sessions to the existing bridge; never map a shared desktop PID to one arbitrary conversation. Test unavailable bridge, idempotent request receipts and original-session delivery.
- [ ] Run targeted regressions and project checks. Rebase, publish through the existing main release pipeline, update node clients/configuration, then re-run two real tasks and follow-ups. Check both native conversation evidence and Hub events/receipts; preserve evidence and rollback references.

## Verification commands

`python3 -m pytest tests/test_native_continuation.py tests/test_runner_conversation.py tests/test_supervisor_process.py tests/test_supervisor_control.py tests/test_control_client_cli.py -q`

`python3 -m ruff check --select E9,F63,F7,F82,F401 hub tools connectors fleet_core`

Baseline: 94 passed, 2 skipped, 2 subtests passed for runner conversation, supervisor process, and control-client CLI tests.
