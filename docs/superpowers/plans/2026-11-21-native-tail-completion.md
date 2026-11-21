# Native Tail Completion Follow-up

Execute inline with evidence-driven-bugfix and verification-before-completion.

## Production evidence and scope

PR104 deployed `7713457f8f7a38a8326dae507d02dbbbd4380579`; signed clients
`7713457f8f7a-s24` passed artifact/file/identity checks. The new Windows Pi
acceptance task `t-8fe9dae5736b` actually read README and confirmed cwd but
exhausted the 30-second post-exit tail budget while synchronously persisting
incremental JSON records. It terminated with `group_remaining`, exit124.
Original recovery `cmd_99d117c890331c3e80fa4c43e8b3f017` was correctly rejected
with `scope_mismatch`, yet capture had advertised `native_resume=True`.

Preserve that task, native transcript, rejected receipt, worktree, signed
rollback releases and evidence. No weakening terminal control gates, removing
durability or dropping source deltas. Use the existing isolated feature
worktree `codex/native-tail-completion`; preserve the primary checkout.

## Implementation and gates

- [x] Reproduce finite dense tail truncation using FakeOps/fake clock: 162 Pi
  records at 0.3 seconds persistence each must reach the final message/EOF.
- [x] Reproduce inaccurate failed-session delivery flags, worktree retention
  and stale local resumable flag after a failed follow-up. Initial regressions:
  six failed, nine passed; evidence retained in the private verification root.
- [x] Increase the finite total drain capacity to 120 seconds. Preserve the
  two-second inactivity deadline and bounded handling of infinite writers.
- [x] Separate retained native identity from delivery readiness; require clean
  `completed` terminal reason to advertise continuation. Retain failed native
  identity/worktree. A naturally completed nonzero CLI exit remains resumable.
- [x] Clear stale local delivery flags when a follow-up becomes nonresumable.
  Replay also revokes older clients' failed-completion claims using the
  original read-only manifest; an additional regression failed before the fix.
- [x] Fresh local lifecycle/capture/control/spool regressions: 327 passed,
  two platform skips, two subtests. Ruff and diff checks passed.
- [ ] Exact-head Windows/macOS/Linux CI, rebase and merge.
- [ ] Follow the exact main deployment, verify source/public assets/databases,
  upgrade signed clients preserving rollback, verify both running identities.
- [ ] Complete nine available production CLI FIRST/FOLLOW paths. Label the
  corrected Pi retest separately; finish Mac Claude's already-observed follow
  without sending it again. Windows Hermes remains absent and is not a pass.
- [ ] Verify original history/settings/defaults preservation, update canonical
  operations records on vault master, run doc-check, report actual boundaries.
