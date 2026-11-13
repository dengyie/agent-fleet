# Functional review fixes

Goal: repair session recovery, client update verification/recovery, and complete assistant event history; deploy through the existing main CI transaction.

Implementation order (each change gets failing evidence, a root fix, and fresh verification):

1. `tools/client_update/installer.py`: distinguish successful one-shot probe receipts from daemon liveness. Require a receipt produced after the pre-switch snapshot, including matching release sequence. Test an exited successful probe and reject a stale receipt.
2. `tools/client_update/installer.py`: journal switch intent before changing either link. Recover legacy `staged` transactions by comparing actual links too. Test interruptions immediately after each link change and before journal persistence; retain the original rollback target. Protect live runtime versions during release GC.
3. `frontend/assets/views/assistant.js`: follow event cursors until an empty page, preserve errors for retry, and stop stale rendering after disposal. Add real-browser acceptance for completed history beyond 100 events and the running-to-completed refresh.
4. Hub adoption service/routes and node supervisor/control client: on a fresh client startup, obtain signed recovery commands from the Hub's canonical active adoptions before processing regular controls. Revalidate live process identity, reuse recovered manifest/group identity, rebuild bridges/checkpoints, and retain retryable startup recovery across network failure. Existing clients remain compatible. Exercise real Hub/client/Supervisor restart, repeated poll, and revoked/changed identity cases.
5. Run focused regressions, complete pytest/browser/journey gate, lint and diff checks. Rebase onto current public main, publish a reviewable PR, merge after CI, wait for main deployment receipt, and verify public version plus authenticated functional smoke. Test clients with dedicated fixture processes and private state; never signal unrelated user sessions.

Constraints: use the isolated `codex/functional-review-fixes` branch, preserve other checkouts, UTF-8 without BOM, no manual deletion or large binary downloads. Production release and canary testing are authorized by the user's request.
