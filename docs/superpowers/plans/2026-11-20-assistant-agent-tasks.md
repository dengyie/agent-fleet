# Main Assistant Agent Task Dispatch Implementation Plan

**Goal:** Let the main assistant start Windows Codex and Claude Code work, follow the original task and continue its resulting native conversation.

**Architecture:** Extend the existing administrator operation allowlist with targets/create/list/get/cancel over TaskService. Reuse the current durable runner queue, machine/project policy, native session projection and session tools. No new execution transport or machine-specific IDs in the system prompt.

**Tech Stack:** Python, Flask composition, SQLite task repository, existing assistant broker and native Windows runner.

## Constraints

- Original dispatch work used `codex/assistant-agent-tasks`, based on production main `0b63d28`; the PR98 capture follow-up used `codex/agent-dispatch-reliability`. Preserve other worktrees.
- Task identities and idempotency tokens come from the authenticated Run. Never expose credentials or arbitrary command templates.
- A queued task is not a completed task; its resulting session is the only target for follow-up. Never recreate uncertain work.
- Use UTF-8 without BOM. Deploy the Hub through the existing GitHub CI. PR97 did not change node code; the PR98 follow-up below updates node capture and polling.

## Steps

- [x] Add `tests/test_assistant_agent_tasks.py` using the real app/task repository. Prove absent task operations, original task deduplication, host/project/Agent policy, disabled-service gating, authorization, native session projection and worker routing independent of the current workspace.
- [x] Add registered-project discovery to `TaskHostPolicy`/`TaskService` in `hub/application/task_service.py`. Return only project names, supported family names and observed installation status; never paths or credentials.
- [x] Extend `hub/domain/assistant_admin.py` and `hub/application/assistant_admin_service.py` with `tasks.targets/create/list/get/cancel`. Reuse `TaskService.create(..., actor=owner)` with the existing owner/command token. Respect `config.tasks_enabled`, and normalize existing task-domain failures.
- [x] Update `hub/platform/admin_tools.py`, `hub/platform/runtime/assistant_prompt.py` and `hub/domain/tool_activity.py` so discovery, dispatch and progress are understandable and distinct from existing-session continuation. Link this plan from the platform document.
- [x] Run `python3 -m pytest tests/test_assistant_agent_tasks.py tests/test_assistant_project_admin.py tests/test_assistant_vm_sessions.py tests/test_main_assistant_prompt.py tests/test_services.py -q`, relevant broker/activity/session-model tests and repository Ruff/diff checks.
- [x] Rebase the publication branch onto latest GitHub main, run PR CI, merge and follow the original deployment run. Verify the deployed version.
- [ ] Through the online main assistant, create one dedicated Windows task for each core Agent, inspect its actual response, then send exactly one follow-up to its resulting original session. Preserve task/command IDs and live evidence. Do not touch user work sessions.
- [ ] Record actual results and remaining limits in this plan and the existing canonical operations note on the vault's primary master branch.

## Local verification

The new behavioral suite first reported 11 failures and 2 passes against the absent operations. After implementation and boundary checks, 332 affected tests passed, including both native Agent task lifecycles, idempotent dispatch, installed/unknown evidence, task feature gates, project policy, cancellation, cross-workspace routing and existing session/model behavior. Repository Ruff and diff checks passed. Production acceptance will be recorded in the release PR and canonical operations note after deployment.

## Production acceptance findings and follow-up

PR97 deployed as `f67f94f`. Full PR and main both passed 8380 tests, 204 subtests, 47 journeys and 2830 selectors; Windows 563 passed with 9 expected skips. Physical Windows new-tool tests: 18 passed. The main assistant created exactly one Codex task and one Claude Code task from its original VM workspace.

Real tool-use acceptance exposed existing failures that prevent declaring the workflow complete:

- Windows retained 2170 capture records; historical pending uploads filled 134217325 of the shared 134217728 bytes. New task capture failed with `source_record_not_persisted`, so the native session was absent from Hub despite actual task execution.
- The runner replayed all historical captures synchronously before each task poll. Network failures multiplied the delay. Move replay to its own transport/thread and keep task polling independent; retain the existing spool writer locks.
- Task-to-session projection scanned only the newest 1000 sessions. An exact machine/attempt lookup and index resolve existing task bindings regardless of archive size.
- The main assistant attempted workspace `sleep` while waiting or ended at queued. Add bounded long polling to `tasks.get(wait_seconds=30)` and explicitly continue the original task; this bounds one read, not task execution time.
- Codex's Windows elevated sandbox returned `timed out ... runner pipe-in` when used by the S4U service. Verify task-scoped direct execution settings on the existing dedicated test conversation before updating the runner-only configuration; do not change user-wide Codex settings.
- Claude's original upstream still returned HTTP503. Once the exact session is readable, use the already implemented catalog takeover for that original session.

Follow-up regression reproduced all three capture/poll failures and the 1000-session mapping failure before their fixes. After moving replay, reserving 64 MiB for live captures above the archive's 128 MiB cap, closing failed capture initialization and adding exact lookup/long polling, 432 tests and 3 subtests passed. No retained transcripts were deleted. The initial live attempts remain evidence and must not be reported as successful tool-use acceptance.

## PR98 Publication and Resumed Acceptance

PR98 merged as `0f403e8127f827564e7db82bfc02b34207007ac9`. PR CI44738024662 and main CI44742049928 both passed: Linux 8385 tests and 204 subtests; Windows 586 tests with 9 expected skips. The main package and automatic deployment succeeded. Fresh production verification matched the public manifest and ten relevant source/asset hashes, returned health200, and passed the platform/supervisor database checks. Physical Windows new-tool/capture tests passed all 23 cases.

On 2026-11-21, both Mac and Windows were upgraded to signed client `0f403e8127f8-s22` with the previous releases and launch/task backups retained. The original Windows Codex and Claude Code captures were restored from their exact native transcript files under their original Fleet session IDs; Hub acknowledged 121 and 75 events respectively. These imports restore historical evidence and are distinct from automatic capture of a fresh task.

The online main assistant resumed original Codex task `t-58c073e5e5a1`, actually read its worktree directory and `# Agent Fleet`, then received exactly one `_FOLLOW` reply in native session `01a1f8da-c4d4-7d12-a744-092261af176d`. Both signed message receipts succeeded. Independent Hub/native-log verification passed.

A fresh online Codex task `t-a44e8107f8d6` also automatically acquired a session, executed the file-reading tool and completed one original-session follow-up. Independent verification confirmed task/session binding, successful receipts and native ID `01a1f9cd-0f92-7330-9987-722a43b16067`. The machine still retained 2173 capture records and 134720792 bytes during this acceptance; the old task's manual recovery was not used as proof of fresh capture. User defaults remained unchanged.

For original Claude task `t-392976bd1570`, the main assistant successfully configured the existing session to `deepseek-v4-flash` and issued the first file-reading message. Native execution completed successfully in about 58 seconds and returned `# Agent Fleet`, but the main assistant ended its run before that output arrived and therefore did not send the requested `_FOLLOW`. The original task's historical state remains `failed`; recovery of the conversation does not rewrite that result. The remaining gap is addressed in [Native Session Event Waiting Implementation Plan](2026-11-21-native-session-wait.md). Its final follow-up and publication verification remain pending.
