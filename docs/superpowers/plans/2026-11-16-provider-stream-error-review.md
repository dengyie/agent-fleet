# Provider Failure Semantics Review Implementation Plan

> Execute sequentially in the existing isolated worktree. No delegation is required.

**Goal:** Preserve explicit HTTP rejection semantics when diagnostic-body reads fail, and prevent in-band stream errors from committing incomplete answers or executing partial tool calls.

**Architecture:** Keep classification at the provider boundary. Reuse the existing retry loop, request observer, durable Run events and history projection; no new service, retry layer, database schema or UI state source.

**Tech Stack:** Python, stdlib HTTP, pytest, existing SQLite/Flask worker and browser fixtures.

## Constraints

- Baseline: GitHub main `193d9b6c7b250a6e5cc74828e83009922d49a373`; protected primary checkout stays untouched.
- API-site channel retry is owned by another agent and is outside this repository change.
- No production model requests, historical request replays, configuration changes, credentials changes, or new model execution limits.
- UTF-8 without BOM. Every repair requires a failing regression before production code changes.

## Task 1: Explicit rejected HTTP status survives a diagnostic read failure

Files: `hub/platform/providers/openai_compatible.py`, `tests/test_provider_transport_boundaries.py`.

- [x] Reproduce `503` followed by a diagnostic-body timeout: with one configured retry, the second successful HTTP response must be reached; verify the unmodified adapter fails this test.
- [x] Cover retry exhaustion and `401`/`403`/`400` controls with timeouts, connection failures and malformed HTTP bodies. The final diagnostic retains HTTP classification, cause and received request IDs.
- [x] Extract the existing status-to-ProviderError classifier and reuse it both after normal error-body reads and after read failure. Keep unknown pre-header/accepted-response failures non-retryable.
- [x] Run `PYTHONPATH=. python3 -m pytest -q tests/test_provider_transport_boundaries.py tests/test_provider_contract_fidelity.py tests/test_request_metadata.py tests/test_platform_provider.py`.

## Task 2: Reject error frames before committing streamed responses

Files: `hub/platform/providers/openai_compatible.py`, `tests/test_request_metadata.py`, `tests/test_request_metadata_api.py`, `tests/test_assistant_live_feedback.py`.

- [x] Reproduce text/tool deltas followed by an explicit error frame and a DONE marker; current code must not silently succeed or dispatch the partial tool.
- [x] Introduce the stable `provider_stream_error` code at the SSE parser's error-frame boundary. Preserve only allowlisted upstream error codes; raw error messages and reasoning stay private.
- [x] Verify one HTTP attempt, closed response, unknown request outcome, retained partial public text and gateway IDs, no tool execution, and durable Run/Conversation history across restart and retention.
- [x] Run the original failing cases, provider/worker/recovery regressions and existing real-browser live-feedback scenarios.

## Task 3: Keep streamed presentation retention separate from execution

Files: `hub/platform/providers/observation.py`, `hub/domain/run_progress.py`, `fleet_core/request_metadata.py`, `tests/test_assistant_live_feedback.py`.

- [x] Reproduce missing omission signals for ASCII/UTF-8 streams interrupted by an error or disconnect (4 failing cases). Reproduce loss of cancellation checkpoints after the public-text limit (1 failing case).
- [x] Send one explicit clipping marker through existing text events and consume it in the existing pure projection. Continue throttled activity after presentation retention fills.
- [x] Verify the original interrupted-history cases, post-limit cancellation, response closure, no replay and worker/restart/retention regressions.

## Task 4: Review, contract and release evidence

Files: `docs/testing/request-metadata.md`, `docs/testing/journeys.json`, `docs/reviews/2026-11-16-provider-failure-production-review.md`.

- [x] Record confirmed findings with triggers, impact, root causes, minimal fixes and exact regression selectors; link from the existing request contract.
- [x] Register every new regression selector in the appropriate existing MODEL-02, ASSIST-02 or USAGE-01 journey.
- [x] Run lint, diff checks, focused regressions, browser tests and journey/release tests. Preserve red/green evidence outside the repository.
Release acceptance: rebase against fresh main before merging; publish only through existing exact-commit CI and deployment gates. Verify the deployment read-only without making paid model requests. The canonical operations runbook records the final SHA, original CI evidence and deployment receipt after release.

## Local verification

- Clean baseline: 101 provider/metadata/transport/state tests passed. New failure evidence: 14 HTTP/SSE classification cases, 4 interrupted clipping cases, and 1 post-limit cancellation case failed before their respective repairs.
- Provider/observer/worker/history checks: 169 passed. Broader runtime/checkpoint/lease/HTTP/frontend/journey/release checks: 272 passed. These scopes overlap; do not add them as a unique total.
- Real-browser success and stream-error branches passed again after waiting for responsive sidebar transitions before screenshots. Desktop and 390px output were visually inspected.
- Lint and diff checks passed; all 10 new regression functions are mandatory journey selectors. No software downloads, production model calls, historical replays, settings or credential changes.
