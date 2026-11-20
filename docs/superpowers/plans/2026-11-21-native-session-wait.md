# Native Session Event Waiting Implementation Plan

> **For agentic workers:** Execute the tasks inline, preserving the original task, session and command identities. Use evidence-driven-bugfix and verification-before-completion for each verification checkpoint.

**Goal:** Let the main assistant wait for a remote Agent's actual output and finish the requested conversation after message delivery succeeds.

**Architecture:** Add an optional bounded wait to the existing Hub-only `session.read_events` adapter. Reuse `SessionService.session_events` and its stable ingestion cursor; never hold a database transaction while waiting. Update the fixed tool contract and prompt to distinguish command application from the Agent's completed work.

**Tech Stack:** Python, Flask, SQLite session repositories, existing main-assistant tools and Windows native Agent continuations.

## Global Constraints

- Work on `codex/native-session-wait`, based on published main `0f403e8127f827564e7db82bfc02b34207007ac9`; preserve all other worktrees.
- Accept only integer `wait_seconds` values from 0 through 30. The default remains an immediate read.
- Preserve pagination, redaction, byte limits, source filtering and current administrator authorization. Recheck authorization while waiting and before returning data.
- A single empty wait is not a remote task timeout. Follow the original session and command without repeated messages or workspace sleeps.
- Retain original failures, native logs, backups and rollback releases. Produce UTF-8 without BOM. Use the existing full CI and automatic production deployment.

## Failing Evidence

- After PR98 and signed client `0f403e8127f8-s22` deployment, the online main assistant configured the original Windows Claude Code session to `deepseek-v4-flash` and sent one file-reading message.
- Run `run_39fba6510878f72830b8523e` ended while the original session was still running. It correctly declined to claim success, but left the requested follow-up unfinished.
- Native execution completed successfully about 58 seconds after starting, returning the original working directory and `# Agent Fleet` under marker `FLEET_MAIN_DISPATCH_CLAUDE_CODE_20261120_FIRST`.
- `hub/platform/session_tools.py` and `AgentSessionService._execute` expose only immediate session-event reads. Task long polling does not help after the original task has already ended and a native continuation is running.
- Original evidence is retained in `/Users/mango/.cache/agent-fleet/verification/agent-dispatch-reliability-20261120/claude_code-continuation.json`, its events file and `claude-observation.json`.

## Task 1: Reproduce and Add Bounded Waiting

**Files:**

- Create: `tests/test_assistant_session_wait.py`
- Modify: `hub/application/agent_session_service.py`

- [x] Add an asynchronous regression using the existing VM-session fixture. Start a publisher that waits until an empty read is observed, then ingest a new output event for the same session. Request `session.read_events` after the old cursor with `wait_seconds=1` and require the new output and cursor.

```python
result = execute(app, 'session.read_events', {
    'session_id': 's1', 'after_cursor': '2', 'wait_seconds': 1,
})
assert [row['payload']['text'] for row in result['events']] == ['late output']
assert result['next_cursor'] == '3'
```

- [x] Add deadline and authorization tests using a local fake clock assigned only to the adapter module. Cover an empty deadline preserving the cursor, already available events returning immediately, default immediate reads, rejected non-integer/out-of-range values, and administrator revocation during a wait.
- [x] Run `python3 -m pytest tests/test_assistant_session_wait.py -q`. Require the supported-wait cases to fail against the old argument contract before implementing.
- [x] Import `time`, add `wait_seconds` to the read-event argument allowlist, validate it through `_integer`, and wrap the existing event query with this bounded loop:

```python
wait_seconds = _integer(args.get('wait_seconds', 0), 0, 30)
deadline = time.monotonic() + wait_seconds
while True:
    if not self._authorized(owner_id):
        raise ApplicationError('forbidden', 'forbidden', 403)
    page = self.sessions.session_events(
        session_id, limit=limit, after_cursor=int(after),
        include_source=include_source,
    )
    remaining = deadline - time.monotonic()
    if page['events'] or remaining <= 0:
        break
    time.sleep(min(0.1, remaining))
if not self._authorized(owner_id):
    raise ApplicationError('forbidden', 'forbidden', 403)
```

