# MCP Connection Registration Implementation Plan

> **For agentic workers:** Use `executing-plans` inline in the existing isolated feature checkout, task by task. Preserve the exact PR43 candidate; publish this increment separately. No production merge or deployment is authorized by this plan.

**Goal:** Configure and discover a usable read-only MCP connection from the account page, with durable original catalog authority and normal Hub restart support.

**Architecture:** Share the trusted MCP configuration parser between the setup CLI and the registration command. Add an explicit opt-in for account registration and offline attachment of durable registrations, preserving the existing file-only attachment behavior when this opt-in is off. Use the existing catalog for discovery, original epoch/revision publication, approval, immutable registration, revoke and restore-disabled behavior.

**Tech Stack:** Python 3.10+, Flask, SQLite, static JavaScript, existing pytest/Playwright/Chrome. No new packages, database tables, secrets store or transport implementation.

## Global constraints

- Existing feature checkout only; UTF-8 without BOM. PR43's published HEAD remains unchanged.
- New gate: `platform_mcp_catalog_registration_enabled=False`, resolved by `AGENT_FLEET_PLATFORM_MCP_CATALOG_REGISTRATION_ENABLED`. It requires the platform and MCP connector gates. Gate-off startup does not attach durable records absent the original trusted file.
- Exact registration payload: `{"connector": <existing MCPConnectorConfig fields>}`. `max_tools` may be omitted as in the original file parser; `selected_tools` must be a JSON array. Credentials remain `env://[A-Z0-9_]{1,128}` references, never raw secret values.
- New connections require an enabled owner/workspace inside the same original catalog insert transaction. Exact repeats preserve the existing record, including revoked/restore-disabled records, and cannot change endpoint, policy or secret reference.
- Registration and startup perform no network request or secret resolution. The original trusted file's transport/private aliases remain attached when its connector is already present.
- Discovery is a separate explicit POST with `expected_epoch`; stale versions reject before network I/O, publication uses the original CAS after I/O, changed discovery remains pending, and existing pinned runs retain the old revision.
- Original cookie-admin/Origin/strict JSON/no-store boundaries apply. Original admit/revoke behavior and 2048-byte body limits remain intact; registration is bounded to 16384 bytes.
- Closing/navigating/changing account stops local observation; it does not prove server discovery was cancelled. No automatic POST retry or automatic discovery after registration.
- Disabled registrations are never reactivated by registration or startup. Backup restore's existing disabled facts continue to govern calls.

## Task 1: Durable offline registration and restart attachment

**Files:** create `hub/platform/connectors/registration.py` and `hub/application/mcp_registration.py`; modify `hub/composition/mcp_registration.py`, `hub/infrastructure/mcp_catalog_repository.py`, `hub/application/connector_catalog.py`, `hub/application/mcp_catalog_admin.py`, `hub/composition/platform.py`, `hub/composition/extensions.py`, `hub/config.py`, `hub/web.py`, `hub/accounts/http.py`, `hub/http/mcp_admin_routes.py`; create `tests/test_mcp_registration.py`.

**Interfaces:** `parse_mcp_registration(row) -> MCPConnectorConfig` is pure. `MCPRegistrationCommand.parse(payload)` freezes canonical configuration bytes. `register_mcp_connection(catalog, command)` uses the original store/adapter and returns the original `MCPCatalogRecord`. `attach_durable_mcp_connections(catalog)` performs bounded offline attachment. `MCPCatalogAdminService(catalog, *, registration_enabled=False).register(command)` returns the existing detail DTO.

- [x] Add failing tests using a real normal Hub, disposable platform/account databases, the original admin login, and the existing original MCP fake peer/HTTPS fixtures. The new route must return a usable durable registration without invoking transport or secret resolution.

```python
payload = {"connector": {**asdict(config(secret_ref="env://MCP_TEST_TOKEN")),
                         "selected_tools": ["weather"]}}
response = request(admin, "post", "/register", json=payload)
assert response.status_code == 200
assert response.get_json()["connector"]["summary"]["state"] == "registered"
assert catalog.attached_ids() == frozenset({"weather"})
```

- [x] Run the new module before product changes and retain its failing JUnit/log in the private verification root.

```bash
PYTHONPATH=.:tests /Users/mango/project/codex/agent-fleet/.venv/bin/python -m pytest tests/test_mcp_registration.py -q --junitxml=/Users/mango/.cache/agent-fleet/verification/mcp-registration-20261113/red.xml
```

- [x] Extract the original strict parser and use it from both file loading and the new frozen registration command. Add the opt-in, original transactional workspace guard, original-adapter attachment and the admin endpoint. Do not resolve secrets, discover tools or create a model Run.

```python
@dataclass(frozen=True, slots=True)
class MCPRegistrationCommand:
    config_snapshot: bytes

    @classmethod
    def parse(cls, payload):
        if type(payload) is not dict or payload.keys() != {"connector"}:
            raise MCPCatalogError("invalid_mcp_registration_command")
        return cls(encode_config(parse_mcp_registration(payload["connector"])))
```

