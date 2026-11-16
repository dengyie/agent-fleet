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

### Task 7: Recover incomplete model generations without replaying tools

**Evidence:** PR73 mixed Run ended unknown after a received HTTP 200 timed out during the model stream. Two real loopback HTTP/Native regressions reproduce the terminal timeout despite configured retries, including a complete-looking tool call whose stream never completes. A progress projection regression shows that a retry's offset-zero text would be ignored after a partial draft.

**Root cause and minimal design:** Provider retry handling surrounds only request admission; body-consumption errors bypass it. Retry configured transient timeout/network failures and explicitly transient stream errors within the same provider call, before any ModelResponse reaches Native/tool execution. Close each response before retrying, retain per-request unknown usage/correlation, preserve committed tools and checkpoints, and replace only the current incomplete narration when the next request actually produces offset-zero text. Unknown tool outcomes and process-crash recovery remain non-replayed. Exhausted attempts remain unknown; cancellation prevents another dispatch.

- [x] Reproduce two real HTTP generation failures and one progress reset failure.
- [x] Apply provider retry handling and current-draft replacement, then rerun original failures.
- [x] Verify exhaustion, cancellation, response closure, transient-vs-malformed stream errors, cost metadata and no tool replay. Related provider/HTTP/worker/checkpoint regressions: 285 passed; two real Chromium feedback/reload cases passed.
- [x] Reproduce four cases where local progress storage OSError/TimeoutError was mistaken for model transport failure; restrict error conversion to response iteration and verify the original exception, one dispatch and response closure.
- [ ] Publish after regression/CI, then perform exact-release real model and mixed-operation acceptance.

### Task 6: Route ordinary registered-service log reads to the service Node

**Evidence:** Three API/worker/Node regressions failed: normal log reads had no configured reader, while the tool was still advertised. Four adapter regressions additionally showed nonzero log-command exits being reported as successful empty logs and successful Docker stderr being discarded.

**Root cause and scope:** The normal diagnostic composition lacks a Node log reader; the existing fixed adapter was wired only to reconciliation. Reuse signed commands, exact service Node identity, existing timing, journals, checkpoint suspension and receipt recovery. Keep service logs read-only and explicitly capability-gated; do not route them to the selected workspace Node or create a separate task framework.

- [x] Capture failing ordinary local/remote workspace tests and unavailable-tool advertisement.
- [x] Wire a per-Run service log broker to the existing delivery and worker checkpoint lifecycle.
- [x] Verify original three failures turn green without duplicate execution or extended deadlines.
- [x] Capture nonzero log-exit and Docker stderr failures before fixing the adapter.
- [x] Verify target ownership, capability gates, failure/unknown/cancellation, original receipt recovery and CLI configuration. Focused worker/runtime/service regression: 362 passed; selected Ruff checks, diff and UTF-8/no-BOM checks passed.
- [x] Update existing capability docs, release notes and journey gate; run affected regressions and CI.
- [x] Publish through the existing release flow and verify real service logs on the dedicated test Node. PR74 main `1d2296f7cd7dd61ff98b2917972a44f5e586466e`, CI/deploy `43873296262` passed. Independent deployed source hashes, manifest and SQLite quick/FK checks passed. Main-assistant Run `run_aa659977a97e559fd3501fd5` queried the original approval/action and read the real service-Node log marker with redaction, live SSE and conversation recovery. Independent command inventory proved one logs execution and no repeated restart; the original test-service PID stayed 259749.

**Additional observed evidence:** Release `26763de` passed exact deployment and all four selected-model read-only checks. The ordinary mixed Run `run_be7699a5e25c64c09e103e99` successfully retried one HTTP 503, then became unknown after a later model stream timed out; do not resubmit it. Independent service acceptance confirmed actual inspect, pending approval, one operator-approved restart and a changed dedicated process PID. Final observation Run `run_34d8f6fadc23947c4386aeac` read the original successful action, then failed on `service.read_logs/diagnostics_unavailable`, reproducing this log defect online. Separate regression exposed service command/grant URL-safe random IDs containing forbidden reserved substrings; hex generation fixes the two deterministic failures.

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

### Task 8: Keep ordinary code delivery within the requested Git scope

**Evidence:** Ordinary GLM application Run `run_0811eb100de1895184657261` completed its 14 tests, then staged files and attempted an unrequested commit. The missing author configuration caused another failed command, followed by a made-up command-local identity and a successful commit. The application request and repository instructions required implementation, a feature branch and tests, but did not require staging or committing. The current policy does not distinguish code delivery from Git publication.

**Minimal design:** Clarify that code delivery ends with changes and validation unless the user or applicable repository instructions explicitly require staging, committing or pushing. Explicit Git delivery remains supported. Author identity must come from existing configuration or supplied instructions, not an invented value. Keep the tool contract and authority unchanged; no command interception or new execution framework.

