# Official plugins and GitHub CLI implementation plan

> **For agentic workers:** Execute this plan inline, task by task, using the existing isolated worktree. No delegation or new chat is needed.

**Goal:** Deliver a usable official plugin registry and a GitHub CLI authentication management plugin across Hub UI and execution nodes.

**Architecture:** Keep pure plugin manifests/contracts in `fleet_core`, persistence/application/HTTP in `hub`, and host execution in `tools/plugins`. Reuse signed platform commands and NodeJournal. Bundle official plugins with the release; plugin enablement and node offers are distinct. A dedicated plugin-node entrypoint runs under the intended OS user and announces configured contexts.

**Tech stack:** Existing Python 3.10+, Flask, SQLite WAL, browser ES modules and installed GitHub CLI. No additional runtime dependency.

## Constraints and decisions

- Base: main `0160d0a`; branch `codex/official-plugins`, protected primary checkout untouched.
- UTF-8 without BOM. Do not install programs, change real GitHub accounts, or deploy as part of local feature acceptance.
- Registry protocol v1; built-in plugin `github-cli`, version `1.0.0`. Plugin metadata, action contracts and node handlers are registered rather than added to core dispatch branches per action.
- Plugin and node target enablement default off. Node offers carry actual installed version, contexts and freshness, separately from administrator configuration.
- Contexts represent this node process's OS user/environment and optional `GH_CONFIG_DIR`; no implicit sudo or credential copying. Environment-token overrides are displayed and block stored-account mutations that would be ineffective.
- Device login is a persistent local job. Start/status/cancel commands remain short; a bounded worker owns `gh auth login`, terminates it on parent EOF, and saves only parsed progress. Runtime restart never replays an uncertain login.
- `gh auth status --json hosts` is interpreted from account states, not exit code alone. CLI missing, unsupported output, network/auth failure, account state and repo permissions remain distinct.
- No raw token, raw CLI output, arbitrary executable/argv or config-file contents are returned to Hub.

## Task 1: Registry, operations and Hub API

Files: `fleet_core/plugins.py`, `hub/infrastructure/plugin_repository.py`, `hub/application/plugin_service.py`, `hub/http/plugin_routes.py`; composition wiring; `tests/test_plugins.py`.

Interfaces:

```python
registry.get(plugin_id)  # PluginDefinition; unknown IDs fail
definition.validate(action, arguments)  # normalized action arguments
service.catalog(owner_id)  # manifests + persisted enablement
service.targets(owner_id, plugin_id)  # owner nodes + offers + context observations
service.submit(owner_id, plugin_id, node_id, action, arguments, idempotency_key)
service.operation(owner_id, plugin_id, command_id)
```

- [x] Write tests for persistent enablement, owner scoping, duplicate/conflicting submissions, node readiness and exact action arguments; run and observe missing-module failure.
- [x] Add typed registry with action metadata and GitHub action validation. Add settings/offers/observations tables in the existing platform database. Keep operations in `platform_commands` with scoped list/lookup.
- [x] Add operator catalog/settings/targets/operations HTTP routes and node announce endpoint under the existing machine-auth blueprint. Wire through platform composition. Disabled plugins remain inspectable and permit login-status/cancel of existing work.
- [x] Run `python3 -m pytest tests/test_plugins.py tests/test_platform_node_routes.py tests/test_boundaries.py -q`.

## Task 2: Node plugin runtime and GitHub CLI

Files: `tools/plugins/{runtime,github_cli,login_store,login_worker,node}.py`, `tools/platform/node_{runtime,executor}.py`, `tests/test_github_cli_plugin.py`, `tests/test_plugin_node.py`.

Interfaces:

```python
executor.execute(command)  # bounded standard Node result envelope
executor.manifest()  # plugin versions and contexts
runtime.manifest()  # existing node fields + optional plugins
load_runtime(config_path)  # runtime + poll interval
```

- [x] Write tests using temporary fake `gh` executables with actual subprocess/SQLite behavior: JSON auth failures with exit 0, multiple accounts, token overrides, switching, logout, repo permissions, missing CLI, login/cancel/restart, context isolation.
- [x] Implement short commands and persistent device-login worker, bounded subprocess output/time, node registration and executor routing. Reuse NodeJournal, command signatures and existing node credentials. A standalone node supports `--config`, `--manifest`, `--once` and resident mode with an ownership lock.
- [x] Run signed Hub→Node→receipt integration tests, including duplicate delivery and restart recovery. Extend import-boundary coverage for the new node entrypoint.

## Task 3: Plugin center and real browser acceptance

Files: `frontend/assets/{api/plugins.js,plugins/registry.js,plugins/github-cli.js,views/plugins.js,styles/plugins.css}`, app/navigation/page wiring; browser fixture and `tests/test_plugins_browser.py`.

- [x] Build catalog with plugin/target enablement and generic registered contribution mount/dispose. GitHub contribution shows target/context/host, account overview, actions, device code, operation history and cancellation.
- [x] Persist operation recovery on Hub, poll active work after reload, avoid stale-response updates and overlapping polls; stop polling on route suspend/unmount and resume when visible. Disabled plugins preserve inspection/cancel UI.
- [x] Test in a local real browser against the actual Flask app and signed temporary fake-gh node: enable, inspect, login progress, reload, finish, switch, repo access, failure and cancel; record no browser exceptions.

## Task 4: Verification and developer documentation

Files: `docs/platform/official-plugins.md`, docs/testing journey additions, existing project README/document links, canonical vault project entry.

- [x] Document registry extension recipe, command/API contract, example node config and Linux/macOS/Windows service setup, identity/environment behavior and unsupported CLI recovery.
- [x] Run focused tests, full Python suite, JS syntax/lint and production package/release-evidence checks appropriate to the change. Separate existing platform-specific skips from failures.
- [x] Review the diff for functional races and compatibility; fix and rerun affected checks. Record the exact commands/results and remaining live-account acceptance boundary.
- [x] Update the canonical vault project entry and router/MOC links with the implementation location and unreleased state. Leave a reviewable feature branch and show the resulting files.

## Verification and acceptance boundary

The implementation reuses the existing command plane and was exercised with real SQLite, Ed25519 commands, the node journal, owned subprocesses and Chrome. Focused Python 3.10 node/plugin coverage passed (88 tests before the added protocol-preservation case); the final plugin/browser suite passed 37 tests. Frontend/browser/release checks passed 157 tests and 2 subtests after fixing the added navigation entry's effect on sidebar resizing. Ruff, ES module syntax, shell syntax, UTF-8/BOM checks and the static frontend smoke passed.

Failing evidence was retained in regression cases for malformed offers, authentication JSON network errors, malformed repository JSON, old-worker context ownership, login history eviction, selection recovery and Git protocol preservation. Each was observed failing before its fix and freshly rerun. The sidebar regression also reproduced with geometry showing its node height clamped at 60px; bounded scrolling for the navigation leaves the adjustable lists usable.

Final full-suite evidence belongs to the feature commit's CI checks and the local JUnit/journey report; local macOS skips for Linux pidfd and /proc are not substitutes for Linux release evidence. Real GitHub device authorization, remote VMs and each OS credential store remain external acceptance work. The local real-gh check was read-only. This branch has not been deployed.
