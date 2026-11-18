# Session access consistency implementation plan

**Goal:** Web pages and the main assistant report and enforce the same current session authority.

**Architecture:** SessionService owns the public aggregation of captured session metadata, adoption state and enabled Supervisor actions. Captured `managed` is historical provenance, not live authorization. Adopted writes reuse AdoptionService's admission lock and signed command queue. UI consumes explicit state/actions rather than inventing `adopted`.

**Constraints:** Preserve revoked history, existing command receipts and unrelated worktrees. UTF-8 without BOM. Tests use isolated databases, a loopback Hub and synthetic processes; never control production sessions. No dependency or schema migration is needed.

## 1. Reproduce and unify live access

- [x] Add `tests/test_session_access_consistency.py`: revoked HTTP list/detail versus assistant; uncaptured adoption 404; revoked message/question writes; old/new session coexistence; Hermes follow-up rejection.
- [x] Run `python3 -m pytest -q tests/test_session_access_consistency.py`: six failures on `ade418f`.
- [x] In `hub/application/session_service.py`, aggregate actual adoption state and available actions, including adoption-only details. Batch adoption lookups in lists to avoid per-row I/O. Wire the existing adoption service in `hub/composition/control_plane.py`.
- [x] Make `hub/application/agent_session_service.py` reuse that projection; preserve per-call authorization and durable command reconciliation.
- [x] Route HTTP messages/questions through adopted admission rather than directly enqueuing; propagate operator identity. Include real state in UI contracts and render only declared actions.
- [x] Re-run the same six regressions, plus existing adoption, session, assistant and HTTP suites.

## 2. Verify the session UI lifecycle

- [x] Add a loopback browser regression for revoked status, missing actions, readoption detail and settings overlay while a control receipt is pending.
- [x] Fix the undefined callback on resume, and preserve pagination when refreshing events after a command has been admitted. Separate a failed read from a successful command submission.
- [x] Verify at desktop/mobile sizes with no page errors, no duplicate command and no false success status.

## 3. Validate and document

- [x] Update the existing testing matrix and testing guide with the new behavior selectors.
- [x] Run fresh scoped regressions, browser checks, Python lint and the full available suite; report platform skips separately.
- [x] Review the final diff and record evidence and remaining production verification boundaries. Commit only this branch's changes.

## Evidence boundary

The previous production UI observation is not proof of current adoption authority: `session.js` supplied a literal `status: 'adopted'`. This iteration must not claim real local sessions have regained control without authentic server/command evidence.

## Verification so far

- Backend and neighboring contracts: 298 passed, 3 subtests passed.
- Real Chromium and session contract suites: 57 passed; desktop/mobile assertions, transient receipt failures, more than 13 polls and late same-command responses covered.
- Additional delayed-read test first failed, then passed with immediate admission feedback; both delayed feedback and resume-in-flight scenarios passed after the final change (2 passed).
- UI overflow reproduced at 390px (496px document width), then fixed at the session heading.
- Existing adoption-only control endpoint remains for adoption-enabled/session-store-disabled installations; native managed sessions use the gated Supervisor session-control endpoint. Both enforce their owning service admission.
- First full run: 8175 passed, 75 platform skips, 5 failures. Corrected two stale test doubles, placed shared labels in the existing UI helper directory, and removed a concurrent counter mutation from the guardian test; 93 affected regressions passed. Final full run: 8182 passed, 204 subtests passed, 75 platform skips, zero failed (1312.92 seconds).
- These changes remove the assistant adapter's duplicate adoption projection and direct mutation path; no adoption records or user sessions are deleted or reset.

- Question-card receipt polling had the same early-stop defect at 30 reads; reproduced with a controlled timer and fixed with bounded polling frequency, pause/resume and stale-response isolation. The question + contract suite passed 31 tests.

## Final verification boundary

- Final browser lifecycle run: 9 passed (8 new scenarios plus existing question/browser integration). Question/contract suite: 31 passed, including the added controlled-timer lifecycle case.
- Ruff production rules (`E9,F63,F7,F82,F401`) and `git diff --check` passed; changed Python parses as 3.10; changed/new files are UTF-8 without BOM.
- Journey coverage is **44/47**. `NODE-01`, `RELEASE-01`, `WF-01` remain incomplete on macOS because their Linux process/isolation cases are skipped; the report therefore correctly marks the release gate failed despite zero pytest failures. This is not production release approval.
- No production deployment, live-session controls or adoption state resets were performed. Current work is isolated on `codex/session-adoption-consistency` from `origin/main` (`ade418f`).
- Final full report: `/tmp/agent-fleet-session-access-results-final.xml`; journey report: `/tmp/agent-fleet-session-access-journeys-final.json`. Temporary reports are local verification output, not CI release artifacts.
