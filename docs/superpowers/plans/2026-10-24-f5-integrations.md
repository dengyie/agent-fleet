# F5 Integration and Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification checkpoints.

**Goal:** Deliver the documented F5 integration boundary as three default-off, independently testable slices: a pinned read-only HTTP MCP connector, an explicit-capability external Agent adapter, and a narrow Fleet MCP export.

**Architecture:** Domain contracts remain framework-free. The MCP connector owns protocol framing, bounds, schema pinning, output validation and a single-attempt transport port; the connector catalog owns registration/revision and ToolBroker exposure. External Agent adapters expose only declared capabilities and preserve `unknown` when external identity or cancellation evidence is missing. Fleet MCP routes expose three read-only/scoped workflow operations through a separate integration identity and stable idempotency token.

**Tech Stack:** Python 3.10+, stdlib dataclasses/json/urllib, existing bounded HTTP transport patterns, Flask, SQLite repositories and pytest. The existing jsonschema dependency is now declared in runtime requirements (`>=4.23,<5`) because both enabled MCP boundaries validate schemas at runtime.

## Global Constraints

- `platform_mcp_connectors_enabled=false`, `platform_external_agents_enabled=false`, and `platform_fleet_mcp_enabled=false` by default.
- First MCP version is stateless HTTP JSON-RPC using protocol version `2026-07-28`; no stdio, dynamic URL, browser login, package installation, or arbitrary plugins.
- MCP discovery is bounded to 100 tools, 256 KiB total schema, 32 KiB per schema, depth 16, and no external schema references; runtime exposure defaults to 20 selected tools.
- Server annotations, descriptions, and `readOnlyHint` never grant Fleet authority; risk/retry/policy are administrator-pinned.
- One MCP call is one transport attempt; timeout maps to the registered retry contract and never triggers a blind retry.
- Secrets are referenced by `SecretRef` and resolved only at call time through the existing SecretBroker contract; raw values never enter snapshots, errors, events, or responses.
- External Agent sends are never repeated unless the adapter declares `submit_idempotent` and a lookup/reconciliation proof is available.
- All API-visible 64-bit identifiers are strings; all timestamps are UTC/ISO-8601 or injected monotonic/epoch values.
- Existing fixed ToolBroker tools and all gates remain behaviorally unchanged when F5 gates are off.

---

### Task 1: Read-only MCP connector and catalog

**Files:**
- Create: `tools/platform/connectors/__init__.py`
- Create: `tools/platform/connectors/mcp.py`
- Create: `hub/application/connector_catalog.py`
- Modify: `tools/platform/tool_broker.py`
- Modify: `hub/config.py`
- Test: `tests/test_mcp_connector.py`

**Interfaces:**
- `MCPTransport.request(endpoint: str, *, headers: Mapping[str, str], body: bytes, timeout_s: float) -> MCPTransportResponse`
- `MCPConnector.discover() -> MCPDiscovery`
- `MCPConnector.call(tool_name: str, arguments: Mapping[str, Any]) -> MCPCallResult`
- `ConnectorCatalog.register(...)`, `.discover(...)`, `.tool_definitions(...)`, `.call(...)`
- `ToolBroker` accepts optional `mcp_catalog` and `mcp_enabled` and exposes only catalog-selected names.

- [x] Add failing tests for protocol/version, discovery bounds, schema pinning, input/output validation, timeout/no-retry, secret redaction, and gate-off ToolBroker behavior.
- [x] Implement bounded registration and one-attempt connector protocol; validate schema digests before use.
- [x] Implement process-local catalog revision pinning, pending revision admission and ToolBroker dispatch; short metadata locks never span network I/O.
- [x] Add the default-off configuration flag and explicit local worker injection.
- [x] Run connector/provider suites and a real HTTPS Native Run; gate-off/acceptance policies block the MCP call.

### Task 2: External Agent adapter contracts