- [x] Keep the existing response assembly and 128 KiB limit unchanged. Run the new tests and existing VM-session regression suite.

## Task 2: Expose and Explain the Wait Contract

**Files:**

- Modify: `hub/platform/session_tools.py`
- Modify: `hub/platform/runtime/assistant_prompt.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/test_assistant_session_wait.py`

- [x] Expose the optional schema property:

```python
'wait_seconds': {'type': 'integer', 'minimum': 0, 'maximum': 30}
```

- [x] Explain that `session.read_events(wait_seconds=30)` waits for new events after `next_cursor`, and that an empty bounded wait leaves the remote work pending. Explain that `session.get_command` success proves application of the control, and actual completion must be verified from the session's later tool results and response.
- [x] Update the fixed prompt to continue waiting on the same native session while it is busy; stop only when the requested output/follow-up is complete, the user cancels, or actual error evidence identifies a blocker.
- [x] Assert the schema limits and immediate default, and exercise waiting through the VM main-assistant worker. Add the applicable new suite to Windows CI so the shared assistant contract remains portable.
- [x] Keep the portable waiting tests on real session/transcript repositories. Run the VM Hub Worker integration on POSIX, where its existing private-storage permissions are supported.
- [x] Run `python3 -m pytest tests/test_assistant_session_wait.py tests/test_assistant_vm_sessions.py tests/test_main_assistant_prompt.py tests/test_assistant_agent_tasks.py tests/test_session_model_takeover.py -q` plus repository Ruff and `git diff --check`.

## Task 3: Publish and Verify the Original Workflow

**Files:**

- Modify: `docs/superpowers/plans/2026-11-20-assistant-agent-tasks.md`
- Update: the existing canonical operations note and routes on the Obsidian vault's primary `master`.

- [x] Record PR97/98 publication, signed Mac/Windows upgrade, original task recovery and the remaining native-wait gap in the existing task-dispatch plan.
- [ ] Commit the bounded-wait change, push its branch and create a PR describing the observed early stop and the resulting wait behavior. Follow full Linux/Windows CI, rebase onto latest main, merge and follow the original automatic deployment run.
- [ ] Verify the deployed manifest, relevant source hashes, health and database checks.
- [ ] Continue the original Claude main-assistant conversation without resending its successful first message. Require the new wait tool while issuing exactly one outstanding `_FOLLOW` message, a successful original command receipt and the same native session's actual answer.
- [ ] Independently query the original task/session/run events and receipts. Verify unique messages, README tool output, native identity preservation, model configuration and unchanged user defaults. Verify the fresh post-PR98 Codex task's automatic capture with the historical backlog still present.
- [ ] Record exact final evidence and limits in the PR description and canonical vault note. Do not claim the old upstream Claude 503 channel was repaired; the original session was recovered through a working model profile.

## Verification Checkpoints

Baseline main-assistant/session tests passed: `42 passed in 2.60s` before implementation. The new suite first reported `6 failed, 7 passed`, covering the absent wait contract and authorization checks. After implementation and an additional VM-worker integration case, the relevant regression command passed all 91 tests in 9.36 seconds; repository Ruff and diff checks passed. Only fresh publication results and real online native output can complete the final checkpoint.

The first PR CI (`44781253256`, head `0c34934`) passed Linux but reported `587 passed, 9 skipped, 13 errors` on Windows. All errors occurred in the imported full-Hub fixture before waiting ran: the existing Supervisor private store requires POSIX `os.getuid`. The read tests now construct real session/transcript repositories without starting that unrelated private store; only the full VM Hub Worker integration is POSIX-scoped. The affected local suite was rerun with `91 passed in 9.13s`, and Ruff/diff checks passed. The complete initial failure log remains in the verification evidence directory.

The physical Windows host reran the new suite on an isolated verification source copy with the host's unrelated `automation-kit.tests` package excluded: `13 passed, 1 skipped in 3.04s`. The sole skip is the full POSIX Hub Worker integration, which remains active in Linux CI. The verification-copy preparation errors and successful output are all retained in separate evidence files.
