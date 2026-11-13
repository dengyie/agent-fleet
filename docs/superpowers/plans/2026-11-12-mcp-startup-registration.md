# MCP startup registration implementation plan

> Execute inline in the existing protected-main feature worktree, using writing-plans, evidence-driven-bugfix and verification-before-completion. No delegation or production mutation.

**Goal:** Let the normal Hub process attach trusted MCP registrations without a custom Python entry point, and provide explicit operator discovery using the same durable catalog.

**Architecture:** A bounded JSON file supplies existing MCPConnectorConfig values and optional exact-endpoint private aliases. Composition attaches adapters with EnvironmentSecretBroker, without resolving credentials or contacting servers. A CLI previews the configuration by default; explicit single-connector discovery publishes through the existing catalog, preserving its initial admission, pending revision and revocation semantics.

**Constraints:** UTF-8 without BOM; Python 3.10 floor; at most 128 connectors; existing 20-tool selection, HTTPS, read_only and immutable registration contracts; no new dependencies; no auto discovery on startup; no changes to web administration authority or network behavior; disabled gate ignores configuration files. Existing explicit registration injection remains supported; combining a file with injected catalog/adapters is rejected to avoid ambiguous authority.

## Task 1: Replayable startup and CLI evidence

Files: `tests/test_mcp_startup.py`, `hub/config.py`, `hub/web.py`, `hub/composition/platform.py`, new `hub/composition/mcp_registration.py`, new `hub/mcp_setup.py`.

- [x] Add normal make_app tests using environment and fleet-gates.conf, including explicit empty override. Require attached IDs and durable registration, and zero transport/secret calls at startup.
- [x] Add CLI preview/no-store/no-network and explicit discovery/restart/call tests against an independent real HTTPS peer. Check pending refresh, failure errors and no tools/call during discovery.
- [x] Run `PYTHONPATH=. python3 -m pytest tests/test_mcp_startup.py -q`; retain the original failure output before implementation.

## Task 2: Trusted assembly and explicit discovery

- [x] Add `read_mcp_registration(path)` with a 256 KiB bounded regular-file read, strict JSON object/fields/version, duplicate ID rejection and original configuration/transport validation. Return fully validated adapters with lazy environment secret broker.
- [x] Add `platform_mcp_registration_file` to FleetConfig/from_root and make_app using original explicit/env/file precedence. In enabled composition load all adapters, validate enabled workspace bindings, then register into the original durable catalog. Reject combined file/injected authority. Gate-off startup does not open the file.
- [x] Add `python -m hub.mcp_setup --registration ABSOLUTE_PATH [--database ABSOLUTE_PATH --connector ID --discover]`. Preview does not open the database or contact endpoints; discovery requires an existing database and exact configured connector, validates workspace, registers that connector and uses ConnectorCatalog.discover. Return bounded codes and connector IDs/revision/state only; changed definitions report pending without auto approval.
- [x] Re-run original failing tests and existing connector/persistence/admin/HTTP/startup regressions. Inspect real HTTPS method order, persisted bytes and restart pins.

## Task 3: Deployment documentation and required journeys

- [x] Document schema, environment/gates setting, environment SecretRef provisioning, preview/discover commands, first-discovery activation, later pending approval and restart/revocation semantics in `docs/platform/workflow-platform/mcp-startup.md`; link from platform index/roadmap and acceptance ledger.
- [x] Register meaningful new selectors under WF-06 and verify complete matrix collection. Run relevant gate regressions, lint and diff checks. Record exact commands/counts and remaining full Linux/browser/production/external TODOs; no broad completion claim.

## Verified result — 2026-11-12

Evidence root: `/Users/mango/.cache/agent-fleet/verification/mcp-startup-20261112-4HGX3r5h/`.

- Original startup/CLI regressions: `PYTHONPATH=. python3 -m pytest tests/test_mcp_startup.py -q` → 5 failed, 1 passed, `red.xml`/`red.log` retained. The failures confirm missing normal startup file assembly, explicit argument and CLI module.
- Original six cases after implementation: 6 passed; extended new module contains 35 instances across 8 required selectors.
- macOS Python: new startup plus connector/persistence/admin/pins/HTTP/workflow startup/release layout, 8 modules → 325 passed, 2 subtests passed, no skips, 30.49s. `related.xml`/`related.log`.
- Immutable snapshot `568a56421e758c00898ac0aee7768ea72ee05024`, 1201 files, 7,585,932-byte source bundle. Linux Python 3.11.2, existing environment, original 8 modules → 325 passed, 2 subtests passed, no skips, 95.60s. Remote directory `/tmp/agent-fleet-native-20261111/mcp-startup-568a5642/`; local copied originals in `linux-results/`. Source hashes before/after match, checkout clean. All four copied reports match remote SHA-256.
- Independent real HTTPS peer observed exactly server/discover → tools/list → tools/call → server/discover → tools/list; default preview and Hub startup performed no network or secret resolution. Restart used persisted pins; changed descriptions remained pending; revoked rediscovery returned connector_revoked without further network, restart granted no tools. Environment token bytes absent from the persisted database.
- Config/journey gate/release evidence/Fleet boundary/runtime dependency regressions: 207 passed, `gates.xml`/`gates.log`.
- Full matrix: 46 journeys, 2448 selectors, 7321 collected instances, zero missing selectors. `collection-tests.json`/`.log`. Collection-only status is incomplete by design; no journey pass inferred. Original mistaken root-wide collection failed by importing a compatibility module as a test, retained separately in `collection.json`/`.log`; correct `pytest tests --collect-only` exits 0. Linux related report proves all 8 new selectors/35 instances passed, not full WF-06.
- Ruff, diff check, UTF-8 without BOM, Python 3.10 AST and new local documentation links verified. Runtime code/tests match the executed snapshot; subsequent ledger/plan documentation only differs. Independent audit: `verified-summary.json`.

This completes trusted deployment assembly and discovery, not all F5 configuration UI or real external service/account policy acceptance. Full current Linux/browser gate, production receiver/configuration, actual production task/download/restart acceptance, other input/adapter/browser/notification TODOs remain open. No development commit, push, merge, production update or large binary download was performed.
