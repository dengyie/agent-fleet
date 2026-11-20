# Native Tail Drain Implementation Plan

> **For agentic workers:** Execute inline using evidence-driven-bugfix and verification-before-completion. Preserve the original failed task, command receipts and native transcript.

**Goal:** Preserve a finished Agent's remaining native output when capture callbacks consume the post-exit flush budget.

**Architecture:** Treat the existing two-second flush budget as an inactivity deadline, refreshed only by actual output progress. Retain a separate 30-second total drain cap so a noisy descendant cannot keep a completed leader's group alive indefinitely. Do not relax terminal control gates or relabel the failed live acceptance.

**Tech Stack:** Existing Python Supervisor, cross-platform GroupOps, Windows signed clients, pytest, GitHub CI and automatic deployment.

## Global Constraints

- Continue in the clean isolated worktree on `codex/native-tail-drain`, based on published main `377047ea4e5adc8d789852a0872055afdb6e1864`.
- Retain the failed fresh task `t-520d01961b93`, its original native ID `a70c1ae9-5c39-43b1-a6be-cff3c1e3dc4a`, rejected model command and provider-only unknown Run.
- Preserve rejection of operator-terminated, quarantined and unverified leftover groups. Do not repair the failure by rewriting its manifest or creating a replacement under its identity.
- Use UTF-8 without BOM. Preserve all native history, signed release rollback and unrelated worktrees.

## Failing Evidence

The Windows fresh Claude task read README and wrote `FLEET_MAIN_FRESH_CLAUDE_CODE_20261121_FIRST` to its exact native JSONL at 19:18:31Z. The runner returned exit124 with local manifest reason `group_remaining`; Hub events lacked the tail. The remaining model configuration was correctly rejected with `scope_mismatch`. Private process inspection found no process in the original task directory or using its native ID. Evidence is retained under `native-session-wait-20261121/fresh-claude-*.json`.

`Supervisor._finish_completed_locked` stops draining after two wall-clock seconds even when each pump delivers more output. Runner capture callbacks write encrypted, quota-checked spool records and consume that same budget. The new fake-clock regression must prove whether progressing output can be mistaken for a held-open pipe before changing the code.

## Task 1: Reproduce and Bound Progress-Aware Draining

**Files:**
- Create: `tests/test_native_tail_drain.py`
- Modify: `tools/supervisor/supervisor.py`
- Modify: `.github/workflows/ci.yml`

- [x] Use existing `FakeOps` and a fake Supervisor clock. Queue three final lines with a callback that advances time by 1.1 seconds per line. Require all lines, original exit code and `completed`, rather than `group_remaining`.
- [x] Verify the resulting same native identity can configure a model and append one follow-up, without touching real processes.
- [x] Cover a continuously writing held-open member reaching the total cap and a quiet member reaching the original inactivity cap. Keep cleanup/termination outcomes unchanged.
- [x] Run `python3 -m pytest tests/test_native_tail_drain.py -q` and preserve failures before implementation.
- [x] Add `_FLUSH_TOTAL_BUDGET = 30.0`; track `idle_deadline` and `total_deadline`, bound each pump by the remaining minimum, refresh only on `pr == "line"`, and keep EOF/abort/forced-teardown behavior unchanged.
- [x] Include this portable test file in Windows CI. Run the new regressions, Supervisor process/control and native model/continuation tests, Ruff and diff checks. Run the new tests on the physical Windows host as well.

## Task 2: Publish and Verify a New Regression Case

**Files:**
- Update this plan with actual regression/publication evidence.
- Update the existing canonical vault note and routes on vault primary `master` after acceptance.

- [ ] Commit, push and open a focused PR. Follow exact-head Linux/Windows CI, rebase onto latest main, merge and follow original automatic main deployment.
- [ ] Verify deployed manifest/source hashes/health/databases. Upgrade signed clients with a new sequence only after confirming no active owned children; preserve the previous releases and backups.
- [ ] Keep the first failed fresh Claude task untouched. Create a separately marked, post-fix regression task through the main assistant; verify automatic session binding, actual first tool output and exactly one follow-up on the same native ID.
- [ ] Recheck original Codex/Claude and fresh Codex evidence; compare the three pre-resume native-log prefixes byte for byte and confirm user defaults remain revision8.
- [ ] Record full CI counts, original failed attempts and final limits in PR descriptions, evidence and canonical operations records.

## Local Verification

The initial fake-clock suite failed all three cases. After the progress/inactivity fix, Supervisor/native/control regressions reported `206 passed, 2 skipped, 2 subtests passed in 14.42s`; the physical Windows host passed all three new regressions. Both strict group-remains failure and the original two-second quiet deadline remain covered.