This task records the original contract-only slice. The follow-up [external Workflow dispatch plan](2026-10-25-external-agent-workflow-dispatch.md) moves the framework-free contracts into `hub/domain/external_agent.py` with compatibility exports here, adds explicit `.lookup`, and connects the original coordinator/admission to durable target and task pins. The unchecked wider F5 requirements remain separate from that local implementation.

**Files:**
- Create: `tools/platform/external_agents/__init__.py`
- Create: `tools/platform/external_agents/base.py`
- Create: `tools/platform/external_agents/muse.py`
- Test: `tests/test_external_agent_adapter.py`

**Interfaces:**
- `ExternalAgentCapabilities`
- `ExternalTaskRef`, `ExternalTaskStatus`, `CancelReceipt`
- `ExternalAgentAdapter.submit`, `.inspect`, `.request_cancel`, `.export_outputs`
- `MuseExternalAgentAdapter` wraps an injected controlled bridge and maps unsupported/uncertain evidence explicitly.

- [x] Test capability consistency, uncertain send, foreign task evidence and requested versus confirmed cancellation; Muse export explicitly returns unsupported.
- [x] Implement framework-free base contracts and state mapping.
- [x] Implement Muse adapter over the existing controlled bridge without copying account/approval policy. Its declared idempotence/lookup rely on that bridge; Workflow dispatch was outside this original task and is now implemented by the follow-up plan.
- [x] Run the adapter and existing Muse regression suites. Real external Agent verification remains separate.

### Task 3: Narrow Fleet MCP export

**Files:**
- Create: `hub/application/integration_identity.py`
- Create: `hub/http/fleet_mcp_routes.py`
- Modify: `hub/web.py`, `hub/bootstrap.py`, `hub/config.py`
- Test: `tests/test_fleet_mcp_boundary.py`

**Interfaces:**
- `IntegrationIdentity` and token verifier bound to owner, scopes, templates, workspaces, budget.
- Flask MCP endpoint implements `tools/list` and `tools/call` for exactly `workflow.create_from_template`, `workflow.get`, `workflow.list_artifacts`.
- Stable `client_token` maps to one canonical parameter hash; different content conflicts; revocation blocks immediately.

- [x] Add failing HTTP/MCP-client tests for discovery, auth, scope, forbidden methods, idempotency conflict, result limits, and revocation.
- [x] Implement identity validation and bounded JSON-RPC route; workflow ownership derives from persistent scoped token namespaces after identity reconstruction.
- [x] Wire the three existing workflow application operations with budget checks before creation.
- [x] Run the HTTPS client and boundary/auth suites, including account-enabled Bearer isolation.

### Verification and documentation

- [x] Run focused F5/worker/auth/provider/Muse suites under Python 3.10.21 and 3.13.7: 255 passed each.
- [x] Run compileall, frontend syntax checks, `git diff --check`, and the existing workflow regression subset. The unchanged full-suite repeat has 4709 passed and 5 platform skips; the strict journey gate still exits 1 for Linux-only RELEASE-01 evidence.
- [x] Update progress, acceptance, and journey documents only with evidence-backed status; keep real external MCP/Agent pilots `not_run`. Preserve the first run's three intermittent failures and the unchanged passing reruns without claiming a root-cause fix.
- [x] Record protocol source and selected version in the implementation-progress entry.

The original three tasks did not deliver a remote MCP/Muse pilot, persistent connector catalog/identity administration, configuration UI or external output pipeline. The follow-up identity plan below closes persistent registration/revocation; the other wider F5 requirements remain open. All three gates remain off by default. Local boundary evidence must not be used as proof of these wider capabilities.

The follow-up [durable Run pinning plan](2026-10-25-mcp-run-pinning.md) closes worker-time catalog borrowing: new local Runs persist private revision/definition digests, missing pins grant no MCP authority, revocation rejects before model I/O, and model declarations and calls both validate the original pins. Its historical final 321-test related set passes on both Python versions, and its full browser gate has 4743 passed and five platform skips, with strict exit 1 for Linux RELEASE-01.

