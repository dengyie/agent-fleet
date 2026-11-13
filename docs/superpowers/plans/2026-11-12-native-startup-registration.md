# Native startup registration implementation plan

> **For agentic workers:** Execute this plan task-by-task in the current isolated feature worktree. Follow the evidence-driven-bugfix gates; execution is already authorized by the ongoing “做完所有 todo” request.

**Goal:** Make implemented local Native text/code/image/audio Workflow inputs usable through normal Hub startup, without Python dependency injection.

**Architecture:** Extend the existing bounded operator-owned Workflow manifest with an optional `native` collection. A focused composition module builds original templates, plans and Run executors, resolving the complete model snapshot through DefaultsService and then pinning the manifest binding's provider snapshot. Keep the legacy coding-only reader return value and CLI selection unchanged. Credential references remain private and are preserved into original Run snapshots.

**Tech stack:** Python 3.10+, Flask, original SQLite repositories and Workflow v1 contracts.

## Global constraints

- Work only in `codex/functional-root-fixes`; preserve unrelated changes and production.
- UTF-8 without BOM. No new dependencies, provider calls at startup, background threads, new execution engine or database schema.
- Existing schema-version-1 coding manifests remain valid. The manifest has `schema_version` and one or both of `coding_delivery`, `native`; unknown fields fail. Each supplied collection has 0–32 entries and their combined count is 1–32.
- Native entry keys: `owner_id`, `workspace_id`, `definition`, `bindings`. Bindings exactly cover definition steps, with keys `step_key`, `target_key`, `conversation_id`, `model_profile_id`, `provider_snapshot`, `input_mode`. `provider_snapshot` is the complete provider/model/config/capability binding (plus an optional bounded `secret_ref` reference) used after restart; the model profile remains an enabled catalog permission check. Modes: `text`, `code`, `text_code`, `media`.
- Only existing `native_run` steps, no output specs/validators/capability requirements. Workspace/conversation/model must exist, be enabled/unarchived and share owner/workspace. Reject remote/default-node routing rather than silently changing it. Fixed adapter `native@1`, capacity 1; reserve each step's declared full model budget.
- Dispatch retains existing worker/scheduler requirements. Tasks is required only for coding entries. Existing explicit dependency collision and Muse merge checks remain in force.
- Normal provider snapshots include `secret_ref` (nullable, bounded string); accept both the old four-field shape and the current five-field shape without stripping credential binding.

## Task 1: Capture the actual failures

**Files:** Create `tests/test_workflow_native_startup.py`.

- [ ] Create normal-Hub fixtures with a real platform catalog, manifest and ArtifactStore; obtain templates and create Workflows through HTTP. Use original coordinator ticks and original worker, substituting only the external provider boundary.
- [ ] Test text/image/audio input consumption, queued restart with one Run, endpoint/credential pinning, legacy coding coexistence, disabled gates and invalid registrations.
- [ ] Test the existing `RegisteredRunTarget` against actual DefaultsService snapshots for all input flags.
- [ ] Run `PYTHONPATH=.:tests python3 -m pytest tests/test_workflow_native_startup.py -q --tb=short`; preserve the original failed report outside the repo.

## Task 2: Preserve normal provider bindings

**Files:** Modify `hub/infrastructure/workflow_run_executor.py`.

- [ ] Use one provider-shape predicate for the three inline-input checks: exact required four keys plus optional `secret_ref`; validate optional reference as null or a nonempty string of at most 256 characters.
- [ ] Preserve original serialized bytes and legacy snapshots. Do not alter worker credential selection.
- [ ] Re-run direct target tests and existing Native/provider/worker regressions.

## Task 3: Load Native templates through the existing manifest

**Files:** Modify `hub/composition/workflow_registration.py`; create `hub/composition/native_registration.py`.

**Interfaces:** `read_workflow_manifest(path) -> dict` returns the bounded validated top-level manifest; `read_workflow_registration(path) -> list` returns its coding entries for existing CLI callers. `load_native_registration(entries, config, fleet, repository, *, clock) -> (templates, routes, executors)` supplies original registration values to the existing loader.

- [ ] Separate full-manifest reading from the legacy coding-only accessor, preserving file/size/strict-JSON checks.
- [ ] In `load_workflow_registration`, require Tasks only if coding entries exist. Append Native templates/routes/executors before constructing the original catalog. Reject duplicate target keys, missing/duplicate/extra bindings, unsupported contracts and unavailable catalogs.
- [ ] Resolve config using `fleet['services']['platform_defaults'].resolve_run_config(owner, conversation=metadata['overrides'], overrides={'workspace_id': workspace, 'model_profile_id': profile})`; reject any selected execution node, then replace the live provider binding with the validated manifest `provider_snapshot`. Register flags from the explicit mode, original policy and `CatalogRef('native', 1)`.
- [ ] Run the entire new test file plus startup/bootstrap/Muse/config/boundary/native/backup/provider regressions. No monkeypatch of dispatch success or database terminal states.

## Task 4: Record deployable configuration and evidence

**Files:** Update `docs/platform/workflow-platform/startup-registration.md`, `docs/platform/workflow-platform/README.md`, `docs/platform/development-completion.md`, roadmap, acceptance matrix and `docs/testing/journeys.json`.

- [ ] Document a complete literal Native-only manifest example, existing catalog setup and restart semantics. Link this plan from existing project entry points.
- [ ] Register new required selectors under WF-06 and verify collection resolves every selector.
- [ ] Run required Ruff rules, UTF-8/BOM and diff checks; run Python 3.10 associated tests using the existing isolated dependency environment.
- [ ] Freeze any new Linux associated run separately. The already running `dc6a42b...` full gate proves only its own older source; retain its reports and do not mislabel it as covering this change.

## Verification state

The normal startup path and the provider-snapshot restart regression are implemented; the focused startup suite is **21 passed** and the related Native/provider suites are green. This slice closes one normal configuration gap; a new exact Linux/browser freeze, real model/media services, production deployment and other project TODOs remain separate requirements.