- [x] Prove immutable exact retry after unavailable workspace, conflict preservation, disabled repeat, insert rollback, normal restart without a registration file, gate-off legacy attachment, private-alias preservation, maximum pages and no startup I/O. Run original startup, authority, administration and plain/encrypted backup regressions. Commit the backend and matching browser contract tests together in Task 3 so the independent increment remains usable from a clean checkout.

## Task 2: Explicit discovery and actual account-page configuration

**Files:** modify `hub/application/connector_catalog.py`, `hub/application/mcp_catalog_admin.py`, `hub/http/mcp_admin_routes.py`, `frontend/assets/api/integrations.js`, `frontend/assets/views/account/integrations.js`, `frontend/assets/views/account.js`, `frontend/assets/styles/integrations.css`; create `frontend/assets/views/account/mcp-registration.js`, `tests/fixtures/frontend/mcp_registration_browser.cjs`; extend `tests/test_mcp_registration.py` and `tests/test_mcp_catalog_admin_browser.py`.

**Interfaces:** `ConnectorCatalog.refresh(connector_id, *, expected_epoch=None)` preserves existing callers and fences an explicit expected epoch before I/O. `MCPCatalogAdminService.discover(connector_id, expected_epoch)` returns the current original detail, including pending state. `registerMCPConnector(payload, options)` and `discoverMCPConnector(connectorId, expectedEpoch, options)` share `platformRequest`. `openMCPRegistration(section, callbacks)` owns a native form dialog with exact owner/workspace/connector/HTTPS endpoint/SecretRef/tool names and original timeout/result/discovery limits.

- [x] Prove stale epoch yields zero requests, successful discovery yields usable original tools, schema change creates pending state, revoke during discovery prevents publication, failed/uncertain discovery leaves the original record, and repeated registration never rediscovers.

```python
before = len(transport.calls)
response = request(admin, "post", "/discover?connector_id=weather",
                   json={"expected_epoch": old_epoch})
assert response.status_code == 409
assert len(transport.calls) == before
```

- [x] Add the discovery route and reuse the original catalog's discovery/publication path. Initial discovery follows existing CLI semantics; changed definitions require the existing explicit approval.
- [x] Add a registration dialog and separate discovery action. Snapshot and lock the original submitted configuration for uncertain-result retry; show registered/active/pending/disabled and attached/runtime-ready facts accurately. Add no storage or new transport.
- [x] Run real desktop/390px mobile registration→explicit discovery→tool use→reload, lost registration response, uncertain discovery readback, modal-close/navigation/account-change cases. Save and inspect screenshots; prove HTTP counts and no automatic discovery/retry.

```bash
FLEET_PLAYWRIGHT_MODULE=/opt/homebrew/lib/node_modules/@playwright/mcp/node_modules/playwright PYTHONPATH=.:tests /Users/mango/project/codex/agent-fleet/.venv/bin/python -m pytest tests/test_mcp_registration.py tests/test_mcp_catalog_admin_browser.py -q
```

## Task 3: Register evidence and publish the exact increment

**Files:** update `docs/testing/journeys.json`, `docs/platform/workflow-platform/mcp-startup.md`, `docs/platform/workflow-platform/README.md`, `docs/platform/development-completion.md`, `docs/superpowers/plans/2026-10-20-workflow-platform-roadmap.md`, `.github/workflows/ci.yml` and this plan. Update the existing Obsidian canonical runbook and its project/router twins on vault master.

- [x] Register new meaningful selectors under WF-06 and document the opt-in, exact immutable retry, environment secret reference, explicit discovery, restart and disabled behavior. Keep external MCP pilots and the complete project scope open until actual evidence proves them.
- [x] Run required Ruff, Python 3.10/JavaScript syntax, UTF-8/no-BOM, diff, relevant authority/Run-pinning/backup/browser/package/gate tests and full selector collection. Extend the existing Windows selection to the actual new backend module without installing a new runtime.
- [x] Commit only verified changes. Export the full tree delta to a separate publication branch rooted at the reviewed PR43 HEAD; preserve the pre-push hook, verify complete tree equality, create/attach a draft PR and dispatch its exact CI. Inspect terminal Linux/browser/Windows evidence independently; do not substitute PR43 evidence for this increment.
- [x] Record actual publication/CI facts and keep formal production setup/deployment, real external service trials and remaining global TODOs distinct. The unrelated vault rebase has now completed; canonical runbook, project/development indexes and router twins were updated onmaster and the declared governance scan passed.

Return to [MCP startup](../../platform/workflow-platform/mcp-startup.md), [Workflow entry](../../platform/workflow-platform/README.md) and [development ledger](../../platform/development-completion.md).

## Implementation evidence

The private verification root is `mcp-registration-20261113/`. The original 33-case registration failure and two missing-browser-entry failures are retained. Later original-state, revoked-after-restart, mobile field visibility and durable-corruption failures are retained beside their fixes. Registration/parser failures are distinguished from damaged persisted snapshots; actual digest corruption returns store-unavailable503 rather than a caller400.

