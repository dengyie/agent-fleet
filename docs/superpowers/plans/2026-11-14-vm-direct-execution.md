# VM Direct Execution Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task in this session.

**Goal:** Run ordinary assistant workspace commands on an existing execution VM without requiring a sandbox launcher, and configure enrollment from Settings.

**Architecture:** Keep Hub orchestration and the signed Node command/receipt journal. Add an explicit `vm_direct` Node backend alongside the existing `sandbox` backend. The execution VM owns its workspace files and processes; Hub local execution keeps its existing sandbox policy. Settings generates a Node enrollment file from the selected node and its bound workspaces; installing that file and restarting the Node applies the configuration.

**Tech Stack:** Python 3.10+, existing Flask/SQLite services, vanilla JavaScript settings forms, existing bounded process and Ed25519 Node transport.

## Global Constraints

- UTF-8 without BOM; no new dependencies or cloud instance purchases.
- Preserve the protected primary worktree and historical production Runs.
- Reuse command IDs, durable receipts, cancellation and process group cleanup; uncertain execution is never replayed.
- Explicit direct mode admits arbitrary argv, including shell/interpreter commands; the VM account is its execution boundary.
- Publish through the existing PR/main CI artifact and receiver pipeline.

### Task 1: Explicit VM backend and Node configuration

**Files:** Create `fleet_core/backends/vm.py`, `tools/platform/workspace_registration.py`, `tests/test_vm_execution.py`; modify `tools/platform/node_runtime.py` and `tools/plugins/node.py`.

**Interfaces:** `VMBackend(root).execute(command_id, argv, timeout_s=30, should_cancel=None) -> ToolReceipt`; `workspace_settings(value: object) -> dict` translates `{execution_mode, roots, sandbox_launcher}` into NodeRuntimeConfig arguments. `NodeRuntimeConfig.execution_mode` defaults to `sandbox` and accepts `vm_direct` explicitly.

- [x] Write and run failing tests for direct shell/interpreter execution, nonzero output, malformed argv/timeouts, output bounds, cancellation, signed Node execution and duplicate receipts.
- [x] Implement VM execution using `run_bounded(argv, cwd=self.root, timeout_s=min(timeout_s, 30), stdout_limit=MAX_OUTPUT_BYTES + 1, env=..., should_cancel=should_cancel)`; retain merged stdout/stderr and the actual returncode. Use workspace HOME/TMPDIR and inherited PATH, without Hub secrets.
- [x] Select `VMBackend` only for explicit `vm_direct`; omit exec from sandbox manifests/actions when no launcher exists. Extend the existing plugin Node CLI to accept workspace-only enrollment and configuration.
- [x] Run `python -m pytest tests/test_vm_execution.py tests/test_platform_node_runtime.py tests/test_platform_node_workspaces.py tests/test_plugin_node.py -q` and commit the deliverable.

### Task 2: Centralized enrollment and truthful tool behavior

**Files:** Create `hub/application/workspace_node_setup.py`; modify `hub/http/settings_routes.py`, `frontend/assets/api/settings.js`, `frontend/assets/views/settings/catalogs.js`, `hub/platform/tool_broker.py`, `hub/platform/runtime/native.py`, `hub/application/run_worker_service.py`, `hub/application/platform_readiness_service.py`; add regression tests in `tests/test_vm_execution.py`, `tests/test_settings.py`, `tests/test_settings_browser.py` and existing recovery/availability tests.

**Interfaces:** `WorkspaceNodeSetupService.enroll(owner, node_id, body) -> dict` validates signing/remote gates, Hub origin, mode and bound enabled workspace roots before rotating the existing node credential. `POST /settings/nodes/<id>/enrollment` returns `{ok, enrollment}` containing the existing enrollment contract plus workspace settings.

- [x] Write failing API/UI tests for enrollment, owner scope, invalid mode/root/missing signing, no credential rotation on rejected requests, and downloaded file consumed by the original Node CLI.
- [x] Add Settings controls for VM direct/sandbox mode and optional launcher, with a one-time enrollment download and clear restart instructions. Keep mode/config generation and validation in Python.
- [x] Require explicit remote `workspace.exec` capability for model declarations. Explain argv/cwd/timeout/output limits in the tool description.
- [x] Return confirmed `workspace.exec/process_failed` output and returncode to the model so it can correct the next command. Preserve failure cards, cancellation, limits and unknown semantics; never automatically rerun the failed command. Verify receipt-checkpoint recovery executes once.
- [x] Make configuration readiness validate the selected remote node/gate rather than requiring a Hub-local directory; retain explicit `not_tested` connectivity reporting.
- [x] Run settings/browser, remote HTTP, worker, checkpoint and process regressions; register all new meaningful selectors in `docs/testing/journeys.json`.

### Task 3: Publish and deploy an execution Node

**Files:** Update existing setup/deployment documentation and testing index; retain private deployment evidence outside Git.

- [x] Verify the existing CUCloud VM (`mesh-cu-huhe`) is reachable, has adequate memory/disk, and preserve its existing services. Use a dedicated unprivileged `fleet-node` account and separate state/workspace directories.
- [ ] Run lint, source/package checks and relevant browser regressions; self-review call chains and diffs. Commit, apply the exact candidate patch to a feature branch based on current public main, and create a PR.
- [ ] Verify original full PR CI evidence at the exact revision, rebase onto latest main, merge and verify full main CI/deployment artifacts and production revision.
- [ ] Install the exact published release on the execution VM, enroll through the settings API, run the existing Node CLI as a systemd service, and bind a new VM workspace as the account default without moving/deleting existing local workspace data.
- [ ] Execute a bounded real assistant command on the VM; independently verify command output, receipt, file location, incremental feedback and refresh restoration. Verify cancellation and no active leftovers.
- [ ] Record the actual deployed revisions, paths, configuration application/restart steps and rollback in the existing canonical ops note/indexes on vault master. Report verified results and remaining limitations in Chinese.

## Verification so far

- Initial direct-mode tests failed 7/7 before implementation. Additional setup/validation regressions exposed missing enrollment and invalid-input exceptions.
- Local relevant suite: 261 passed, including desktop/mobile download, signed HTTP command failure/correction, crash receipt recovery and process cleanup.
- Full Linux/Windows CI and production VM acceptance remain required before the deployment can be called complete.
