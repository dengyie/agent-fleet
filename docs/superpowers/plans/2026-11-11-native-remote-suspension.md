# Native remote wait suspension

Goal: return the global worker slot while an original remote command is pending, without repeating dispatch, changing its deadline or losing workspace capacity.

Architecture: keep the existing SQLite Run and Command repositories. A private `remote_wait` checkpoint records the original response and stable `run_id:step:N`; `runtime_wait_command_id` retains workspace admission while the Run lease is released. Resumption observes the same command with a zero local wait. An injected synchronous waiter remains supported.

- [x] Persist remote waits and return the worker without sleeping; retain original command identity, deadlines, transcript and cumulative usage.
- [x] Preserve workspace capacity across suspend, resume claim and slot admission; another workspace can use the global slot.
- [x] Fence HTTP cancellation and Node claim in the same transaction. Also reject cancellation racing enqueue. Queued commands expire; dispatched uncertain commands remain unknown.
- [x] Observe pending/terminal receipts before reopening workspace, model or provider dependencies. Polling an unchanged wait produces no lifecycle events.
- [x] Preserve browser submit approval attribution on resumed receipts.
- [x] Release wait reservations on terminal transitions and restored backups; restored unfinished execution remains unknown.
- [x] Register regression selectors and run associated tests.
- [ ] Run the complete strict Linux gate and native Windows/two-node acceptance on this integrated version.
- [ ] Publish a reviewed candidate and run the real model/Node acceptance against its exact release SHA.

Evidence on 2026-11-11:

- Observation order: original case failed (`failed` instead of `waiting_node`); expanded matrix reproduced 3 failures / 2 passes, including unknown incorrectly changed to cancelled. Reports: `/tmp/native-remote-suspension-observe-red-confirmed-20261111.xml`, `/tmp/native-remote-suspension-observe-expanded-red-20261111.xml`.
- Terminal reservation: next Run could not claim the workspace after an administrative terminal transition; one failure in `/tmp/native-remote-suspension-terminal-capacity-red-20261111.xml`.
- Resumed browser receipt lost approval attribution; one failure in `/tmp/native-remote-suspension-attribution-red-20261111.xml`.
- Fresh associated verification: 212 passed / 10.53s across ten modules, including thirteen new suspension cases, actual SQLite/Node file execution, cancellation, runtime/checkpoint recovery, scheduling, remote receipt/deadline handling, backup and capability reservations. Report: `/tmp/native-remote-suspension-final-associated-20261111.xml`.
- Additional integration regression: 287 passed / 53.33s across ten separate Node HTTP/runtime/workspace/routes, native Workflow memory/text/recovery, two-node fixture, full-flow and heartbeat modules. Report: `/tmp/native-remote-suspension-integration-20261111.xml`. These controlled tests do not replace real remote devices or external model acceptance.
- Native Linux preflight: independent integrated freeze `4f4b6478b64720f29bf5a94e23b025f5b2e0add0` passed 240 tests / 2 subtests / zero skips across eleven process, sandbox, suspension, checkpoint, scheduler and index modules in 78.58s. All 1131 source digests were unchanged and checkout was clean. Environment: Debian 12 / Linux 6.1 / Python 3.11.2 / Node 22.23.4 / bubblewrap 0.6.1, in a private Singapore acceptance directory. Evidence: `/Users/mango/.cache/agent-fleet/verification/native-linux-capacity-20261111/native-preflight-4f4b6478/`. This is not the complete strict gate; its browser package is pending explicit large-download permission.
- The preceding immutable checkpoint freeze `06e8758615605d310591e9c2236b798f60d8a2b5` completed 6921 passed / 5 platform skips / 204 passing subtests, 43/44 journeys and 46 valid PNGs. It predates this suspension change; it is not its full gate. The original release verifier still rejects `journey_incomplete`.
- Integrated follow-up freeze `90374763513e1b7878544d4d4256cfcab93268ad` includes this slice. macOS full tree: 7201 passed / 11 Linux-only skips / 204 subtests; Linux full tree: 7162 passed / 50 skipped (48 browser module absent, 2 rsync later separately closed by 8 focused passes) / 0 failures, 28/44 journeys. Windows CI selection plus new regressions: 238 passed / 2 POSIX skips. The same-version release verifier still rejects `journey_incomplete`; the Linux browser requirement and reviewed candidate/live acceptance remain open. See the [current integrated deployment assessment](../../platform/development-completion.md#当前部署判断2026-11-11).
- Windows compatibility follow-up on the same source plus corrected test-only private-ACL fixture: checkpoints, remote suspension, capacity index and two-node HTTP handoff **62 passed / 0 skipped / 0 failures** in 43.01 seconds. The original attempt's 4 handoff failures came from passing an unprotected temp directory to the Windows receive adapter, which correctly refused to advertise `artifact.receive`. The test now creates and reopens current-user private DACL roots, matching the established native receive contract; product ACL enforcement is unchanged. Original and corrected JUnit are preserved under `.../windows/windows-native-regressions.xml` and `.../windows/windows-native-rerun2.xml` in the candidate evidence root. This is controlled single-machine integration, not two physical Nodes or the missing Linux browser gate.

Return to [development ledger](../../platform/development-completion.md). This slice does not complete the remaining stop/renew, archival, external integration or live acceptance requirements.
