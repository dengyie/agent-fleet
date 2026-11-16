# Main Assistant Live Acceptance Iteration Plan

> **For agentic workers:** Execute this plan inline in the current isolated worktree. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve the Agent Fleet main assistant's project-operation behavior and prove the deployed release through exact-profile, account-authenticated live Runs.

**Architecture:** Keep the main assistant policy concise and provider-neutral, with task guidance that follows existing tool contracts. Extend the existing bounded MODEL-LIVE acceptance path to prove streaming progress, an allowed workspace tool receipt, final completion, and conversation recovery. Keep provider retries in the provider runtime and permissions in ToolBroker; do not expand production authority for acceptance.

**Tech Stack:** Python 3.10+, Flask/SQLite platform APIs, pytest, GitHub Actions, deployed Agent Fleet Hub.

## Global Constraints

- Preserve existing worktree changes and develop from the latest `origin/main` in an isolated feature branch.
- Ordinary assistant Runs retain unbounded step and token defaults; explicit workflow contracts retain their declared budgets.
- Live acceptance calls only exact model profile IDs supplied by the user or workflow input; never implicitly enumerate enabled models.
- The live acceptance route remains restricted to `workspace.list`. Separate ordinary assistant acceptance uses explicitly designated test conversations and directories; it may create test files or save unchanged defaults, but must not inspect secrets or mutate unrelated services/data.
- Unknown or timed-out Runs are evidence of an unconfirmed outcome and must not be resubmitted automatically.
- A successful deployment, `/healthz`, or static manifest alone does not prove main-assistant functionality.
- Do not claim full live acceptance until all selected primary profiles and the configured fallback have passed the required Run evidence checks.

---

### Task 1: Improve main-assistant operating guidance

**Files:**
- Modify: `hub/platform/runtime/assistant_prompt.py`
- Test: `tests/test_main_assistant_prompt.py`
- Test: `tests/test_assistant_tool_availability.py`
- Test: `tests/test_platform_run_worker.py`

**Interfaces:**
- Preserve `build_assistant_messages(messages, *, model, workspace, node, tools)`.
- Keep prompt context allowlisted and ensure the policy is attached only to ordinary main-assistant Runs.

- [x] Add behavior-contract assertions for repository inspection, platform defaults, service evidence, actual tool availability, progress reporting, unknown side-effect handling, and verified completion.
- [x] Run the focused tests and inspect the current tool definitions and worker wiring.
- [x] Implement only guidance the actual current tool catalog can support; keep permissions and retry mechanics in code.
- [x] Run focused prompt, worker, checkpoint, and tool-availability regressions.

### Task 2: Strengthen MODEL-LIVE evidence

**Files:**
- Modify: `tools/platform/acceptance_check.py`
- Modify: `tests/test_platform_acceptance_check.py`
- Modify: `tests/test_acceptance_flow.py`
- Modify: `.github/workflows/live-acceptance.yml`
- Modify: `docs/testing/release-acceptance.md`

**Interfaces:**
- Keep repeated `--model PROFILE_ID` as the only selection used by CI.
- Extend bounded report fields to prove accepted response, visible pre-terminal progress, `workspace.list` tool success, final reply, and conversation recovery without storing prompt/reply/file contents.

- [x] Add regression tests for missing streamed progress, missing tool activity/result, terminal-event ordering, and exact four-profile workflow defaults.
- [x] Confirm the public Run DTO exposes bounded progress during execution and event API returns ordered public events.
- [x] Implement live acceptance checks using the existing authenticated Run/event/conversation APIs; retain unknown outcomes without replay.
- [x] Pin the workflow's suggested profile order to `deepseek-v4-flash`, `glm-5.3-flash`, `deepseek-v4-pro`, then `gpt-6-luna` as fallback; allow explicit override without implicit catalog enumeration.
- [x] Verify profile syntax, duplicate rejection, failure continuation across selected profiles, and redacted artifact output.
- [x] Run the focused acceptance and CLI tests.

### Task 3: Full regression and production deployment

**Files:**
- Modify only files required by Tasks 1-2.

