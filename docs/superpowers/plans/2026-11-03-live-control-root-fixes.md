# Production Agent Control Root Fixes

Execute inline using evidence-driven-bugfix and verification-before-completion.

Baseline: production `a515d97b6e0a1b61ca8dbd3b82d05551e4ee7a4f`. Specifications: `2026-08-26-agent-session-supervision-design.md` (bounded TERM grace), `2026-08-29-agent-instance-discovery-adoption-design.md` (identity guard, detach never terminates), and `docs/testing/test-chains.md`.

## Implementation and verification

- [x] Reproduce the stale running session through the real adoption HTTP surface and SQLite. Test successful terminate, failed/unverified terminate, detach, replay, application reconstruction and atomic store failure/retry.
- [x] Inject the existing session repository into the common `SupervisorReceiptHook` in `hub/application/control_router.py` and `hub/bootstrap.py`. Use persisted command identity and the first canonical receipt; terminate_session/cancel_attempt confirmed success invokes the existing atomic `finish_managed_session`. Receipt and pending projection are committed atomically. A store failure returns a cause-preserving 503; authenticated poll recovers pending projections after restart without redelivering terminal commands. Missing sessions defer for 30 seconds so a bounded batch of 100 cannot starve later sessions. Schema initialization backfills historical confirmed exits once. Do not add a second state store or depend on adoption's in-memory tracking.
- [x] Reproduce the missing TERM grace through a signed command. Change `ControlClient` default to 5 seconds, matching Supervisor; verify delayed graceful exit, explicit zero and bounded force escalation.
- [x] Reproduce discovery time under a non-UTC parent timezone. Set `TZ=UTC` in the existing minimal ps environment and document the parser's UTC input. Verify discovery and the shared Supervisor identity guard. Old non-UTC identities fail closed and need explicit re-adoption; do not reinterpret stored identities.
- [ ] Bind tests to ADOPT-01/SESSION-01, update test chains, run focused suites, compileall, Python 3.10 syntax parsing, diff checks and the complete existing test gate.
- [ ] Investigate CLI model failures with existing credentials and isolated configuration. A plain text response is not proof of tool execution. Preserve global CLI settings and do not invent credentials.
- [ ] Record fresh operating evidence in the canonical Obsidian manual on vault master, with deployment and installed-client scope explicit.

## Evidence and constraints

Production session `adopt_d2ee682669c6d152161d5cc1c54e6ebe` remains running after the terminate receipt succeeded and PID disappeared. Evidence is in `/Users/mango/.codex/agent-fleet-deploy-evidence/20261103/live-control-session-state.json`. POSIX discovery reproduced a 28800-second future timestamp. ControlClient explicitly overrides Supervisor's default grace with zero.

Existing Python >=3.10, Flask, SQLite, pytest and cryptography only. Primary checkout changes are preserved. All new text is UTF-8 without BOM. Command lookups use the existing primary-key index, O(1); no new facade, framework or dependency.
