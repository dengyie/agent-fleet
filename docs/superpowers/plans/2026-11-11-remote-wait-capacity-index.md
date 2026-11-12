# Remote Wait Capacity Index Implementation Plan

> **For agentic workers:** Execute this task inline with evidence-driven-bugfix and verification-before-completion. Preserve the existing feature worktree and immutable Linux acceptance freezes.

**Goal:** Keep remote wait admission work proportional to active wait reservations instead of completed Run history.

**Architecture:** Add a partial SQLite index to the existing additive platform initialization, after `runtime_wait_command_id` is present. Keep the shared capacity queries and their ownership/capacity semantics. The index contains only non-null wait reservations, keyed by owner, so candidate admission seeks one owner's active waits and slot admission scans only active reservations.

**Tech Stack:** Python 3.10+, SQLite WAL, pytest; no new dependencies.

## Task 1: Bound admission work with real SQLite evidence

**Files:** `tests/test_remote_wait_capacity.py`, `hub/infrastructure/platform_db.py`, `docs/testing/journeys.json`, `docs/platform/development-completion.md`.

**Interfaces:** Existing `PlatformRepository.init()`, `claim_run(..., max_workspace_concurrency=1)` and `claim_worker_slot(..., max_workspace_concurrency=1)`. No new API or state.

- [x] Create one actual suspended remote command, 4000 valid terminal Runs with their own messages, and a queued Run in a different workspace. Count SQLite VM instructions through `set_progress_handler` on the real repository connections, with the claim clock frozen before the next remote observation is due. Require correct capacity decisions and fewer than 5000 instructions.
- [x] Run `PYTHONPATH=. python3 -m pytest tests/test_remote_wait_capacity.py -q`. Valid red evidence: `/tmp/native-wait-capacity-index-red-valid-20261111.xml`, two failures, 20000 slot instructions and 80300 candidate instructions. An earlier test accidentally allowed the waiting Run's next observation to become due; that fixture failure is retained separately.
- [x] Add the following initialization statement after the nullable wait column migration:

```python
conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_native_wait_owner "
             "ON runs(owner_id) WHERE runtime_wait_command_id IS NOT NULL")
```

- [x] Rerun the two real workload cases, then native suspension, scheduler, worker, backup and existing data-index regressions. Record actual instruction counts and results.
- [x] Register the new selector in RUN-02 and update the existing development ledger. Preserve the original full-gate reports; this new index requires a new integrated freeze.
- [x] Update canonical project entries when the vault primary worktree has no unrelated dirty changes.

Return to [development ledger](../../platform/development-completion.md) and [remote suspension plan](2026-11-11-native-remote-suspension.md).

## Task 2: Complete the bounded conversation lookup

After the partial index, the slot case passed but candidate admission still executed 48300 instructions. Statement-level tracing proved that the wait-capacity query was bounded; each of the two message-to-Run joins spent 24000 instructions scanning the owner's 4000 unrelated terminal Runs. Both joins use `(owner_id, trigger_message_id)`, which had no matching index.

- [x] Retain the remaining red and actual SQL costs in `/tmp/native-wait-capacity-index-partial-red-20261111.xml`.
- [x] Add `CREATE INDEX IF NOT EXISTS idx_runs_owner_trigger ON runs(owner_id, trigger_message_id)` next to the existing owner/conversation index. Keep both indexes because their access paths differ.
- [x] Rerun the same workload and associated history, scheduler, checkpoint, backup and index regressions. Refresh the older owner/state query-plan test to include the newly supported `waiting_node` state and its `runtime_resume_at` predicate; preserve its index requirement.

Fresh verification: nine associated modules passed 190 cases with actual Chrome history acceptance and zero skips. Report: `/Users/mango/.cache/agent-fleet/verification/deployment-readiness-20261111/associated-results.xml`. A separate repeat of the workload passed both cases; the SQLite progress handler, sampled every 100 instructions, recorded progress counts of 0 for slot admission and 400 for candidate admission, against the unchanged 5000-instruction ceiling. Report: `/Users/mango/.cache/agent-fleet/verification/deployment-readiness-20261111/capacity-results.xml`. These results are associated regressions, not a complete Linux release gate.

Canonical recording: execution contract §5.1.2 documents the active wait reservation and owner/message indexes; development ledger and existing Obsidian canonical project entry, MOC route and project index now point to current deployment evidence. The integrated candidate still lacks the complete Linux/browser gate.