The final `browser-final.xml` records **80 cases, zero failure/error/skip**, including the original MCP and Fleet administration journeys, nine new actual browser instances, a real HTTPS peer with private aliases/environment credentials, cross-process startup/use, and plain/encrypted restore-disabled authority. HTTP counts prove no automatic discovery or replay. The latest desktop result and390px focused-tool-field PNGs were inspected.

The original backup/architecture/ownership/evidence/frontend package scope records **162 passed** in `backup-boundaries.xml`. The publication check initially found the new account module absent from the explicit release-layout allowlist (**503 passed, one failure**); the precise module was added without admitting arbitrary account files. The initial report is retained in `publication-checks/related.xml`; the fresh identical13-module command passed **504cases,2passing subtests,zero failure/error/skip** in `related-green.xml`. Required Ruff and the changed tests, Python3.10 grammar, JavaScript syntax, UTF-8/no-BOM and diff checks passed. Collection resolves46journeys and2558selectors against7601tests; collection is not a passing full gate.

The Windows workflow now selects the new actual backend module using the existing Node runtime. No new package/runtime or production configuration was introduced. Publication/full-CI facts must be added only after their actual terminal evidence. The Obsidian vault has an unrelated active rebase/detachedHEAD, so its master-only update remains deferred without interrupting that work. Other MCP/Muse configuration, actual external service trials, production setup/deployment and the complete project TODO remain open.

## Scope attachment performance correction

Final public HEAD `6ea7e09083299c755862131dfe8db2aebf978410` equals development `4393f27c723f000851d10dead11d144558743739` at complete tree `4c87f4495e9bcb01eeba91c694b0a2dc8b73281b`. Official CI43292358951 completed successfully: independently downloaded Linux evidence verifies7605cases,46journeys,2560selectors,77 valid PNGs,zero failure/error/skip and204passing subtests against the clean exact publication checkout. Windows334cases:332passed,2POSIXskips,zero failure/error,55MCP-registration cases. PR44 is ready for review. The initial run below was cancelled by concurrency when the corrected revision was dispatched; its Windows evidence is retained separately. Feature package/deploy jobs skipped and all five external checks remain `not_run`.

Production receiver checked the locally packaged7200581-byte candidate, SHA-256 `d4d940c5c6af3759577d21f923c5c941d3ea3fb274fc35ca38b1af254525e99b`, without changing LIVE/version/configuration. Main and public manifest remain301e259. Production readiness is not inferred: both stored nodes have stale heartbeats, the container lackspytest/bwrap and the actual namespace probe fails withOperation not permitted. Workflow registration/dispatch and actual production task/download/recovery acceptance remain open. Final evidence is recorded in the linked [ledger](../../platform/development-completion.md).

The initial increment was published as [PR44](https://github.com/dengyie/agent-fleet/pull/44), HEAD `5d9e2f88818e89058501faa76b685fc13cbb9a37`, exactly matching development `0ec239cee2fcb2489bcca5bb7fea6d80bea7bfa4` at tree `193df0f5b26d565599cab34250b8ccfa6600ec2a`. CI43288542639 was dispatched on that revision. Its completed Windows artifact independently records330cases:328passed,2POSIXskips,zero failure/error. The Linux job was still running at this checkpoint; this is not evidence for the correction below.

A real normal-Hub/SQLite benchmark with128active connectors showed single-tool catalog preparation P95 rising from2.015ms with the original attached-file behavior to80.298ms with durable registration enabled. This measures the authority/adapter preparation before the connector RPC, not end-to-end tool/model latency. The cause was repeated decoding of all128already-attached immutable definitions during new-registration attachment checks; the original call's current authority read still only needed its own connector.

Three deterministic cases at1/20/128connectors failed because a single original call decoded2/21/129records rather than one. The valid initial report is `publication-checks/scope-cost-red-valid.xml` (3failed,1passed); an earlier editing mistake placed an existing corruption assertion in the new peer fixture, producing a fourth NameError, and that original invalid-fixture report is retained separately. The corrected peer scenario proves that an already-running Hub still attaches a connection registered after its first tool call.

`get_scope` now accepts a bounded tuple of already-attached IDs and excludes those rows in the original SQLite query. `_durable` continues to read and validate the current selected connector's config, active revision and disabled state on every declaration/call; no authorization value is cached. Startup remains the original complete bounded offline scan. New registration/revocation behavior is verified using actual original calls, and no transport or persistent table was added.

On the same128-connector store after the correction, P95 was2.971ms for the original behavior and3.137ms with registration enabled (50samples each, under concurrent local regression load). The previous performance fixture incorrectly used an environment reference with the original fake Broker that only accepts `secret://mcp`; this fixture failure is preserved and excluded from timing evidence. Both actual timing reports and the completed larger associated/browser verification belong in `publication-checks/`. The current collection resolves46journeys/2560selectors/7605tests. The correction requires its own exact public revision and full CI; the initial PR44/Windows result must not be substituted.

The fresh15-module command including both actual administration browser modules passed **537cases plus2passing subtests,zero failure/error/skip**,154.37s (`scope-cost-associated-green.xml`). This includes the original3cost failures and the additional cross-process/new-registration case, actual original RPC results and fresh revocation checks. Full selector collection and required Ruff passed; publication remains separate from deployment.
