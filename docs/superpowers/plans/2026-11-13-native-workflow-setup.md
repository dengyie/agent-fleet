# Native Workflow Setup Implementation Plan

> **For agentic workers:** Use `executing-plans` inline in the existing isolated feature checkout. Preserve the reviewed publication candidate; no merge, deployment or external model call is part of this slice.

**Goal:** Prepare Native Workflow workspace and conversation bindings through the existing preview/apply CLI so a fresh ordinary Hub can load the manifest.

**Architecture:** Extract the existing pure Native row parser for reuse by startup and setup. Keep workspace/conversation creation in the original platform transaction and retain the three-conversation coding wrapper. Add exact template selection for Native entries; a mixed manifest without a selector retains the legacy coding selection.

**Tech Stack:** Python 3.10+, existing Flask/SQLite, pytest; no dependencies.

## Global Constraints

- UTF-8 without BOM; existing isolated feature branch only.
- Preview never initializes schema or creates a missing database directory.
- Existing workspace roots, labels, overrides, archived records and history retain original conflict/preservation rules.
- Native definitions retain the existing 1–8 linear-step contract. Repeated conversation references are created once; templates and executors are not created by setup.
- Invalid selection or Native shape fails before any database creation. Models and provider secrets are configured separately; setup does not call providers or enable dispatch.
- Coding callers retain the exact three-distinct-conversation contract and report format.

## Task 1: Prove the missing initialization path

Files: create `tests/test_workflow_native_setup.py`.

- [x] Exercise actual CLI subprocess preview/apply on a Native-only manifest, inspect missing-store behavior and idempotent counts, then load the same manifest through ordinary `hub.web.make_app` after separately registering its model.
- [x] Cover shared conversations, explicit template selection in mixed/multiple manifests, unsupported definitions/bindings, conflict rollback and simultaneous apply.
- [x] Run `PYTHONPATH=.:tests /Users/mango/project/codex/agent-fleet/.venv/bin/python -m pytest tests/test_workflow_native_setup.py -q`; save the failing JUnit outside the repository.

## Task 2: Implement shared parsing and original-store preparation

Files: modify `hub/composition/native_registration.py`, `hub/infrastructure/workflow_setup.py`, `hub/workflow_setup.py`.

- [x] Extract `parse_native_registration(row)` returning `(definition, by_step)` from the current startup parser. Keep original startup checks and errors.
- [x] Add a bounded generic conversation-binding preparation entry point sharing `_inspect`, the original transaction and create-only behavior. Compute report counts from actual unique bindings. Preserve the legacy wrapper's three-distinct checks.
- [x] Add `--template` selection using the strict existing manifest reader and parser; without it prefer coding entries when present, otherwise select the exact Native owner/workspace entry. Ambiguous or missing selection exits 2.
- [x] Rerun the original failing command and existing setup/startup/native tests. Inspect JUnit and source diff before updating status.

## Task 3: Register coverage and operator instructions

Files: modify `docs/testing/journeys.json`, `docs/platform/workflow-platform/startup-registration.md`, `docs/platform/development-completion.md`, `docs/platform/workflow-platform/README.md`.

- [x] Add actual new selectors under existing WF-06; retain all previous selectors and external states.
- [x] Document Native CLI preview/apply, template disambiguation, shared conversation counts, model prerequisites and no dispatch side effects. Link this plan from existing entry points.
- [x] Run setup/startup/native/provider/backup regressions, Ruff required rules, Python syntax, UTF-8/no-BOM, `git diff --check`, and selector collection. A new full same-source Linux/browser gate remains distinct from the older reviewed candidate.

## Verified results — 2026-11-13

- Initial CLI regressions: 6 failed / 11 passed. Additional boundary regressions: 3 failed / 19 passed (oversized target, invalid model profile, and a Native template named coding-delivery). Both failing reports are retained.
- Final setup/startup collection: 81 passed / zero failures, errors or skips. Expanded 14-module setup/startup/Native input/Memory/recovery/plain and encrypted backup/boundary suite: 390 passed / zero failures, errors or skips.
- Journey and release-evidence gate regressions: 134 passed / zero failures, errors or skips. Full collection resolves all 2516 selectors across 46 journeys and 7504 cases; collection is not full-suite execution.
- CI-required Ruff rules passed using the installed Ruff executable. All nine changed/new files were verified as UTF-8 without BOM; Python sources parse with the Python 3.10 grammar. `git diff --check` passed.
- Raw evidence lives under the private verification root `native-workflow-setup-20261113/` (red/boundary-red/final-setup/related-final/gate-final XML and logs, plus collection-final.log). The first green command referenced a nonexistent module and exited 4 with no tests run; only subsequent completed runs count as passing.
- This development increment is separate from public PR42/db4c7ff. No merge, publication, application restart, model call or production database modification was performed for this slice. Full new-source Linux/browser/Windows and production acceptance remain open.

Return to [startup instructions](../../platform/workflow-platform/startup-registration.md#工作区和会话初始化), [Workflow entry](../../platform/workflow-platform/README.md), and [development ledger](../../platform/development-completion.md#native-初始化开发增量2026-11-13未进入公开候选).
