# Capability Unknown/Isolated Postcheck Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Extend the existing deterministic workspace digest postcheck to capability-bound unknown or isolated workspace writes without replaying the original command, changing its execution fact, or releasing its resource reservation.

**Architecture:** The operator-facing postcheck service derives the original path and expected digest from the private capability command, then creates the same deterministic `reconcile.workspace.digest` command. The helper capability must already be present in the Workflow contract, binding, Hub action registration, and Node local registration. Capability dispatch recognizes only that persisted postcheck descriptor, verifies the original binding and current node policy/report/definition, and injects the original signed capability context into the read-only check. The check uses no new Workflow admission, budget reservation, generation, or target selection; matching evidence is audit-only and leaves the original Command/Run/Workflow unknown with its `held` or `isolated` reservation intact.

**Tech Stack:** Python 3.10+, existing Flask/SQLite/WAL, `CommandPostcheckService`, `CapabilityDispatchRepository`, `NodeCapabilityService`, `NodeToolExecutor`, pytest; no new dependency.

## Global Constraints

- Only an owner-authenticated postcheck for an existing unknown capability-bound `tool.workspace.write` command is eligible.
- The client cannot provide path, content, capability context, node, workflow, generation, or target; all are derived from the original command and persisted binding.
- The generated action is exactly `reconcile.workspace.digest` with `{path}` and `retry_class="read_only"`.
- The postcheck is read-only and has no new Workflow intent, budget reservation, capability reservation, generation, Run, or target migration.
- `held` and `isolated` reservations remain occupied until the original execution has independent stop/completion evidence; a matching digest never proves causality or releases capacity.
- The original Command, platform Run, Workflow step and Workflow execution state remain `unknown`; mismatches and read failures remain unresolved.
- Existing ordinary workspace/service postchecks, capability gate-off behavior, Node journal idempotency, signed command verification and all default-off gates remain unchanged.
- Tests use temporary SQLite, local filesystem and loopback/in-process Flask transport only; no production node, provider, VPS, or external network is contacted.

---

### Task 1: Write the capability postcheck regression

**Files:**
- Modify: `tests/test_platform_postcheck.py`
- Modify: `tests/test_capability_dispatch.py` only if a lower-level guard test is needed

**Interfaces:**
- Existing `POST /api/platform/v1/commands/<command_id>/postcheck` and `GET` routes remain unchanged.
- Existing test capability fixture returns an admitted binding, a signed capability command, and a real Workflow reservation.

- [x] Add a test that creates a real capability-bound workspace write, marks its Command `unknown`, changes the reservation to `isolated`, requests the postcheck, and asserts the queued check has the original node/resource/run scope and a Hub-derived `{path}` only.
- [x] Preserve the red baseline command in the implementation record; the new capability case reached the expected guard failure before the final dispatch fix:

```bash
PYTHONPATH=. python3 -m pytest tests/test_platform_postcheck.py -q -k capability
```

Expected result: the new case fails because `_derive()` rejects the reserved `_fleet_capability` argument or the capability guard rejects the unknown Run.

### Task 2: Derive only the private capability command and keep its audit semantics

**Files:**
- Modify: `hub/application/command_postcheck_service.py`
- Modify: `tests/test_platform_postcheck.py`

**Interfaces:**
- `CommandPostcheckService._derive(owner_id, command_id)` continues to return `(original, path, expected_sha256, check_id)`.
- The derived capability command must pass the existing repository `create_postcheck()` and public DTO redaction unchanged.

- [x] Extend `_derive()` to accept exactly `tool.workspace.write` arguments `{path, content, _fleet_capability}` when the reserved field is a mapping; validate the capability context with `CapabilityExecutionContext.from_command` and reject malformed/foreign bindings as `postcheck_not_supported`.
- [x] Preserve the current non-capability `{path, content}` branch and all existing rejection/status behavior.
- [x] Add assertions that path/content/context are never returned by POST/GET responses, the check id is deterministic, repeated requests enqueue one command, and the original Command remains `unknown` after matched, mismatch and failed checks.
- [x] Run:

```bash
PYTHONPATH=. python3 -m pytest tests/test_platform_postcheck.py -q
```

Expected result: ordinary postchecks pass; the new case reaches capability dispatch and fails only at the unknown/isolated capability guard.

