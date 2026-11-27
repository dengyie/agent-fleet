# Session Upload Pass Recovery Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task in this session.

**Goal:** Keep live session uploading and retained-history recovery finite and cooperatively stoppable without losing durable events or confirmed acknowledgements.

**Architecture:** The uploader owns the pass boundary and ACK handling. SessionBridge passes a stop predicate through; RunnerCapture supplies its background stop event and retained-history replay supplies the existing replay stop predicate. HTTP protocols, spool format, source identity and Hub cursors stay unchanged.

**Tech Stack:** Python 3.10+, existing threading, encrypted LocalSpool, Flask/SQLite, pytest. No new dependencies.

## Global Constraints

- Backend only; do not modify `frontend/` product code or deployment implementation.
- Keep `hub -> fleet_core <- tools`, existing browser APIs, signed commands and idempotency.
- UTF-8 without BOM; preserve primary checkout, existing user state and unrelated work.
- No real Agent CLI, OAuth or model calls. Use temporary encrypted spools and real local HTTP/SQLite services.
- Rebase latest public main before merging; publish through the existing sanitized clone and established CI/deployment procedure.

## Confirmed evidence and scope

Baseline `59e5ef851f48`, `python3 -m pytest tests/test_session_upload_recovery.py -q`: **5 failed**. With a producer appending during each HTTP request, a pass admitted 6 events although only 2 existed at entry. Background stop still uploaded 3 batches; retained-history stop still uploaded 11 batches. The root cause is an unbounded drain loop with neither a fixed last sequence nor an inner stop check. Runner `stop_upload()` joins that worker; replay checks its stop predicate only between whole sessions.

Files and responsibilities:

| File | Responsibility |
|---|---|
| `tools/session/uploader.py` | Capture pass high-water sequence, select only its prefix, stop between requests, persist in-flight ACK |
| `tools/session/bridge.py` | Propagate optional keyword-only stop predicate |
| `tools/session/runner_capture.py` | Wire background/replay stop; retain final flush after stopping background worker |
| `tests/test_session_upload_recovery.py` | Concurrent producer, stop, restart, real capture and HTTP recovery evidence |
| `.github/workflows/ci.yml` | Include the new test module in existing Windows/macOS selections |
| `docs/native-provider-runtime.md`, `docs/testing/README.md` | Record finite-pass behavior, shutdown boundary and reproducible commands |

### Task 1: Finite upload passes

**Interface:** `SessionUploader.flush_once(guard=None, *, should_stop=None) -> int`. Existing positional guard remains supported. `should_stop` is a zero-argument predicate; absent means no cancellation.

- [x] Write and run concurrent-producer regression against original code; preserve red output.
- [x] At entry, read `last_sequence` under `guard`; do not send later sequences in this pass. Keep count/byte limits and contiguous ACK validation.
- [x] Use the existing per-batch scan, filtering its bounded result to `sequence <= high_water`; stop once ACK reaches the boundary.
- [x] Check the predicate before reading/sending each batch. If it becomes true during POST, apply that response before returning. Never cancel or replay native operations.
- [x] Re-run upload and spool regressions, including partial ACK, lost response and restart.

Implementation outline:

```python
with guard:
    high_water = self._spool.status()['last_sequence']
while should_stop is None or not should_stop():
    with guard:
        cursor = self._spool.status()['ack_sequence']
        if cursor >= high_water:
            self.last_error = None
            return total
        pending = [event for event in self._spool.read_after(
            cursor, self._batch_events, self._batch_bytes)
            if event['sequence'] <= high_water]
    # Existing POST and ACK path runs outside/inside guard respectively.
```

### Task 2: Stop propagation and recovery

**Interfaces:** `SessionBridge.flush(guard=None, *, should_stop=None)` and `RunnerCapture.flush(*, should_stop=None)` preserve ordinary callers. `RunnerCapture.start_upload()` passes `self.stopping.is_set`. `replay_pending()` passes its existing predicate. `finish()` deliberately uses ordinary `flush()` after background stop to retain final events.

- [x] Write background stop, in-flight ACK/restart and replay-owner-release tests; confirm failures.
- [x] Wire predicate only into uploader calls; do not abandon an active request or release a spool writer while it is still active.
- [x] Add a real temporary Hub/SQLite HTTP test: stop during an accepted-but-response-lost upload, recreate capture, and recover the same event IDs once.
- [x] Run capture, native continuation and live ACP integration regressions.

### Task 3: Review and production delivery

- [x] Update runtime contract/test index and existing cross-platform selectors. Explain that shutdown can still wait for one in-flight transport call; no arbitrary injected-transport hard deadline is claimed.
- [x] Run relevant tests, Ruff, `git diff --check`, UTF-8/BOM and backend scope checks. No database/schema/spool migration.
- [ ] Rebase latest main, commit and publish sanitized public ancestry; follow PR and main gates.
- [ ] Verify same-run production artifacts, snapshots/rollback, then update signed clients through existing safe procedure with history/config preservation checks.
- [ ] Update canonical vault documentation on vault master with actual evidence and report remaining boundaries.

## Fresh local verification

- Original regression: **5 failed** on unmodified product code; private evidence `session-upload-recovery-20261127/baseline-red.txt`.
- First uploader/spool/capture regression: **118 passed** before strengthening the new cases.
- Final relevant suites (upload, spool, bridge, capture, native sync/continuation, ACP worker/permissions, HTTP/idempotency/cursors, frontend contracts and release layout): **388 passed, 5 subtests**.
- Final release gate: `python3 -m pytest tests/test_session_upload_recovery.py tests/test_boundaries.py tests/test_cli_stdlib_imports.py tests/test_provider_transport_boundaries.py tests/test_journey_gate.py tests/test_release_layout.py -q` — **102 passed, 2 subtests**. This overlaps the wider suites; counts are not added.
- Ruff critical/syntax/import selection, `git diff --check`, and UTF-8 without BOM inspection passed. No frontend product changes, dependencies or schema migrations.
- The new module contains eight parameterized behavior cases and is included in Windows/macOS CI and the existing SESSION-01 journey. Hosted full CI, production and signed-node rollout evidence will be retained separately after publication; local tests alone do not prove deployment.
