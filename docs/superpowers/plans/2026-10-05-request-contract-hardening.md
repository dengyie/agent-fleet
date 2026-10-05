# Request Contract Fidelity Implementation Plan

> Execution: inline in the existing isolated worktree, one complete file at a time.

**Goal:** Preserve documented monetary precision and exception-cause evidence at the existing provider boundary.

**Architecture:** Keep pure request values and typed public metadata in `tools/platform/request_metadata.py`; adapters decode wire values, and the worker's existing `log_failure` remains the only error log entrypoint. Do not introduce another service, serializer framework, provider facade, or event store.

**Tech Stack:** Python 3.10+, Flask, SQLite, native ES modules, pytest, Playwright 1.63.0. No new project dependencies.

## Global Constraints and Source Mapping

- `docs/testing/request-metadata.md`: decimal USD values, nullable token details, provider-cost precedence, frozen estimates, bounded JSON/SSE decoding and no replay after accepted-response failures.
- `docs/platform-diagnostics.md`: retain safe exception types and explicit cause frames; never log raw exception messages, prompts, response bodies or credentials.
- `docs/superpowers/plans/2026-09-26-m1-local-run-worker.md`: immutable configuration and lease-fenced events/terminal writes.
- The existing synchronous provider and UTC epoch exchange are explicit repository contracts. This change does not migrate the runtime or timestamps.
- Monetary wire numbers must not pass through binary floating point; ordinary tool argument numbers retain their existing JSON representation.
- Response processing remains O(n) time / O(n) bounded response storage, with an O(n) conversion only for the existing structured-argument mapping variant. No duplicate full-body parsing.

## Task 1: Exact wire amounts and explicit metadata types

Files: `tools/platform/request_metadata.py`, `tools/platform/providers/base.py`, `tools/platform/providers/openai_compatible.py`, `tools/platform/providers/observation.py`, `hub/domain/request_metadata.py`, `hub/domain/run.py`; regression `tests/test_provider_contract_fidelity.py`.

- [x] Preserve a raw JSON cost `123456.123456123456` exactly through both JSON and SSE; current result `123456.12345612346` proves the defect. Cover zero and precision limits separately.
- [x] Decode JSON fractional tokens with `json.loads(..., parse_float=Decimal)`. `decimal_amount` already validates Decimal values. Map Decimal values back to ordinary finite JSON numbers only within structured tool arguments; preserve nested lists/dicts and JSON serializability.
- [x] Declare TypedDict contracts for usage, pricing, cost items and request metadata in the existing pure module; use them in provider responses, observers and public request projection. No new runtime validation framework or mandatory metadata for old histories.
- [x] Verify independent wire fixtures; assert exact amount and all nested tool argument values/types.

## Task 2: Preserve root causes without leaking messages

Files: `tools/platform/providers/openai_compatible.py`; regression `tests/test_provider_contract_fidelity.py`.

- [x] Inject a timeout containing a private sentinel and assert the resulting ProviderError has the original exception as `__cause__`; current cause is None.
- [x] Cover connection/read timeout, OSError, JSON syntax, SSE UTF-8 and SecretBroker failures; use `raise ProviderError(...) from exc` (or ProviderUnavailable for broker resolution) at existing translation boundaries.
- [x] Log through the existing `hub.diagnostics.log_failure`; assert original exception type/location survives but the private marker is absent from errors, observations and serialized log records. Do not add adapter-level logging or alter retry decisions.
- [x] Assert accepted-body failures still produce one HTTP attempt, unknown status and known HTTP status; no regression to retry behavior.

## Task 3: Verification and documentation

Files: `docs/testing/request-metadata.md`, `docs/testing/journeys.json` and this plan.

- [x] Add each new function selector to USAGE-01 / MODEL-02 as appropriate, keeping existing coverage.
- [x] Run focused provider/request/worker/diagnostic tests; compile changed Python modules and parse them with the Python 3.10 grammar. No static type checker is currently configured in this repository.
- [x] Run the complete pytest journey gate with the configured real Chromium runner, JUnit and screenshot evidence. Fix any new failures in this scope before final delivery.
- [x] Recheck the full diff against the source documents, record verified scope/limits, and preserve unrelated T3.7 work.

## Verified result

- Baseline focused suite: 49 passed. Initial independent wire/cause regression: 3 failed / 1 passed before the fixes; expanded focused suite: 123 passed.
- Final local complete gate: 2530 passed, 154 subtests, 2 macOS `/proc` skips, 27/27 journeys; real Chromium enabled. Evidence: `/tmp/agent-fleet-contract/final/` (temporary), with JUnit, journey report and screenshots. Linux release CI must cover both platform-specific cases with no skips.
- Changed Python modules compile and parse with the Python 3.10 grammar; no static type checker is configured, so these checks are not presented as static type verification. `git diff --check` passed.
- Bounded review conclusion: both confirmed defects are fixed in the request metadata/provider boundary; no additional blocking finding or confirmed dead code in this change. Existing synchronous I/O, UTC epoch wire values, retry decisions, storage and unrelated T3.7 work remain governed by their current specs. This is not an assertion of zero defects across the whole repository.
