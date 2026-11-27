# VM Native Stream Reliability Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task in this session.

**Goal:** Apply verified vm2api control/data-plane lessons to Fleet's native Agent stream and process completion without duplicating its VM gateway.

**Architecture:** Keep browser operator HTTP, signed task/command delivery, node-owned native processes, encrypted capture and stable Hub ingestion cursors. Improve the existing ACP transport and Supervisor at their ownership boundaries; kernel process liveness and protocol completion require separate evidence.

**Tech Stack:** Python 3.10+, stdlib subprocess/threading/queues, existing Flask/SQLite and pytest; no dependency additions.

## Global Constraints

- Backend only; do not modify `frontend/` product code or deployment implementation.
- Reuse `hub -> fleet_core <- tools`, existing APIs/receipts/idempotency and capture DTOs.
- UTF-8 without BOM. Preserve the protected primary checkout and unrelated edits.
- No real vendor Agent/model/OAuth calls. Use independent local stdio peers and real process groups.
- Do not copy restricted upstream code or claim unavailable kernel source was reviewed.
- Rebase latest public main, use existing CI/artifact/receiver and signed client release paths.

## Source and file responsibilities

Pinned vm2api public main: `2eab36cb90b0897aed1cbcf911ae6f4a0d88ca81` (2026-11-27). See the linked source comparison for evidence and boundaries.

| File | Responsibility |
|---|---|
| `tools/providers/acp_transport.py` | Incremental binary framing, bounded writes, pending response/error coordination |
| `tools/supervisor/supervisor.py`, `tools/supervisor/native_resume.py` | Verify process-tree cleanup before successful managed completion |
| `tests/test_acp_transport_bounds.py` | Independent real-pipe framing, cancellation, contention and terminal transport tests |
| `tests/test_supervisor_process.py` | Cleanup refusal/unknown/retry regressions and real process lifecycle |
| `tests/test_agy_acp.py`, independent peer | Prove text/tool delivery occurs before prompt completion |
| `docs/research/vm2api-architecture-reference.md` | Pinned source evidence, adoption decisions and remaining architecture boundaries |
| `docs/native-provider-runtime.md`, `docs/testing/README.md` | Authoritative runtime behavior and reproducible test entry points |

### Task 1: Incremental native stdio framing

**Interfaces:** Keep `AcpTransport(process, ...)`, `request`, `notify` and all DTOs unchanged. Add private chunk framing inside the transport; a frame is at most `MAX_FRAME` bytes including newline, CRLF is accepted, UTF-8 is decoded only after a full frame. A partial EOF fails closed using the existing bounded frame error family.

- [x] Add a test pipe wrapper whose `readline` fails, whose `read` returns bounded fragments, and which verifies exact multibyte payloads and many coalesced frames.
- [x] Run `python3 -m pytest tests/test_acp_transport_bounds.py -q`; confirm new evidence fails on current `readline` implementation.
- [x] Read with `read1` when available, otherwise `read`, at most 64 KiB. Accumulate only one bounded partial frame and parse complete frames before reading more. Reuse strict JSON duplicate-key/nonfinite and RPC validation.
- [x] Cover one-byte UTF-8 fragments, multiple frames/read, exact limit, oversize newline-free stream, partial EOF and malformed UTF-8. Verify a real child emits an update and waits for a parent acknowledgement before returning its RPC result.

### Task 2: Bound outgoing contention and stop ambiguous dispatch

**Interfaces:** Keep one-second write budget and existing bounded `write_timeout`, `cancelled`, `protocol_eof` errors. The one-second budget covers lock acquisition, outgoing queue and flush together. An already-aborted request must not write bytes. `close()` wakes pending callers with a bounded error; first terminal error wins. Notifications remain available for native cancellation before runtime close.

- [x] Add deterministic lock contention, pre-cancelled real stdio, concurrent close/failure and close-wakes-request tests. Use events/barriers instead of probabilistic scheduling.
- [x] Run targeted tests and preserve the failure evidence.
- [x] Implement one monotonic write deadline, bounded lock wait and failure notification. Check request cancellation before dispatch. Serialize first-error transition with a lock, wake waiters on close and retain duplicate response rejection.
- [x] Run transport, permission races, accounts and actual supervised ACP worker regressions; assert prompt count at most one and no private peer error text in captured public rows.

### Task 3: Verify process-tree completion

