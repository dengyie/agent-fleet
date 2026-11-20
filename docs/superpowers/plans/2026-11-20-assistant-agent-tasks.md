# Main Assistant Agent Task Dispatch Implementation Plan

**Goal:** Let the main assistant start Windows Codex and Claude Code work, follow the original task and continue its resulting native conversation.

**Architecture:** Extend the existing administrator operation allowlist with targets/create/list/get/cancel over TaskService. Reuse the current durable runner queue, machine/project policy, native session projection and session tools. No new execution transport or machine-specific IDs in the system prompt.

**Tech Stack:** Python, Flask composition, SQLite task repository, existing assistant broker and native Windows runner.

## Constraints

- Work inline on `codex/assistant-agent-tasks`, based on production main `0b63d28`; preserve other worktrees.
- Task identities and idempotency tokens come from the authenticated Run. Never expose credentials or arbitrary command templates.
- A queued task is not a completed task; its resulting session is the only target for follow-up. Never recreate uncertain work.
- Use UTF-8 without BOM. Deploy the Hub through the existing GitHub CI; no node code changes are planned.

## Steps

- [x] Add `tests/test_assistant_agent_tasks.py` using the real app/task repository. Prove absent task operations, original task deduplication, host/project/Agent policy, disabled-service gating, authorization, native session projection and worker routing independent of the current workspace.
- [x] Add registered-project discovery to `TaskHostPolicy`/`TaskService` in `hub/application/task_service.py`. Return only project names, supported family names and observed installation status; never paths or credentials.
- [x] Extend `hub/domain/assistant_admin.py` and `hub/application/assistant_admin_service.py` with `tasks.targets/create/list/get/cancel`. Reuse `TaskService.create(..., actor=owner)` with the existing owner/command token. Respect `config.tasks_enabled`, and normalize existing task-domain failures.
- [x] Update `hub/platform/admin_tools.py`, `hub/platform/runtime/assistant_prompt.py` and `hub/domain/tool_activity.py` so discovery, dispatch and progress are understandable and distinct from existing-session continuation. Link this plan from the platform document.
- [x] Run `python3 -m pytest tests/test_assistant_agent_tasks.py tests/test_assistant_project_admin.py tests/test_assistant_vm_sessions.py tests/test_main_assistant_prompt.py tests/test_services.py -q`, relevant broker/activity/session-model tests and repository Ruff/diff checks.
- [ ] Rebase the publication branch onto latest GitHub main, run PR CI, merge and follow the original deployment run. Verify the deployed version.
- [ ] Through the online main assistant, create one dedicated Windows task for each core Agent, inspect its actual response, then send exactly one follow-up to its resulting original session. Preserve task/command IDs and live evidence. Do not touch user work sessions.
- [ ] Record actual results and remaining limits in this plan and the existing canonical operations note on the vault's primary master branch.

## Local verification

The new behavioral suite first reported 11 failures and 2 passes against the absent operations. After implementation and boundary checks, 332 affected tests passed, including both native Agent task lifecycles, idempotent dispatch, installed/unknown evidence, task feature gates, project policy, cancellation, cross-workspace routing and existing session/model behavior. Repository Ruff and diff checks passed. Production acceptance will be recorded in the release PR and canonical operations note after deployment.