- [ ] Run changed-file tests, relevant browser/runtime tests, lint, and repository-required CI checks. Local relevant regression set: 149 passed; CI remains required.
- [ ] Review the complete diff against `origin/main`, preserving unrelated work.
- [ ] Push a feature branch and merge only after required CI passes.
- [ ] Wait for the main deployment workflow, then verify the deployed manifest SHA and `/healthz`.

### Task 4: Exact-profile live acceptance and iteration

**Files:**
- No source edits unless a failed evidence check identifies a reproducible defect.

- [ ] Run MODEL-LIVE against the deployed SHA for the three requested primary profiles and `gpt-6-luna` fallback, one selected profile at a time in the configured order.
- [ ] For every profile, verify accepted submission, progress visible before terminal completion, successful `workspace.list`, nonempty final result, conversation recovery, and account logout revocation.
- [ ] For each failure, inspect its Run/events and provider request metadata before changing code; do not duplicate timed-out Runs.
- [ ] Repeat deployment and exact-profile acceptance after each root-cause fix until all required checks pass.
- [ ] Record any capability that cannot be exercised in production as blocked/not-run with concrete evidence; do not report it as passed.

### Task 5: Resolve observed environment and management gaps

**Evidence:** On release `8c2df56`, VM coding Run `run_297d33cd255c4cef62d844f9` guessed an unrelated absolute path and initially discovered zero tests before correcting both. Earlier archive acceptance could not find a target outside the bounded conversation list. The worker currently excludes Hub controls whenever a remote execution node is selected.

**Files:** `hub/platform/runtime/assistant_prompt.py`, `hub/platform/assistant_tool_broker.py`, `hub/application/run_worker_service.py`, `tests/test_main_assistant_prompt.py`, `tests/test_assistant_tool_availability.py`, `tests/test_assistant_tool_routing.py`, and the existing platform architecture document.

**Interfaces:** Keep the existing prompt builder signature. `AssistantToolBroker(hub, remote, *, hub_tools)` dispatches `execute` to the existing scoped Hub broker for the explicitly selected management tools and to the existing remote broker otherwise; `observe` observes the original remote command. `LocalRunWorkerService` derives Hub tools only from installed management services and only for ordinary main-assistant Runs. It passes the exact union of Hub controls and Node-capable tools to the provider. Dedicated acceptance, summary and Workflow contracts retain their allowlists.

- [x] Add failing prompt-contract tests for relative paths, test discovery/counts and bounded conversation lookup; implement guidance and verify the existing worker tests.
- [x] Add a worker regression that reads defaults on Hub, executes a real fixture workspace tool on Node, then renames the owned test conversation on Hub in the same Run; assert Node never receives management commands.
- [x] Verify absent services remain unadvertised, other-owner targets are rejected and a remote receipt checkpoint resumes without repeating earlier management mutations.
- [x] Implement the explicit tool router and effective tool union without adding VM lifecycle, remote artifact or MCP claims.
- [ ] Run prompt, broker, worker, checkpoint, cancellation and dedicated acceptance regressions; inspect full diff and publish through CI.
- [ ] On the deployed release, run an ordinary mixed management/VM task, independently check the file and conversation changes, and re-run the four exact model profiles and UI progress/recovery checks.

**Verified baseline (before Task 5 deployment):** PR69 main `8c2df56b7dbc60ab5be0df1779eb754ec74f4a63`, CI/deploy `43811885952`, deployment receipt `status=deployed/phase=succeeded`, served version and selected source hashes match; production SQLite quick/FK checks passed. All four approved profiles passed SSE/read-only tool/final reply/recovery checks. Ordinary management continuation, archive/restore, unchanged-default save, VM file generation with independent six-test execution, and local artifact byte download passed as separate scenarios. Chrome verified immediate user feedback, unfinished live GLM text, folded 14-request metadata, readable exec results, desktop/mobile reload, no overflow/page errors and logout revocation. These establish the named baseline capabilities only; mixed Hub/VM routing and new prompt behavior remain unverified online until the Task 5 release.
