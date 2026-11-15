# Provider Request Correlation Implementation Plan

> Execute sequentially in the existing isolated worktree; no delegation is needed.

**Goal:** Preserve API gateway and upstream request identifiers for individual HTTP attempts so production errors can be correlated directly with gateway logs.

**Architecture:** Keep request identity (`attempt:step:request_index`) and model response identity unchanged. Extract only two known response headers at the provider boundary, validate their opaque identifiers, and pass them through the existing request lifecycle, bounded history projection, and expandable UI details.

**Tech Stack:** Python provider/observer, SQLite Run events, existing JavaScript request details, pytest and Chromium acceptance. No dependencies or configuration changes.

## Constraints and root cause

- The transport already captures response headers, but the provider discards their request identifiers on both successful and failed attempts.
- MangoApi's actual header names are `X-Oneapi-Request-Id` and `X-Upstream-Request-Id`, confirmed in its request-id middleware and constants.
- Keep gateway internal retries distinct from AgentFleet HTTP attempts. This change does not modify retry classification, execution budgets, cancellation, or ambiguous replay policy.
- Preserve no raw headers, credentials, error bodies, prompts, replies, or reasoning. Only bounded opaque identifiers may enter diagnostics.
- UTF-8 without BOM; leave the protected primary worktree untouched.

## Task 1: Reproduce the missing correlation

- [x] Add failed-then-success and accepted-stream-failure regression tests in `tests/test_request_metadata.py`; verify failure before changing implementation. All three new scenarios failed on discarded identifiers before the fix.
- [x] Add projection/retention and real HTTP boundary tests for the same identifiers.

## Task 2: Preserve identifiers through existing boundaries

- [x] Add optional typed fields and a shared bounded identifier validator in `fleet_core/request_metadata.py`.
- [x] Extract only the two response headers in `hub/platform/providers/openai_compatible.py`, retaining them on explicit HTTP failures and interrupted response reads as well as successful results.
- [x] Allow validated identifiers in `hub/domain/request_metadata.py`; use existing frozen history and totals.
- [x] Display the two identifiers only within existing expanded details in `frontend/assets/views/assistant/request-metadata.js`.

## Task 3: Verify the complete request history

- [x] Extend worker/API restart/retention tests and the existing real-browser retry fixture to verify per-attempt identity and unchanged collapsed summaries.
- [x] Update `docs/testing/request-metadata.md` and register exact tests in `docs/testing/journeys.json`. Link this plan from the existing request metadata documentation.
- [x] Run focused red/green tests, provider/worker/history regressions, browser acceptance, lint, and diff checks.

## Release verification

Local evidence: 185 provider/worker/history/browser regressions, 124 frontend/browser checks, and 134 journey/release-evidence checks passed. The initial bundled Chromium revision was absent; browser verification used the already installed Chrome without downloading software. Mobile screenshots wait for the navigation transition to finish and show the actual correlation details.

Publish through the existing PR and main CI pipeline. Full Linux journey evidence, Windows checks, packaging, deployment receipt, and read-only online revision/hash checks must come from the exact published commit; local checks do not certify deployment. Do not send production model requests merely to create diagnostic identifiers. Preexisting historical records without captured identifiers remain unchanged.
