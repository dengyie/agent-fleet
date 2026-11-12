# Durable MCP Run Pinning Implementation Plan

> **For agentic workers:** Use `executing-plans` to execute this plan in the existing isolated worktree, with evidence before each fix claim.

**Goal:** Enforce the existing contract that an old Run never acquires a newer MCP definition or new MCP authority after queueing or restart.

**Architecture:** Persist a bounded map of immutable tool pins in the existing private Run configuration. The connector catalog compares each pin with its admitted registration and schema in O(1); ToolBroker exposes and invokes only these pins. Reuse the existing Run transaction, error flow, gate, lease and worker; add no table, queue, worker, package or management endpoint.

**Tech Stack:** Python 3.10+, stdlib dataclasses/hashlib, existing Flask/SQLite services, pytest and the current MCP connector.

## Global Constraints

- Baseline: [tool contract §7.1/7.4/7.5](../specs/2026-10-20-artifact-capability-integration-contract.md#75-run-工具授权与定义固定), original F5 plan and existing immutable Run configuration contract.
- Work only in `codex/workflow-platform-docs`; preserve the protected primary worktree and all existing work.
- UTF-8 without BOM; no commit, push, merge, deployment, real external call or additional download.
- MCP remains fixed-endpoint/read-only and default-off. Scope, policy and resource lease still apply on every call.
- A pin contains only tool name, exact revision and a SHA-256 registration/definition digest; at most 20 pins. Never persist resolved secrets, endpoint text, descriptions or schemas in this map.
- Missing pins mean no MCP authority. Queued pre-feature/gate-off Runs do not borrow current catalog permissions.
- A changed or missing selected definition fails closed before a model request. A new catalog with revision 1 must also match the old digest.
- Workflow Run targets may include these administrator-created pins in their existing immutable `config_snapshot`; absence grants no MCP authority. Do not derive pins dynamically during Workflow dispatch/recovery.
- This repair advances the complete platform objective. It does not close external pilots, cross-platform evidence, durable catalog administration or external Agent Workflow dispatch.

## Observed failing evidence

`PYTHONPATH=. python3 -m pytest tests/test_mcp_run_pinning.py -q --junitxml=/tmp/agent-fleet-mcp-run-pinning-before.xml --tb=short`

Observed **5 failed / 1 passed**. A queued Run executed an explicitly admitted new schema, a restarted catalog reused revision 1 for a different schema, and previously unauthorized Runs gained a tool after restart. The Run snapshot contained no MCP pins. The unchanged-restart control passed. The root cause is worker-time selection from mutable process-local state rather than the durable Run configuration.

## Task 1: Immutable pins and catalog comparisons

**Files:**
- Create `hub/domain/mcp_tool_pins.py`.
- Modify `hub/application/connector_catalog.py`.
- Test `tests/test_mcp_run_pinning.py` and `tests/test_mcp_connector.py`.

**Interfaces:**
- `MCPToolPin(public_name: str, revision: int, definition_digest: str)`.
- `parse_mcp_tool_pins(value: object) -> tuple[MCPToolPin, ...]` rejects unknown fields, more than 20 entries, malformed public names/digests, bool or out-of-range revisions.
- `ConnectorCatalog.run_snapshot(*, owner_id: str, workspace_id: str) -> dict[str, dict[str, object]]` returns copied public-name→`{revision, definition_digest}` values.
- `ConnectorCatalog.tool_definitions(..., expected_pins: tuple[MCPToolPin, ...] | None = None)` validates the full selected set before copying any definitions.
- `ConnectorCatalog.call(..., expected_pin: MCPToolPin | None = None)` checks the exact admitted pin inside its short metadata lock, then uses the existing independent connector snapshot outside the lock.

- [x] Add deterministic malformed/scope/digest/pin-copy tests and observe failures.
- [x] Add the pure pin DTO/parser and compute a digest at admission from fixed config, protocol/server version, tool name, input/output hashes and description hash. Exclude resolved secrets; the private config's SecretRef is only an input to the hash.
- [x] Store the immutable pin with each catalog entry. Enumeration returns at most 20 pins; call comparison is one dictionary lookup and value comparison, with no lock across network I/O.
- [x] Check schema drift, pending approval and threaded refresh regressions:

```bash
PYTHONPATH=. python3 -m pytest tests/test_mcp_connector.py tests/test_mcp_run_pinning.py -q
```

## Task 2: Freeze at submission and enforce in the worker

**Files:**
- Modify `hub/application/conversation_service.py` and `hub/bootstrap.py`.
- Modify `hub/application/run_worker_service.py` and `tools/platform/tool_broker.py`.
- Test `tests/test_mcp_run_pinning.py`, `test_mcp_http.py`, `test_platform_run_worker.py`, `test_workflow_dispatch.py` and `test_workflow_gates.py`.

**Interfaces:**
- `ConversationService(..., mcp_catalog=None, mcp_enabled: bool = False)` receives the same explicitly injected catalog as the worker.
- On a new local, non-acceptance Run, after server-owned defaults resolve and before `append_turn`, freeze:

```python
snapshot["mcp_tools"] = self.mcp_catalog.run_snapshot(
    owner_id=owner_id, workspace_id=snapshot["workspace_id"],
)
```

- Already-accepted tokens return their original configuration before consulting any mutable catalog. Client overrides cannot supply pins.
- `ToolBroker` and its `tool_definitions` method accept the private snapshot through the `mcp_pins` keyword. Omission uses a private sentinel for lower-level ephemeral callers' existing explicit catalog behavior; explicit JSON null is invalid. The worker always supplies `config.get("mcp_tools", {})`, including an empty map for legacy Runs.
- Broker parsing creates immutable pins, intersects the existing allowlist and validates exact pins again immediately before invocation. Worker definitions use the same frozen set, preserving the total 20-tool budget.

- [x] Freeze pins only on a newly accepted local Run; keep gate-off, remote and acceptance behavior unchanged.
- [x] Enforce missing/changed/rebuilt or disabled catalog and revoked gate cases before model I/O; check the pin again when the actual call happens.
- [x] Verify original failing evidence and the unchanged-restart control, same-token reuse, forged overrides, owner/workspace mismatch, malformed pins, post-declaration drift and gate-off compatibility.
- [x] Run related regressions on both installed Python versions:

```bash
PYTHONPATH=. python3 -m pytest tests/test_mcp_run_pinning.py tests/test_mcp_connector.py tests/test_mcp_http.py tests/test_platform_run_worker.py tests/test_workflow_dispatch.py tests/test_workflow_gates.py tests/test_platform_conversation.py -q
```

All listed suites exist; no runtime dependency installation is required.

The initial 12-module related set passed on Python 3.13.7 (318 tests, 42.41 seconds) and Python 3.10.21 (318 tests, 46.44 seconds). JUnit files are `/tmp/agent-fleet-mcp-run-pinning-related-python313.xml` and `/tmp/agent-fleet-mcp-run-pinning-related-python310.xml`.

Three later failing revocation cases (`/tmp/agent-fleet-mcp-run-revocation-before.xml`) proved model I/O still happened after the catalog/gate disappeared. Explicit nonempty pins now reject before model I/O, while empty/sentinel gate-off calls retain their previous behavior. The pinning/catalog subset passed 92 tests; the final 12-module set passed 321 tests on each interpreter (25.09/27.58 seconds). Final JUnit files are `/tmp/agent-fleet-mcp-run-pinning-final-related-python313.xml` and `/tmp/agent-fleet-mcp-run-pinning-final-related-python310.xml`. These are related regressions; the complete gate is recorded separately in implementation-progress.

## Task 3: Register evidence and verify the resulting tree

**Files:**
- Update `docs/testing/journeys.json`, `docs/testing/workflow-platform-acceptance.md`, Workflow navigation/progress and the F5 roadmap.

- [x] Register every new pinning selector under WF-06, keeping all existing selectors and external checks. The 12 pinning selectors add 34 cases to the original 66 selectors / 131 cases; the resulting 78 selectors / 165 cases are checked by collection and the final gate.
- [x] Run both Python versions' `compileall`, 16 frontend CJS syntax checks, UTF-8/no-BOM checks on 804 text files, 567 Python AST parses, 105 local links in eight current documents, and `git diff --check`.
- [x] Freeze source/matrix digests and run the complete browser-enabled `--require-journeys` command. The final run has 4743 passed, zero failures/errors, five platform skips and 195 passed subtests; 41/42 journeys pass, including WF-06's 78 selectors / 165 cases. Strict exit 1 remains for Linux RELEASE-01; all seven external checks remain not_run.
- [x] Record the red/green regression, actual final scope and remaining original objective requirements in implementation-progress. Preserve the earlier account-browser timeout and its passing reruns as an unresolved intermittent risk. Keep the full platform goal active while any requirement remains unverified.

Final evidence: `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-mcp-run-pinning-full-20261025-4tyl0nwf/`. All 806 source digests match before/after the gate; the matrix digest is `e2062d2927c3a894700251a0e18c493b544f29f8726f574d2265abc13de78c3f`. The 28 PNGs pass CRC/dimension/IEND checks. This completes this local repair plan; the original platform objective remains active.
