# Assistant session capability repair implementation plan

**Goal:** Make administrator session inspection and supported controls directly usable, expose real command evidence, and prevent process metadata from masquerading as a native conversation.

**Architecture:** Reuse SessionService for HTTP/assistant admission and SupervisorService for durable receipts. Node capability and verified native identity determine delivery support; cc-switch profiles never represent capture settings. Preserve explicit feature overrides and historical revoked identities.

**Global constraints:** UTF-8 without BOM; isolated feature branch; no unrelated work; no real-user message smoke tests; no private history publication.

## Tasks

- [x] Reproduce missing assistant tools and false ZCode delivery claims with tests in `tests/test_assistant_session_capabilities.py`; run `python3 -m pytest tests/test_assistant_session_capabilities.py -q` before implementation.
- [x] Expose session-scoped command history, audit logs and local profile discovery in `AgentSessionService`, `SupervisorService` and the existing repositories. Add `apply_local_profile` payload to `session.control`, retaining durable idempotency. Test scoped pagination, rejection reasons, authorization and payload conflicts.
- [x] Set supported follow-up/profile gates on by default for configured Supervisor installations; retain explicit off. Update prompt to use standing administrator delegation, query command evidence and distinguish capture from provider profiles.
- [x] Fix ZCode desktop process classification and native transcript discovery/normalization. Test explicit identity binding, duplicate request history, tool results and source redaction. Installed ZCode exposes a stdio app-server, not a connectable live desktop session transport. Do not enable native delivery solely from the presence of a CLI --resume flag; desktop message delivery remains unavailable pending that integration.
- [ ] Run relevant regressions, lint and CI. Rebase current public main, publish only this patch, verify exact release artifacts, deploy Hub/node updates and independently inspect release and command evidence.

## Evidence

Production command `cmd_3a78bf1b1108075d7ec80eddd680a4a9` for `adopt_6714a8ae492381bcd6e28d5076b25888` was rejected as `unsupported_action`. The adoption points to desktop PID 11037 with no native transcript, while the ZCode native store has multiple runtime sessions in a separate app-server process. Current `apply_local_profile` only updates cc-switch's current provider pointer. It cannot alter capture. Current bridge tails native files from their checkpoint (initial offset zero); adoption time does not itself prevent reading retained history.

Verification: initial 4 assistant capability tests failed before implementation; initial ZCode classification/discovery/normalization tests failed before implementation. Fixed-scope local regression: 361 passed and 5 subtests; independent Linux VM regression: 188 passed and 2 subtests. Full release checks and deployment are tracked by the publication evidence rather than presumed complete here.
