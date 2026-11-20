# Session Model Takeover Implementation Plan

**Goal:** Let the administrator assistant route a managed VM Agent's existing conversation through an online model profile and restore its original configuration.

**Architecture:** Reuse the owner-scoped model catalog, signed supervisor outbox, machine authentication and native continuation store. Signed commands carry catalog identifiers and digests only. A machine-bound, command-bound resolver supplies private configuration; the node applies it to the original idle conversation's next native resume. No global Agent config edits or automatic prompt replay.

**Stack:** Python/Flask, SQLite, signed pull commands, native Claude/Codex CLI, existing JavaScript settings editor.

## Constraints and evidence

- Windows Claude session `runner_ccc39a1c2ae442e7_bb52b67ef4514149a152b1b092dfe78c` resumed its original native conversation but the old gateway returned HTTP 503 for `claude-opus-4-8`.
- `apply_local_profile` only updates cc-switch's current pointer; `NativeResumeMixin` continues using the original environment/options. This is the missing execution boundary.
- Magpie reference: commit `4716a77ff9f8e6bd7ff64780f81674de0fee805e`, `internal/agent/{claude,codex,zcode}.go`. Claude precedence and aliases, Codex saved provider identity, and desktop restart requirements must be handled explicitly.
- Preserve native conversation ID, cwd, user policies and active processes. Configuration success is not model success. Unknown delivery is never blindly replayed.
- UTF-8 without BOM. No keys in tool arguments, receipts, public manifests, command envelopes or logs. Protect other worktrees.

## Execution (inline)

### 1. Catalog and signed control boundary

Files: `platform_schema.py`, `hub/application/session_model_service.py`, `hub/application/agent_session_service.py`, `hub/composition/assistant_management.py`, `hub/http/supervisor_routes.py`, `fleet_core/control.py`.

- [x] Add failing tests in `tests/test_session_model_takeover.py`: catalog filtering, authenticated ownership, exact digest binding, machine/TTL binding, idempotency, no credential leakage, and disabled/incompatible profiles.
- [x] Add explicit `provider_config.agent_endpoints` with `anthropic` and `responses` Base URLs; do not assume a Chat Completions service supports either protocol.
- [x] Expose `session.list_models` and `session.configure_model` (`mode=catalog|original`). Derive machine/family from the session; derive owner from authentication.
- [x] Sign `configure_model` with only owner/profile/digest/mode. Reject forged ownership via generic controls. Resolve secrets only for the authenticated target machine's live delivered command.
- [x] Run `python3 -m pytest tests/test_session_model_takeover.py tests/test_assistant_vm_sessions.py tests/test_supervisor_control.py -q`.

### 2. Node native continuation

Files: `tools/supervisor/model_config.py`, `continuation.py`, `handles.py`, `native_resume.py`, `control_client.py`, `tools/session/runner_capture.py`.

- [x] Tests prove failed Claude config takeover, atomic original restore, same native identity/cwd, busy rejection, persistence/restart, and Codex saved-provider override.
- [x] Store one per-session model override alongside the original continuation. Build effective env/options at resume, without mutating the backup. Claude uses a private settings file and model aliases; Codex overrides the saved provider using Responses.
- [x] Gate the action on a new advertised native capability. Reject unsupported desktop sessions and unfinished writers truthfully.
- [x] Run native continuation/control regression tests. Use real installed Windows CLIs to verify effective requests and session identity.

### 3. Assistant and settings integration

Files: `hub/platform/session_tools.py`, `hub/domain/agent_session.py`, `hub/platform/runtime/{assistant_prompt,native}.py`, `hub/domain/tool_activity.py`, `frontend/assets/views/settings/catalogs.js`.

- [x] Add protocol URL fields to the existing model settings advanced section and preserve them on edits.
- [x] Teach the main assistant to inspect the failure, choose a compatible catalog profile, wait for its receipt, continue once, and verify new events. Explain original restore and unsupported live desktop switching.
- [x] Test schema exposure, meaningful activity labels, settings round trip and credential-free outputs.

### 4. Release and real acceptance

- [ ] Review the resulting call chain and rerun relevant tests; run the repository's required CI.
- [ ] Publish only this task through the public release checkout, rebase onto latest main, merge and deploy through established scripts.
- [ ] Upgrade affected nodes; verify original failing Windows Claude session returns a real model response after catalog takeover. Verify Codex separately and preserve exact receipts and native IDs.
- [ ] Update the canonical Obsidian operations document on master, its existing router/index entries and the evidence record. Report verified support and remaining external limits precisely.

## Pre-release evidence

- Local regression: 453 passed, 2 expected platform skips, 47 subtests; after restoration hardening, 241 passed, 2 skips, 2 subtests. Ruff and diff checks passed.
- Windows native configuration tests passed; desktop/mobile settings save and reload passed.
- Real Windows Claude and Codex resumed their original native IDs and returned acceptance replies through the online gateway. Claude changed from the failing legacy model route to `deepseek-v4-flash`; Codex used `gpt-6-sol` via Responses. Evidence is held privately under `~/.cache/agent-fleet/verification/session-model-takeover-20261120/`.
- Publication, deployed signed-node delivery and final online assistant acceptance remain pending.