**Interfaces:** `wait_session` success still returns `outcome="finished"` only with EOF and confirmed reap plus group absence. Unconfirmed cleanup returns nonzero `125` and existing `escape_unverified`, keeps the handle/owner and a nonterminal manifest for a later explicit stop. Preserve the fence across restart and refuse pause/resume/profile changes that could clear it. Tail timeout after verified cleanup keeps existing `group_remaining`/124 behavior.

- [x] Add fake-backend cases for reap false/exception, liveness true/exception, forced cleanup failure and subsequent explicit terminate; assert no successful completion or owner loss.
- [x] Run the new tests and confirm failure before changing completion.
- [x] In `_finish_completed_locked`, verify `group_reap` and `group_alive` before closing/releasing/marking terminated in both EOF and timeout branches. Preserve original nonzero exit code when clean.
- [x] Run actual subprocess lifecycle/cancellation suites and independent worker capture→Hub SQLite/HTTP tests.

### Task 4: Document, review and publish

- [x] Link pinned source study from runtime contract; describe actual host/guest topology, API compatibility and capability/terminal proof boundaries.
- [x] Register new tests in existing journey selectors and cross-platform CI suites (reuse already selected files).
- [x] Run relevant existing tests, architecture/import/release layout checks, `git diff --check`, UTF-8/BOM check and self-review. No schema migration is needed.
- [ ] Commit feature, rebase latest main and publish through sanitized public history; follow full PR/main CI and production receiver evidence. Update signed clients only through the established release procedure.
- [ ] Update canonical vault entry/index on vault master with verified evidence; report exact tests and remaining real-VM/vendor acceptance boundaries.


## Executed evidence

- Baseline transport regressions: **6 failed, 29 passed**. Lock contention exceeded its deadline, a pre-cancelled prompt wrote bytes, close left a caller waiting, and the reader used `readline`.
- Baseline cleanup regressions: **5 failed, 1 passed** (four subtest failures included). EOF cleanup failure reported `finished`; tail cleanup failure dropped the handle as `group_remaining`.
- Follow-up/restart fence and control regressions were added after tracing their effect on cleanup state; they also failed before the guards were implemented.
- First fresh transport/Supervisor run: **106 passed, 2 skipped, 6 subtests**.
- Actual worker/permission/control/process suite: **236 passed, 2 skipped, 6 subtests** before the final added contention/fence cases.
- Follow-up framing/worker/race run: **137 passed, 2 skipped, 6 subtests**. Final wider verification and CI results are recorded below when completed.

- Wider relevant regression: `python3 -m pytest` over ACP, Supervisor/control, native continuation, provider accounts, standalone imports, release layout, capability layers and session HTTP suites: **680 passed, 2 skipped, 11 subtests**, 41.16 s.
- Release review regression across transport, real worker, Supervisor, continuation, control, dependency lock, standalone imports and release layout: **323 passed, 2 skipped, 8 subtests**, 25.35 s. Last tests were then strengthened to enforce actual one-byte reads and bind an otherwise valid native identity to the cleanup fence.
- `ruff check --select E9,F63,F7,F82,F401 hub tools connectors fleet_core`: **passed** (installed CLI 0.17.4). `git diff --check` and changed-file UTF-8/no-BOM validation: **passed**. No schema or dependency changes.
- Private raw evidence: `/Users/mango/.cache/agent-fleet/verification/vm-native-stream-20261127/`. Published source comparison contains no credentials or copied upstream implementation.

- Final strengthened release gate (ACP transport/worker/races, Supervisor/process/control, continuation and provider control/e2e): **261 passed, 2 skipped, 6 subtests**, 22.88 s.

- First PR Windows CI45977337146 failed one existing Hermes completion test (`escape_unverified` after clean exit; 1193 passed/101 skipped). Investigation found that successful Windows `group_reap` releases its empty Job, then the new completion check falls back to a global PID tree. The fix retains empty-Job evidence on the owned private handle; PID reuse/snapshot failure cannot reverse that proof. Deterministic cases failed before the fix; an unreadable/failed-stop Job remains unconfirmed. `tools/supervisor/handles.py` and `group_ops.py` own this private proof, and `tests/test_round5_windows_process.py` owns its regression.

- Empty-owned-Job proof fix local verification: **206 passed, 2 skipped, 6 subtests**, 15.40 s across Windows job/process, cross-platform Agent, tail, control and ACP suites. The unknown-Job test initially allowed its fake terminate to succeed; the fixture now explicitly refuses termination so it tests genuinely unconfirmed cleanup.
