# Native Run Checkpoint Implementation Plan

**Goal:** Resume completed native model/tool steps after worker loss without repeating uncertain external calls.

**Architecture:** Store a private bounded checkpoint on the existing Run row. Every update checks the current unexpired Run lease in a SQLite writer transaction. Safe phases are ready, response, receipt and complete; provider_pending and tool_pending are uncertain and never replayed. Keep Flask/SQLite and hub -> fleet_core <- tools.

**Scope:** This slice implements durable native progress. Nonblocking remote command waits, general stop/renew, archive/fence retirement, F5 extensions and release/live acceptance remain required items in development-completion.md.

## Task 1: Recovery evidence and private persistence

- [x] Add real worker/SQLite crash tests: completed tool receipt resumes without repeating the tool or prior provider call; completed final answer survives restart; provider/tool pending remain unknown; stale worker cannot save.
- [x] Run tests before implementing; retain original failing output.
- [x] Add nullable private runs.runtime_checkpoint column and lease-fenced load/save adapter, with an explicit 8 MiB serialized ceiling. Never include it in public Run projections.
- [x] Allow expired native claims to resume only from valid safe checkpoints; keep existing conservative recovery for all older Runs and uncertain phases.

## Task 2: Runtime persistence

- [x] Persist original transcript, step and cumulative usage before provider invocation, after settled response, before tool dispatch, after tool receipt and before final return.
- [x] Restore counters/step/transcript and response/final results on a new worker. Stable run_id:step:N identity remains unchanged.
- [x] Verify original crash cases, cancellation, lease fencing, usage, privacy, conversation deletion and backup behavior. Run associated regression modules and update journey selectors.

## Task 3: Completion records

- [x] Update platform contract, development ledger and existing canonical Obsidian entry/indexes. Full gate on the eventual integrated candidate; no claim of release readiness from focused tests.

## Verification and remaining integration

- Completed immutable checkpoint freeze: `06e8758615605d310591e9c2236b798f60d8a2b5`, 6921 passed / 5 platform skips / 204 passing subtests / 1020.59s; 43/44 journeys, 1995 selectors, 46 valid PNGs. Source digests unchanged (1127 files), clean checkout, 1051 backend and 67 frontend files match the freeze. The original verifier rejects `journey_incomplete` because Linux-required cases did not execute. Evidence: `/Users/mango/.cache/agent-fleet/verification/native-checkpoints-20261111/verified-summary.json`. Later [remote suspension](2026-11-11-native-remote-suspension.md) changes are outside this freeze.

- Initial red: `/tmp/native-checkpoint-red-20261111.xml`, 2 failures (missing durable API).
- First associated suite: 91 passed; late-cancel completion regression then failed (1 failed/94 passed), fixed in lease-fenced terminal writeback.
- OS process test uses `os._exit(73)` immediately after committed receipt, reopens the actual SQLite database and resumes on a fresh worker without repeating the write.
- Associated closure before final backup/delete regression: 273 passed/46.12s. Backup roundtrip succeeded; a test used the wrong conversation deletion API (1 failed/278 passed), corrected to archive then delete through the established API.
- Final 15-module closure before restore fencing: 279 passed/46.26s. Restore fencing follow-up pending; original 6903-test frozen full gate does not cover this slice. A new integrated full gate, Linux/Windows and live acceptance remain open.
- Checkpoint includes original limits, bounded private transcript and cumulative usage; same-row storage is covered by existing online SQLite backups and Run deletion. Old-backup restore fences incomplete checkpointed Runs unknown, as a backup may predate effects completed by the original instance. This additional regression first failed with state running. No public API exposes checkpoint contents.

- Final integrated 15-module result: 280 passed / 51.75s, `/tmp/native-checkpoint-integrated-20261111.xml`. Additional compatibility/package/retention regressions: 122 passed / 2 passing subtests / 10.34s; scopes may overlap and are not a full gate. Required matrix now 44 journeys / 1995 selectors.
- Restore fencing originally changed the copied database without rebuilding its integrity envelope (database_hash_mismatch); extended the existing envelope rebuild condition, then reran the whole closure. Persisted final completion now returns before reopening disabled workspace/provider configuration.
- Contract §5.1.1 now records private checkpoint phases, lease fencing, the 8 MiB limit, conservative unknown treatment for uncertain/legacy/corrupt state, and restore behavior. §5.1.2 records stable remote command observation, workspace reservation versus global worker lease, cancellation/claim fencing, terminal cleanup and capacity indexes. The development ledger, project canonical entry and MOC links were updated on 2026-11-11. The current integrated full Linux/browser release gate and exact production acceptance remain separate open tasks.