The subsequent [external Workflow dispatch plan](2026-10-25-external-agent-workflow-dispatch.md) delivers local dispatch/recovery through the existing coordinator: immutable admission pins, persistent Muse journal identity, exact task/session matching, no replay on lookup failure, and cancellation-owned I/O. Agent self-report remains separate from validation and acceptance. The initial slice rejects file/context inputs, Node capability requirements and output specifications before sending; Muse cancel/export remain unsupported. The current 20-module related set has 522 passed and two browser-fixture skips on each Python version because those commands omitted the Playwright path; both fixtures pass in the browser-enabled Python 3.13 full gate. The final uninterrupted full browser gate has 4836 passed, no failures/errors, five platform skips and 195 passing subtests; WF-06 has 128 selectors / 261 cases passing. Strict exit remains 1 for Linux RELEASE-01 and all seven external checks remain not_run. Detailed evidence, the interrupted earlier run, and unresolved historical intermittent failures are preserved in [implementation progress](../../platform/workflow-platform/implementation-progress.md).

The [persistent identity plan](2026-10-25-fleet-integration-identities.md) now supplies immutable SQLite registration, indexed Bearer digest authentication, cross-process revocation and authorization inside the original Workflow transaction. Restored credentials are disabled, startup injection cannot reactivate them, runtime schema checks reject newer readers, and history validation uses a read transaction. The final 13-module set has 381 passed on Python 3.10.21 and 3.13.7 without skips. The final full browser gate has 4906 passed, zero failures/errors, five platform skips and 195 passing subtests; WF-06 has 154 selectors / 331 cases passing. Strict exit remains 1 for Linux RELEASE-01; all seven external checks remain not_run. Connector catalog persistence, integration configuration UI, external input/output wiring and live pilots remain open.

The [persistent MCP catalog plan](2026-10-25-mcp-catalog-persistence.md) closes the process-local default authority gap: immutable registration, durable approved/pending discovery, monotonic revisions, cross-process CAS/revocation, isolated snapshots, independent platform reader 4 and disabled restored connectors. Runtime adapters remain explicit; startup performs no network or secret resolution. The final 13-module related set passes 428 cases on both Python versions; the full gate has 4971 passed、0 failures/errors、5 skipped、195 subtests passed, WF-06 188 selectors / 396 cases passed, and strict exit 1 for Linux RELEASE-01. The previous paragraph's open catalog-persistence item is historical; configuration UI, external Agent input/output wiring and real pilots remain open.

The [MCP catalog administration plan](2026-10-25-mcp-catalog-administration.md) now delivers cookie-admin review of secret-free durable records, complete text-only discovery inspection, and exact epoch/revision admission/revocation. This is the MCP catalog UI subset; registration/transport/network discovery stay in trusted configuration. Shared HTTP cancellation now spans JSON body reading, and the explicit release inventory includes both new modules. The final dual-Python eight-module sets each pass 381 cases without skips; full gate 5045 passed、0 failures/errors、5 skipped、195 subtests passed, WF-06 217 selectors / 470 cases, strict exit 1 for Linux RELEASE-01. Fleet identity configuration UI, external inputs/outputs and real pilots remain open.

The [Fleet identity administration plan](2026-10-25-fleet-identity-administration.md) delivers the secret-free inspection/revocation subset of configuration UI, preserving immutable trusted registration and original digest/Workflow authority. Active-state checks run inside the original revoke transaction; restored or concurrently revoked records conflict. Seven Chrome scenarios and durable HTTP evidence add 26 selectors / 63 cases. Serial dual-Python eleven-module sets each have 323 passed, no skips; full gate 5108 passed、0 failures/errors、5 skipped、195 subtests passed, WF-06 243/533, strict exit 1 for Linux RELEASE-01, external checks not_run. Other configuration, external file/context/output wiring and real pilots remain open.

