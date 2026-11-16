# Main assistant VM session management implementation plan

> Execute inline in the existing isolated feature worktree, with tests and a final diff review.

**Goal:** Let the main assistant discover and control existing managed Agent sessions across reporting machines.

**Architecture:** A Hub application adapter composes ObserveService, SessionService, AdoptionService and SupervisorService. ToolBroker delegates the fixed `session.*` tools and `fleet.list_machines` to that adapter; workspace execution remains on the selected Node. Account-mode access rechecks the current active administrator role at tool execution, independently of browser authentication.

**Tech Stack:** Existing Python, Flask composition, SQLite supervisor queue, pytest. No new dependency or transport.

## Constraints

- `machine_id` comes from observation/session metadata; it is not an execution Node ID.
- Only existing managed sessions can receive messages or fixed control actions. Adoption sessions always pass AdoptionService guards, including revoked/pending status and Hermes restrictions.
- Discovery does not adopt a process. Existing explicit adoption UI/API remains the entry point for unmanaged processes.
- A pending command is not a completed remote action. Read its original receipt; never replay uncertain effects.
- Assistant commands use a stable owner/Run-step idempotency key in the existing durable queue.
- Acceptance, workflow-native and summary Runs must not gain fleet tools.
- No changes to protected main or production state in this task.

## Tasks

1. **Application adapter and tests.** Add `hub/application/agent_session_service.py` and `tests/test_assistant_vm_sessions.py`. Verify machine/session metadata, bounded event reads, fixed action validation, adopted/revoked/Hermes behavior, original receipt target validation, signing/feature gates and idempotency. Extend `AdoptionService.control` with an optional idempotency key, preserving existing callers.
2. **Tool registration and worker composition.** Add fixed definitions in `hub/platform/session_tools.py`; register/dispatch in `tool_broker.py`; inject through `run_worker_service.py` and application composition. Add an active-admin lookup to AccountStore and use it at declaration and execution. Tests must cover a main assistant running on one Node managing a session on a different machine, unauthorized accounts, disabled dependencies and acceptance policy isolation.
3. **Prompt, activity and documentation.** Update `assistant_prompt.py` to distinguish conversations, VM Agent sessions and execution Nodes, and to require receipt-based completion. Add readable activity titles and bounded receipt previews. Link this plan from `docs/platform/agent-fleet-self-hosted-platform.md` and document actual capabilities and limits.
4. **Verification.** Run the new module first against the absent implementation to capture failing evidence, then run it against the implementation. Run affected worker/routing/prompt/adoption/session/account/activity tests and repository Ruff checks. Inspect the complete diff and UTF-8/BOM status. Record exact results; do not claim live VM verification without an actual remote receipt.

## Verification commands

```sh
PYTHONPATH=. python3 -m pytest tests/test_assistant_vm_sessions.py -q
PYTHONPATH=. python3 -m pytest tests/test_assistant_tool_routing.py tests/test_assistant_tool_availability.py tests/test_main_assistant_prompt.py tests/test_platform_run_worker.py tests/test_adoption_service.py tests/test_adoption_control_guard.py tests/test_session_interactions.py tests/test_accounts.py -q
ruff check --select E9,F63,F7,F82,F401 hub tools connectors fleet_core
git diff --check
```

Expected: all selected tests pass, Ruff reports no errors, and diff whitespace checks succeed. Full production release and real-machine control acceptance remain separate from local adapter/worker verification.

## Execution result

- Tasks 1–4 completed locally. Fixed action/tool identities live in `hub/domain/agent_session.py`, schemas in `hub/platform/session_tools.py`, service composition in `hub/composition/assistant_management.py`, and application orchestration in `hub/application/agent_session_service.py`.
- Initial new tests against the previous implementation: 15 failed, 2 passed; failures demonstrated the missing service/tool wiring. A subsequent post-enqueue audit-failure regression also failed before command reconciliation was implemented.
- Final affected-scope run: **484 passed, 3 subtests passed** across assistant VM sessions/routing/availability/prompt/live feedback, platform worker, adoption service/control/E2E/HTTP/lifecycle/review, session interactions/query/management, accounts/HTTP/liveness, MCP, workflow native memory and workflow summary generation. The new VM-session module contains 29 executed cases, including real signed Hub/ControlClient/Supervisor integration with recorded process operations.
- Ruff with repository CI rules passed, `git diff --check` passed, and all 16 changed/new files decoded as UTF-8 without BOM.
- GitHub main was independently read as `8eb89e6251486a9b69b4721488a5c6e87b6030ce`; its source tree matches this branch's pre-change baseline (only the merge commit differs).
- At the end of that initial local verification, no Git commit, push, merge, production configuration write or live VM control had been performed. Subsequent authorized administration/deployment work is tracked in [project administration](2026-11-17-assistant-project-admin.md). Remote process operations in the integration tests are recorded doubles; they prove protocol wiring and identity-drift rejection, not production VM acceptance.