**Files:** `hub/platform/runtime/assistant_prompt.py`, `tests/test_main_assistant_prompt.py`, `docs/platform/agent-fleet-self-hosted-platform.md`, and `docs/releases/release-notes.md`.

- [x] Add a prompt-contract regression asserting separate authorization for staging/commit/push and evidence-backed author identity; run `python3 -m pytest -q tests/test_main_assistant_prompt.py` and observe failure before implementation (1 failed, 8 passed).
- [x] Add the targeted policy clause, rerun the failing test and relevant ordinary-worker/checkpoint regressions, and document the delivery boundary. Related regressions: 102 passed; selected Ruff checks and diff check passed.
- [ ] Publish after required CI, verify the exact deployed policy, and run an ordinary repository repair without requesting a commit. Independently confirm meaningful tests, preserved unrelated work, a feature branch, unchanged HEAD and an unchanged index.
- [ ] Complete the original multi-file application's isolation repair through the main assistant and independently rerun its immutable regression; preserve the original interrupted Run and evidence.
- [ ] Repeat exact-release model, mixed-operation, management, artifact, cancellation and UI acceptance, recording each result without substituting older-release evidence.

### Task 9: Retry explicitly upstream-scoped SSE errors before committing a generation

**Evidence:** On exact deployed release `6c958bf`, application continuation `run_41e68aaa8f64754f55127e3e` ended `unknown/provider_stream_error` after one HTTP200 request and no tools. Gateway request `202611161526506539779725890cb636Til49Fc` records channel366 EOF/interrupted stream, HTTP502 `bad_response_body` internally and an already-committed HTTP200 response externally. The gateway's OpenAI terminal-error writer emits `error.type=upstream_error`; Fleet currently ignores this structured scope and only retries three specific error codes. Unknown/message-only errors must retain their existing non-retryable behavior.

**Minimal design:** Treat the explicit SSE `error.type=upstream_error` contract as retryable within the existing provider generation loop. Preserve response closure, request correlation, per-attempt unknown billing, cancellation, configured retry count and the boundary before any executable ModelResponse. Do not inspect arbitrary error-message text or retry malformed/client errors.

**Files:** `hub/platform/providers/openai_compatible.py`, `tests/test_provider_body_retries.py`, and existing release notes.

- [x] Reproduce failure through real loopback HTTP with upstream-scoped `bad_response_body` and `stream_interrupted` errors after an uncommitted tool draft (both fail before implementation).
- [x] Add the targeted classification and rerun the same cases; assert that the recovered tool executes exactly once and the failed draft never executes.
- [x] Run provider transport, body-retry, progress, worker and checkpoint regressions, including existing generic stream-error rejection. Related set: 249 passed; selected Ruff and diff checks passed. Explicit upstream-error exhaustion retains unknown state, and parameter/auth/message-only/malformed scopes do not retry.
- [ ] Release through required CI, then verify the original application scenario and the full exact-release acceptance matrix.

### Task 10: Preserve repository-relative command meaning

**Evidence:** Exact production `ec6d2e3` repaired repository Run `run_407275c058fb4f4fff86f653` passed the immutable four-test suite, but first copied its AGENTS `unittest discover -s .` at workspace root and discovered zero tests. After repairing the source and passing the explicit-directory suite, it reran the equivalent documented command through subprocess cwd. The policy already explains fixed workspace cwd but does not connect that fact to commands quoted by repository instructions. Original event evidence is retained in `repository-ec6d2e31055f/tool-evidence.json`; this is model behavior, not a failed backend directory switch.

**Minimal design:** Add one policy clause interpreting repository command paths relative to their repository, translating only directory context while retaining test scope/arguments/assertions. Reuse a successful equivalent verification receipt on unchanged repaired source; failures, changed code or insufficient coverage still require verification. Do not add command interception, cwd inference, tool limits or a new execution protocol.

**Files:** `hub/platform/runtime/assistant_prompt.py`, the existing platform architecture document, and release notes. Existing prompt/worker/checkpoint tests check integration; live immutable-repository Runs establish actual model behavior, not prompt-string assertions.

- [x] Capture the zero-test invocation and redundant post-fix invocation from the actual completed Run.
- [x] Add the scoped instruction and document the distinction between policy and verified model behavior.
- [x] Run prompt, worker, checkpoint and tool-routing regressions and inspect the complete diff. Six related modules: 102 passed (7.04s); changed-file Ruff, UTF-8/no-BOM and diff checks passed. Live command behavior remains to be measured after deployment.
- [ ] Publish through main CI and verify exact deployment identity.
- [ ] Run the unchanged repository scenario with the three selected primary models; independently inspect tests, HEAD/index preservation, wrong-directory/zero-test commands and redundant successful verification. Keep failures visible and iterate on evidence.