The [text artifact input plan](2026-10-25-external-agent-text-inputs.md) delivers complete bounded UTF-8 artifact data, immutable private requests committed with the original send intent, exact journal prompt recovery, current metadata authorization before permitted sends, and external schema/reader 2 with Workflow reader 5. Legacy no-input prompts and schema-1 gate-off reads remain exact. Node receive/alias checks continue for Node steps and do not block external Hub text. The final dual-Python twenty-module sets each pass 688 cases with no skips; full gate 5172 passed、0 failures/errors、5 skipped、195 subtests passed, WF-06 271/597, strict exit 1 for Linux RELEASE-01, all seven external checks not_run. Binary/code bundles, Memory/summary, capability requirements, structured output export and real pilots remain open.

## Explicit Memory follow-up

The [Memory input plan](2026-10-25-external-agent-memory-inputs.md) closes only external explicit owner Memory delivery: complete bounded revisions, shared artifact budget, immutable original request recovery, current batch authorization, schema3/reader6 compatibility and complete historical backups. Native/summary, binary/code bundles, capability requirements, structured external export, other configuration and live/platform pilots remain open. Final serial dual-Python 23-module sets each have 784 passed/no skips. 全仓 **5256 passed、0 failures/errors、5 skipped、195 subtests passed**，WF-06 **302 selectors / 681 cases 全通过**，**41/42 journeys**；严格退出 **1**、ci_status=failed，唯一 incomplete 为 Linux RELEASE-01，七项外部检查均 not_run。证据目录：`/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-external-memory-full-20261025-dnxqbpck`。


## Published ArtifactRef output follow-up (2026-10-27)

The approved [ArtifactRef output plan](2026-10-26-external-agent-artifact-outputs.md) implements the original external task's read-only export of existing Hub artifact/structured/code_revision references. Complete byte/schema/source verification precedes atomic original Workflow output/subject/state binding with reader8 fencing and private recovery evidence. Muse export remains unsupported; no remote importer, new write submission or fabricated validation/acceptance. The earlier open-output statements describe their historical snapshots. All47 new selectors/178 cases pass; serial Python3.10/3.13 related sets each have1153 passed/0skip. Full frozen evidence has5524 passed, zero failures/errors, five platform skips, WF-06 374/937 and WF-08 179/380 passing. The866 source digests remain unchanged; strict exit1 persists for Linux RELEASE-01 and all seven external checks remain not_run. Native complete Memory is also implemented; Native full text consumption, summary/binary/code inputs, other configuration and real/platform trials remain open. Exact evidence is recorded in [implementation progress](../../platform/workflow-platform/implementation-progress.md#2026-10-27-外部-agent-已发布-artifactref-输出).

## Native complete text input follow-up (2026-10-27)

The [Native text plan](2026-10-27-native-workflow-text-inputs.md) now delivers complete local artifact text through the original Run snapshot/worker, with a shared text/Memory budget, reader9 fencing, current authorization after metadata I/O, exact original lookup and bounded history/backup closure. The initial configuration-bound error is now a deterministic pre-execution rejection with its original cause. Native text contributes 39 selectors / 115 cases; serial Python 3.10/3.13 sets each pass 1532 tests and 2 subtests, without failures or skips.

全仓 **5639 passed、0 failures/errors、5 个平台 skip、195 passing subtests**；JUnit 实际 5644 个 testcase，不重复累计 subtests。WF-06 **413 selectors / 1052 cases**、WF-08 **201 / 451** 全通过，41/42 journeys。严格退出 1、ci_status=failed，仅 RELEASE-01 缺少三项 Linux pidfd/proc 必需实例；另两项 Supervisor proc 平台 skip，七项外部检查均 not_run。

所有 gate 默认关闭；summary、二进制/代码包输入、CLI/评审/测试/远程或 capability context、其余集成配置、Linux/Windows 与获准真实两机/模型/MCP/Muse试点继续开放。Muse export 仍 unsupported，整体平台与生产验收未完成。 Exact evidence is recorded in [implementation progress](../../platform/workflow-platform/implementation-progress.md#2026-10-27-native-完整文本产物输入).
