# Fleet integration identity setup plan

> Execute inline with writing-plans, evidence-driven-bugfix and verification-before-completion in the existing feature worktree. No subagents, production mutation or binary download.

**Goal:** Let an operator register an immutable Fleet MCP identity into the existing authority without writing a custom Python entry point.

**Architecture:** A bounded trusted JSON manifest contains existing identity snapshots with token SHA-256 only. A default-preview CLI validates the complete file and selects one exact owner/integration key. Explicit apply registers through the original IntegrationIdentityRepository in an existing initialized Workflow database; the normal Hub already reads that store. No new startup injection or web registration authority is necessary.

**Constraints:** UTF-8 without BOM, Python 3.10, no new dependencies, at most 128 entries and 256 KiB, unique owner/integration keys and token digests, original scope/budget/immutable/revocation/restore contracts. Raw tokens and digests do not appear in CLI output. No token generation or external request. Preview does not open/create a database; apply requires existing enabled owner workspaces and does not create them. Template availability remains the original runtime catalog's responsibility.

## Task 1: Failing evidence and original-store integration

Files: new `tests/test_integration_setup.py`, `hub/composition/integration_registration.py`, `hub/integration_setup.py`.

- [x] Add subprocess preview test proving no database creation, and normal Hub gate-enabled HTTP discovery after CLI registration with no injected identities.
- [x] Verify repeated apply preserves the exact record, then revoke through the original store and prove apply/restart cannot reactivate it.
- [x] Run `PYTHONPATH=. python3 -m pytest tests/test_integration_setup.py -q`, retaining original failure output.
- [x] Implement strict bounded file decoding using original canonical identity parser. Add CLI `--registration FILE --owner OWNER --integration ID [--database EXISTING_DB --apply]`, default preview, exact selection, stable errors and redacted summaries.
- [x] Add invalid manifest, missing/disabled workspace, missing store, conflicting immutable registration, cross-identity token conflict and module-entry regressions; run identity/admin/Fleet MCP/backup-related tests.

## Task 2: Acceptance and operator documentation

- [x] Add `docs/platform/workflow-platform/integration-setup.md`, linking index, F5 roadmap and acceptance ledger; explain manifest token hashing, original Hub gates, preview/apply, immutable scope changes and existing UI revoke.
- [x] Register new meaningful WF-06 selectors and verify all matrix selectors collect.
- [x] Freeze source and run new/related tests on Linux using existing runtime, verifying source and copied report hashes. Run lint, UTF-8/3.10 AST, local links and diff checks. Record actual scope and leave full Linux/browser, production and external TODOs open.

## Verified result — 2026-11-12

Evidence root: `/Users/mango/.cache/agent-fleet/verification/integration-setup-20261112-4Q3YnxfX/`.

- Original 2 regressions failed because `hub.integration_setup` did not exist (`red.xml`/`.log`). Initial implementation turned both green. Expanded new module has 7 selectors/27 instances, including subprocess entry, exact immutable state and normal Hub/real HTTPS authentication.
- macOS related 7 modules: `test_integration_setup`, `test_integration_identity`, `test_integration_identity_admin`, `test_fleet_mcp_boundary`, `test_mcp_http`, `test_mcp_startup`, `test_workflow_setup` → final **270 passed/0skip, 31.64s** (`final-related.xml`/`.log`). Prior 269-pass report remains separate from the subsequent HTTPS case.
- Immutable snapshot `08fc115abecac80f9378f2209d3d1a94a6fdcba9`: 1206 files, bundle 7,597,363 bytes. Linux existing Python 3.11.2 same 7 modules → **270 passed/0skip, 96.49s**. Source hashes and clean checkout verified before/after and again after independent probes. Remote root `/tmp/agent-fleet-native-20261111/integration-setup-08fc115a/`; original reports copied to `linux-results/`.
- Independent Linux configured-template probe uses actual workflow_setup CLI, normal Hub, identity CLI and local HTTPS peer. The first probe mistakenly provisioned before Hub initialized Workflow: both cases rejected per original contract, retained as `configured-probe.xml`/`.log` plus original script. Corrected setup order in `probe_configured_fleet_ready.py` → **2 passed, 3.15s**. A 200000/50 identity receives budget_forbidden with no Workflow; 600000/150 creates one workflow, repeated submit retrieves it, get and artifact list succeed. Database integrity/FK checks pass; counts 0/1 Workflow and zero dispatches in both prove no task execution was claimed. Snapshot copies of both original probe databases and all reports match remote SHA-256. This revealed and corrected the new documentation example's single-step budget for the three-step template; runtime budget policy was not changed.
- Config/journey/release evidence/release layout 4 modules → **183 passed, 2 subtests passed**, 16.14s (`gates.xml`/`.log`).
- Original full matrix now 46 journeys/2455 selectors; **7348 instances collected**, missing selectors zero. All 7 new selectors/27 instances pass in the Linux related report. Collection-only and related runs do not establish full WF-06 or release acceptance.
- Ruff, diff check, UTF-8 without BOM, Python 3.10 AST and local document links pass. All product/test source matches the executed snapshot; later changes are documentation. Detailed audit `verified-summary.json`, remote source/report/database audit `remote-final-audit.json`.

Current Linux acceptance host still has no installed Chromium/Chrome or Playwright browser cache. Historical gz-cvm full-gate runtime belongs to the expired host; it is not a current reusable environment. No browser/dependency download, production change, development commit, push or merge was performed. Remaining UI, real external account/policy, actual production task/restore and full current Linux/browser gate remain open, together with all other project TODOs.
