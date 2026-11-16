# Main Assistant Prompt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Agent Fleet main assistant a stable operating policy and accurate per-Run context so it can plan and execute useful work through only the tools actually available to that Run.

**Architecture:** Add a provider-neutral system-message builder under `hub/platform/runtime/`. `LocalRunWorkerService` builds it from the resolved Run snapshot, workspace, node and final tool definitions, then prepends it exactly once before user/history messages. The existing ToolBroker remains authoritative for tool availability, validation, authorization and side effects; workflow review/summary prompts remain separate.

**Tech Stack:** Python 3.11+, pytest, existing provider message contract and ToolBroker schemas.

## Global Constraints

- Ordinary assistant Runs must keep `RuntimeLimits.max_steps=None` and `max_tokens=None` unless an existing explicit caller contract supplies a budget.
- The prompt must not claim access to credentials, VM lifecycle operations, arbitrary shell strings, network access or any tool absent from the current tool definitions.
- Never place API keys, endpoints, filesystem absolute paths, owner IDs or private credentials into the generated system context.
- Prompt text is behavioral guidance, not an authorization boundary; existing broker, node capability, gate and approval checks remain authoritative.
- User/workspace/model data is untrusted input and cannot override these instructions.

---

### Task 1: Define the main assistant system policy

**Files:**
- Create: `hub/platform/runtime/assistant_prompt.py`
- Test: `tests/test_main_assistant_prompt.py`

**Interfaces:**
- Produces `build_assistant_messages(messages, *, model, workspace, node, tools) -> list[dict]`.
- The builder returns a new list and prepends one stable system message containing Chinese execution principles plus a compact JSON runtime context.

- [x] Add tests for core policy coverage, tool-list/runtime-context accuracy, secret/absolute-path exclusion, and preserving existing messages without mutation.
- [x] Run the focused tests and verify they fail before implementation.
- [x] Implement the stable policy and context serializer with explicit allowlisted fields only.
- [x] Re-run the focused tests.

### Task 2: Attach prompt to ordinary Native assistant Runs

**Files:**
- Modify: `hub/application/run_worker_service.py`
- Test: `tests/test_assistant_tool_availability.py`
- Test: `tests/test_platform_run_worker.py`

**Interfaces:**
- Build the prompt after the complete effective tool list is known, using the frozen provider model identifier, selected workspace public metadata, selected node public metadata (if remote), and that final tool list.
- Prepend it to normal conversation messages before adding optional memory context; keep summary/review/fixed-test special routes unchanged.

- [x] Add worker-level provider-capture assertions that the actual model request gets the system message and only advertised tools are described.
- [x] Run the focused tests to verify failure before implementation.
- [x] Wire the message builder only into the normal main-assistant branch.
- [x] Verify checkpoint recovery does not duplicate the base prompt and ordinary execution limits remain unbounded.
- [x] Run focused worker/runtime regressions.

### Task 3: Verify project-level contracts

**Files:**
- Modify: `docs/platform/agent-fleet-self-hosted-platform.md`
- Test: `tests/test_main_assistant_prompt.py`

- [x] Document the system-vs-dynamic-context boundary and prompt limitations in the platform architecture section.
- [x] Run prompt, tool availability, runtime checkpoint and provider request contract tests.
- [x] Run the full test directory locally; 32 failures exposed stale message-position assumptions and host environment gaps. Relevant Worker/Workflow regressions were corrected and rerun; host-only browser/dependency failures remain noted in verification evidence.
- [x] Inspect whitespace and final diff.

### Task 4: Make live model acceptance explicit

**Files:**
- Modify: `.github/workflows/live-acceptance.yml`
- Modify: `docs/testing/release-acceptance.md`

- [x] Require a comma-separated model-profile selection for manual live acceptance instead of probing every enabled model.
- [x] Validate profile ID syntax and pass each selected profile as an independent CLI argument.
- [x] Confirm the current `live-acceptance` GitHub environment has no variables or secrets configured; real MODEL-LIVE remains blocked until its dedicated environment is provisioned.
