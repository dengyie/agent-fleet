# Provider transport boundaries implementation plan

> Execute inline with evidence-driven-bugfix and executing-plans; keep each regression attributable to its root cause.

**Goal:** Fix the four reproduced production review findings: credential forwarding on redirects, unbounded request duration, delayed SSE completion, and a discarded SecretBroker cause.

**Architecture:** A shared stdlib HTTP boundary owns one monotonic deadline and its socket from DNS through response close. Provider and release acceptance reuse it; domain/runtime APIs, Decimal metadata and persisted event contracts stay unchanged. DNS workers only resolve names, share pending lookups, and have a fixed concurrency limit; no background worker may dispatch late HTTP requests.

**Tech stack / constraints:** Python 3.10+, urllib/http.client/socket/threading; no additional dependencies. Preserve system proxies for provider requests, default certificate/hostname verification, byte limits and safe diagnostic codes. Response processing O(n), pending DNS lookup O(1) with at most 8 entries. No async framework migration, new provider facade, pricing changes or unrelated browser changes.

**SSOT:** `docs/testing/request-metadata.md`, `docs/platform-diagnostics.md`, `docs/superpowers/plans/2026-09-26-m1-local-run-worker.md`, `docs/testing/README.md`, `docs/testing/release-acceptance.md`.

## 1. Reproduce

- [x] Add `tests/test_provider_transport_boundaries.py`: real two-origin redirect fixture for 301/302/303/307/308, chunked SSE DONE without HTTP EOF, trickled JSON/headers and full Broker→worker→safe log chain.
- [x] Run `PYTHONPATH=. python -m pytest tests/test_provider_transport_boundaries.py -q`; capture the failing assertions before implementation.

## 2. Close the HTTP boundary

- [x] Create `tools/platform/http_transport.py`: `open_http(request: Request, *, timeout_s: float, use_proxy: bool = True) -> DeadlineResponse`; all status codes returned without following redirects. Response exposes status, headers, bounded-size read/read1 and explicit context-managed close.
- [x] Share pending DNS lookups by host/port with bounded admission. Use request-owned deadline/socket and a joined interruption timer; TLS wrapping registers the SSL socket before handshake. Recheck deadline after reads so truncated EOF cannot become a success.
- [x] Update `UrllibTransport` to use the shared boundary, explicit closable iteration and read1; reject redirects without replay; preserve typed timeout/network causes. Accepted-body failure stays outside the dispatch retry loop.
- [x] Migrate `AccountClient` onto the shared boundary with `use_proxy=False`, retaining cookies, expected status, per-model deadlines and no submission replay. Remove its superseded DNS/connect/timer implementation.
- [x] Verify focused provider, acceptance deadline and metadata tests. Add bounded resolver saturation/late completion, TLS validation, proxy, early-close and timeout cleanup checks.

## 3. Preserve the worker cause

- [x] Replace the worker's `raise RuntimeError(str(exc)) from None` with `raise RuntimeError(str(exc)) from exc` at the ProviderUnavailable boundary.
- [x] Verify the persisted result and log retain safe types/locations and exclude the sentinel secret. Verify timeout releases the worker slot and the next queued Run can execute.

## 4. Verify and release

- [x] Update request metadata/testing docs and exact journey selectors for all new scenarios.
- [x] Run compile/Python 3.10 syntax checks, full pytest and all required browser journeys; inspect output and final diff.
- [x] Commit, fetch/rebase onto latest main, merge via the established public release clone, push main and await CI deployment.
- [x] Verify deployed manifests, public asset hashes, DB integrity, live request metadata and browser recovery; update the canonical Obsidian deployment note and linked indexes, then scan for stale/broken notes.

Local verification: 2549 passed, 154 subtests passed, 2 macOS-only platform skips; 27/27 journeys. Python 3.10 grammar and compile checks passed. Linux CI must prove the release revision with no skips. Initial red evidence: all 9 original regression cases failed before the fixes.

Release verification: Linux CI run [37261284190](https://github.com/dengyie/agent-fleet/actions/runs/37261284190) tested, packaged, and deployed exact revision `6e547a2f5cbb7315239b8d18a3c5a422964fc8a7`; all three jobs succeeded without skips. The canonical Obsidian deployment note records production manifest/hash, database integrity, request metadata, browser recovery, and rollback evidence.
