# Main assistant project administration implementation plan

> Execute inline in the existing isolated feature worktree using executing-plans; deployment is authorized by the user.

**Goal:** Give the administrator's main assistant the project's actual management capabilities and a truthful inventory of unavailable integrations.

**Architecture:** Fixed Hub-only administration tools compose existing application services. Read and write operations have separate allowlists and discoverable parameter contracts. Existing scope, revisions, backups, signed receipts and account liveness remain authoritative. Cloud-provider lifecycle operations require a configured provider and are never inferred from a platform Node record.

**Tech Stack:** Python, existing Flask composition, SQLite and jsonschema; no new dependencies.

## Tasks

- [x] Add tests for actual settings writes, Node creation/removal and reference guards, admin demotion, disabled services, remote routing and restricted Run isolation. Run against missing implementation first.
- [x] Add a fixed operation catalog and application adapter for settings, node enrollment/removal, plugin operations, memory, schedules, workflows, adoption, MCP and usage diagnostics. Reuse existing services and DTO validation; never dispatch arbitrary methods or HTTP endpoints. Expose operation-specific schemas through discovery.
- [x] Wire administration only to normal main-assistant Runs, always executed by Hub; recheck active administrator on every operation. Keep MCP availability unchanged.
- [x] Expand the system prompt with project ownership, capability discovery, practical workflows, actual permissions and receipt interpretation. Add readable activity without credential previews and update linked platform documentation.
- [ ] Run affected tests and lint, review diff, submit a feature commit, rebase on latest main, push, follow CI, merge and follow production deployment. Verify release identity and live read/write behavior on dedicated acceptance resources.

## Validation

```sh
PYTHONPATH=. python3 -m pytest tests/test_assistant_project_admin.py tests/test_assistant_vm_sessions.py tests/test_main_assistant_prompt.py tests/test_platform_run_worker.py tests/test_assistant_tool_routing.py tests/test_assistant_tool_availability.py -q
ruff check --select E9,F63,F7,F82,F401 hub tools connectors fleet_core tests/test_assistant_project_admin.py
```

Node removal is an explicit catalog deletion of a disabled, unreferenced Node, not cloud VM destruction. Credentials are removed transactionally with the Node. Historical commands and conversations are preserved; active references and capability policy bindings block deletion. Config writes retain optimistic versions and RuntimeSettings backups. New-node enrollment rejects duplicate IDs rather than rotating an existing credential on replay.

## Local verification before submission

- Initial five administration tests failed on the absent service; implemented the adapter and expanded to 16 cases covering real stores, remote-worker routing, account demotion, Node credential behavior, Workflow idempotency and guarded removal.
- The affected integration suite passed **612 tests and 3 subtests**, including original VM-session, adoption, settings, account, connector, Workflow, Memory and schedule behavior. Ruff with CI rules and whitespace checks passed.
- A follow-up shared-settings gate check and affected settings/prompt suite passed **64 tests** after exposing existing append-turn, local-profile and schedule toggles. Production release and real remote receipt acceptance require the subsequent GitHub main workflow and dedicated live evidence; local tests do not claim those results. Release identity and live evidence are maintained in the canonical deployment runbook after publication.

## Publication follow-up: workspace/Node reference race

A deterministic interleaving test failed before the fix: after settings validation, a second administrator disabled/removed the Node, and the original workspace save still inserted its dangling Node reference. Move reference validation into `upsert_workspace` under the same `BEGIN IMMEDIATE` transaction as version validation and persistence; remove the obsolete application precheck. All repository workspace writes now follow this invariant. Update one setup fixture to create its referenced Node, preserving every existing labels/history assertion. Fresh affected verification: **232 tests passed**. The initial main publication was stopped before deployment so the production release can include this fix.