### Task 3: Permit one persisted capability postcheck through Hub dispatch

**Files:**
- Modify: `hub/infrastructure/capability_dispatch.py`
- Modify: `hub/infrastructure/command_repository.py` only if a bounded descriptor lookup helper is required
- Modify: `tests/test_capability_dispatch.py`
- Modify: `tests/test_platform_postcheck.py`

**Interfaces:**
- `CapabilityDispatchRepository.prepare_command(connection, command, now) -> PlatformCommand` remains the only trusted context injection entry.
- `CapabilityDispatchRepository.guard_command(connection, command, now) -> None` continues to fence ordinary capability commands and now accepts only a descriptor-backed postcheck.

- [x] Add a private postcheck lookup keyed by `check_command_id` that requires `platform_command_postchecks.kind="workspace_digest"`, original owner/status `unknown`, original action `tool.workspace.write`, exact check action/path/resource/node/run, and a deterministic descriptor row.
- [x] Read the original `_fleet_capability` context, locate its `pinned_dispatch`, and verify binding digest, owner/workspace, generation, original target and original payload hash. Require current step/workflow state to be `unknown` and reservation state to be `held` or `isolated`; reject cancelled, missing, released, foreign or non-current records.
- [x] Reuse the original definition/action registration and current Node offer/policy/credential checks, but do not require the original Run to be `running` and do not create/renew a reservation. The check remains fixed to the original physical node.
- [x] Inject the original `CapabilityExecutionContext` into the generated check, clamp expiry to the original safe deadline when present, and require `read_only`/`reconcile.workspace.digest` exactly. Do not accept a caller-supplied marker or context.
- [x] Keep ordinary active capability dispatch unchanged; ensure gate-off readers reject the persisted capability postcheck without producing an unbound fallback.
- [x] Add tests for held and isolated states, wrong path/node/run/check id, missing descriptor, released reservation, stale capability definition/report, and unchanged reservation/Workflow/Run after every result.
- [x] Run:

```bash
PYTHONPATH=. python3 -m pytest tests/test_platform_postcheck.py tests/test_capability_dispatch.py -q
```

Expected result: all focused tests pass with no changed ordinary capability dispatch behavior.

### Task 4: Full local verification and contract synchronization

**Files:**
- Modify: `docs/superpowers/specs/2026-10-20-artifact-capability-integration-contract.md`
- Modify: `docs/superpowers/specs/2026-10-20-workflow-execution-contract.md`
- Modify: `docs/superpowers/plans/2026-10-20-workflow-platform-roadmap.md`
- Modify: `docs/testing/workflow-platform-acceptance.md`
- Modify: `docs/testing/README.md`
- Modify: `docs/platform/workflow-platform/README.md`
- Modify: `docs/platform/workflow-platform/implementation-progress.md`
- Modify: `docs/testing/journeys.json`

**Interfaces:**
- Required selector registration stays limited to the implemented local slice; it must not mark complete WF-05, Linux/Windows, real two-node, or external live acceptance.
- Documentation records the evidence-only causal boundary and keeps all capability/transfer gates default-off.

- [x] Add a dedicated plan/spec paragraph: a capability postcheck is allowed only for persisted unknown/isolated workspace writes, matches current evidence only, and never releases the reservation or rewrites execution state.
- [x] Register focused selectors in NODE-01/WF-08 without duplicating existing ordinary postcheck selectors; keep capability receive/export and service postchecks outside this slice.
- [x] Run focused Python 3.10/3.13 regressions, compileall, all fixture `node --check`, UTF-8 no-BOM/AST, Markdown local-link/anchor verification and `git diff --check`.
- [x] Freeze the source/matrix and run the complete browser-enabled `--require-journeys` gate, preserving the macOS Linux pidfd/`/proc` incomplete result, dirty status and external `not_run` evidence.

## Exit Criteria

- A matched or mismatched capability postcheck reaches the original fixed Node through the existing signed command/journal path.
- The postcheck never replays the original write, exposes content/context/secrets, creates a second reservation, or moves the target.
- Original Command, Run, Workflow step/execution and held/isolated reservation facts remain unchanged; only bounded postcheck/audit evidence is added.
- Existing ordinary postchecks and capability dispatch regressions pass, and documentation/report evidence explicitly preserves the remaining platform and external gaps.
