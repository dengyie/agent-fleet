# Agent capability layers implementation plan

> Execute in this isolated feature worktree, task by task, with behavioral and dependency checks before integration.

**Goal:** Give cross-platform Agent execution, continuation, capture, Hub admission and browser presentation explicit, independently testable boundaries.

**Architecture:** Keep `hub -> fleet_core <- tools`; declarative family policy remains in `agent_profiles.py`. Hub application services load authoritative facts and call a pure domain policy. Node adapters own CLI syntax, native normalization owns vendor event formats, and the browser consumes bounded public session DTOs through the existing API client.

**Tech Stack:** Python stdlib/dataclasses, Flask transport, SQLite infrastructure, static ES modules, pytest and optional installed Chromium.

## Global constraints

- Preserve native identities, arguments, source archives, continuation checkpoints and signed command protocols.
- No database migration, new framework, new network channel or global client configuration changes.
- Primary checkout remains protected; UTF-8 without BOM.
- Family support is potential capability, never runtime authorization or installation evidence.

## Task 1 — Declarative family capability policy

- [x] Extend `agent_profiles.Profile` with `model_protocol` and `runner_resume_only`; derive `AGENT_PROTOCOLS` in `fleet_core/session_models.py`.
- [x] Add `canonical_family(value)` for the existing `claude` alias and reuse it in Hub admission/model/profile matching.
- [x] Verify registry invariants and model-delivery compatibility: `python3 -m pytest tests/test_agent_profiles.py tests/test_session_model_takeover.py tests/test_native_model_config.py -q` (select existing session-model test filename).

## Task 2 — Pure Hub session access policy

- [x] Create `hub/domain/session_access.py`: frozen `AdoptionAccess` snapshot and `project_session_access(row, adoption, actions, *, adoption_available)` returning the existing public DTO.
- [x] Move `_live_session` policy from `hub/application/session_service.py`; the application only creates a snapshot and calls the policy. Repositories and authority checks stay in the application layer.
- [x] Declare `SessionStore`/`TranscriptStore` ports, storage result and bounded repository errors in `hub/domain/session_ports.py`; application imports the contracts, SQLite adapters re-export the existing error/result names.
- [x] Add policy tests for all families, managed/unmanaged, active/revoked/missing adoption, busy/unbound Runner, desktop bridge, catalog gate and input immutability.
- [x] Run policy and shared HTTP/assistant admission tests.

## Task 3 — Node syntax and native format isolation

- [x] Move `continuation_options` and resume argv syntax into `tools/adapters/native_resume.py`. Export `build_resume_argv(family, exe, token, text, options=())` so Supervisor no longer inserts family-specific flags or Node script prefixes.
- [x] Keep existing `_native_resume_argv`/`continuation_options` imports compatible.
- [x] Move native JSON shaping functions from `tools/session/bridge.py` into pure `tools/session/normalization.py`; preserve bridge re-export compatibility.
- [x] Run adapter, continuation, capture, model configuration and lifecycle regressions, including hostile prompt/options and Windows path fixtures.

## Task 4 — Public browser contract and presentation

- [x] Reproduce missing Hermes follow-up and dropped `message_delivery` DTO in automated fixtures before changes.
- [x] Add bounded public `message_delivery`, `capture`, `control_unavailable_reason` parsing in `frontend/assets/api/contracts.js`; discard unknown data, reject invalid declared shapes, and never infer an action from capability metadata.
- [x] Remove the family veto in `frontend/assets/views/session.js`; authorized actions remain the sole UI admission source. Render bounded delivery reasons through a pure `frontend/assets/ui/session-capabilities.js` presentation model.
- [x] Extract source-control DOM from `views/machine.js` into shared `ui/source-control.js`; both views import it while the original export stays compatible.
- [x] Test older DTO compatibility, unsupported/revoked/busy sessions, unknown families and correct Hermes follow-up submission via real loopback HTTP/Chromium.

## Task 5 — Architectural gates, documentation and integration

- [x] Add dependency tests proving pure policy, CLI syntax and native normalization import without transport/storage/process dependencies; ensure guards run on the Windows/macOS adapter CI jobs.
- [x] Document module responsibility, API contract, extensibility checklist and capability/authorization distinction; link from existing architecture/session docs and README. Update existing Obsidian development/index entry and run doc-check.
- [x] Run lint, diff/encoding checks, relevant backend/frontend suites and client package closure verification; inspect final diff.
- [ ] Commit, rebase latest main, publish PR and follow the authorized integration/release chain with exact CI evidence. Preserve rollback/evidence and avoid unnecessary native messages for pure moves.

## Local verification (2026-11-21)

- macOS native adapter CI selection: 376 passed, 2 skipped, 103 subtests passed.
- Backend/session/storage/package regressions: 320 passed, 10 subtests passed.
- Frontend contract, Chromium follow-up/management and release regressions: 226 passed, 2 subtests passed.
- Ruff production rules and `git diff --check` pass; vault doc-check: 4/4, 96 checks, no errors or warnings.
- Evidence is retained outside the repository under the operator verification directory `agent-capability-layers-20261121`; overlapping suites are not added together as unique coverage.
