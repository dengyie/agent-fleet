# Full-flow testing Implementation Plan

> **For agentic workers:** Use executing-plans to implement this plan task-by-task in the current isolated worktree. Steps use checkbox syntax for tracking.

**Goal:** Make supported user journeys independently verifiable and prevent a green CI result when required journeys were omitted or skipped.

**Architecture:** Keep existing unit/integration suites. Add a strict loopback model server at the external HTTP boundary, exercise the unmodified account/session/scheduler/provider/tool/artifact stack from a browser, and map the whole product to collected pytest cases. A separate account-authenticated release probe produces explicit per-model evidence; local tests never imply external-provider success.

**Tech Stack:** Python/pytest, Flask/Werkzeug, Playwright, JSON Schema; existing Node/browser installation in CI.

## Global Constraints

- Primary worktree remains clean; work on `codex/full-flow-test-coverage`.
- No real credentials, prompts, mailbox contents or browser traces in CI artifacts.
- No automatic replay of unknown production runs; release probes create labeled read-only conversations.
- Missing browser dependencies, required cases, skipped cases and expected failures cannot count as required coverage.
- Runtime feature gates, external providers and real mail delivery are explicitly represented as distinct validation boundaries.

### Task 1: Strict provider and actual stack

**Files:** `tests/support/strict_provider.py`, `tests/support/full_flow.py`, `tests/test_full_flow.py`, `tests/test_full_flow_browser.py`, `tests/fixtures/frontend/full_flow_browser.cjs`, `requirements-test.txt`.

- [x] Validate wire tool names against `^[A-Za-z0-9_-]{1,64}$`, schemas with JSON Schema, matched tool_call IDs, result JSON and bounded request shape. Add a deliberately invalid dotted-name request and assert HTTP 400.
- [x] Serve a scripted list/write/read/artifact/final sequence over loopback HTTP. Use the real provider factory and scheduler; verify file bytes, downloaded artifact digest, final reply and persisted events.
- [x] Exercise account login and invitation/registration/recovery, owner separation, idempotent turn submission, queued/running cancellation, HTTP errors and restart recovery with real Hub HTTP requests.
- [x] Browser: login guard → account login → task submission → leave/return → tool output → final reply → artifact preview/download → refresh → provider failure → recovery → logout.
- [x] Run `PYTHONPATH=. python -m pytest tests/test_full_flow.py tests/test_full_flow_browser.py -q` with browser environment configured; capture and repair actual failures.

### Task 2: Required journey gate

**Files:** `docs/testing/journeys.json`, `tools/testing/pytest_journeys.py`, `tests/conftest.py`, `tests/test_journey_gate.py`, `.github/workflows/ci.yml`.

- [x] Map each supported surface to exact existing/new test node prefixes and measurable acceptance, explicitly identifying simulated/external boundaries.
- [x] Add `--require-journeys` and `--journey-report`; compare collected cases and actual setup/call/teardown outcomes, including every parameterization.
- [x] Test missing selectors, skipped and xfailed cases, teardown failure, valid parameterized execution and report generation with disposable pytest subprocesses.
- [x] CI installs test dependencies and runs the complete suite with required-journey enforcement before packaging; preserves report with existing JUnit/screenshots.

### Task 3: Repeatable release acceptance

**Files:** `tools/platform/acceptance_check.py`, `tests/test_platform_acceptance_check.py`, `docs/testing/release-acceptance.md`.

- [x] Authenticate using account credentials from a protected file; refuse redirects, credential-bearing URLs and non-loopback cleartext origins.
- [x] Read readiness/catalog/release version; require explicit selected models or `--all-models`; submit exactly one labeled read-only task per model.
- [x] Verify terminal success, actual successful workspace.list event, nonempty final result, conversation recovery, and session revocation. Bound polling and report uncertainty without retries or raw response bodies.
- [x] Test CLI exit/report behavior against the same real disposable Hub, including provider rejection, unknown and readiness/authentication failures.

### Task 4: Documentation, complete verification and delivery

**Files:** `docs/testing/README.md`, `docs/testing/release-acceptance.md`, `docs/platform-diagnostics.md`, `README.md`.

- [x] Document workflow inventory, test layers, exact commands, failure triage, evidence retention and external acceptance. Replace stale token-login production instructions with current account flow.
- [x] Run full suite with `--require-journeys --journey-report`, check all mapped outcomes, then run the new account-authenticated release probe against selected live models and report individual results.
- [x] Rebase the publication branch onto latest public main, publish a reviewable PR, verify CI and record precise tests and external limits. Do not claim absence of all future bugs or use test counts as proof of user-flow coverage.
