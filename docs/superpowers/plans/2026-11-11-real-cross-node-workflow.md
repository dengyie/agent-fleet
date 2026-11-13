# Real cross-node coding Workflow acceptance

Execute inline without delegation. Return to the [development ledger](../../platform/development-completion.md) and [acceptance matrix](../../testing/workflow-platform-acceptance.md).

**Goal:** Extend the actual CLI pilot through automatic capability selection, signed source reception and original remote fixed-test execution on the authorized SG Linux Node. Keep the original coding-delivery@1 contract and stop at awaiting human acceptance.

**Architecture:** Reuse `workflow_live_pilot.py`, `wire_workflows`, the existing `remote_test_probe.NativeNode` SSH controller and `remote_test_node_canary.py`. Register trusted test routing through the same production composition function. Never pre-seed developer/reviewer success or substitute a separate validator. Preserve the earlier single-host run and all failures.

**Scope:** macOS original Runner/real configured Codex and separate model reviewer → private Hub → actual Linux Node/bwrap fixed tests. This complements Windows→Linux handoff evidence; it does not certify Windows CLI execution, full WF-04/05 failure/capacity coverage, production deployment or complete release gates.

- [x] Add an explicit optional remote-test configuration to the manual pilot; retain its existing default behavior.
- [x] Assemble original capability/input/remote-test services with existing composition; pin exact offers, source hashes, signed commands and capacity disposition.
- [x] Freeze source independently and copy the 7.44 MB repository bundle to a new private authorized Linux checkout; preserve all prior checkouts. Use existing runtimes, no downloads/installations or global changes.
- [x] Execute and independently verify one actual CLI/reviewer/remote-test Workflow: one develop/review/test Run, identical subject digests, two signed Node commands, original journals, one test execution, exact HTTP artifacts and logout/login recovery.
- [x] Verify appropriate existing regressions, source integrity, UTF-8, Python3.10 syntax, lint and diff checks. Record failures and boundaries honestly.

No human acceptance or production cutover is performed by this probe. CLI usage and platform settlement remain separate evidence.

## Verified results — 2026-11-11

- Original `attempt-3`, freeze `0b3fd2fc9bb5297df6bc0ff4e5d019fa8881fc56`, 1162 source hashes identical on Mac and SG Linux before/after. Elapsed 109.891 s, including actual Codex 0.160.0 developer 45.364 s and independent same-model reviewer 42.160 s (`newapi/gpt-6-luna` / `magpie`, one completed turn and exit 0 each). No platform settlement claimed. This is one task, not a capacity benchmark.
- Original workflow `wf-_sLvGYjGPZHVuTXF0yJ2rcHx`, revision 23, reached execution `succeeded`, validation `passed`, acceptance `pending`. All three generation-1 steps shared subject `f4a64b2f32af7f8872ece6f5360043bfe7b0fdc758932ad1ed7a7b4ca656f41c`. Actual SG Linux Node ran the registered bwrap test recipe once: 31 JUnit cases, no failures/errors/skips. One signed source receive and one signed test command succeeded. Source bundle, receive receipt, and Node file SHA-256 agree; the remote report/JUnit and published artifacts match byte-for-byte. Three original Runs succeeded; all three step reservations were released.
- Original harness exited 1 in final verification: it incorrectly expected one total reservation. The database correctly contained one released reservation per step. The original failure report is preserved. The independent audit on the original database/files verified the workflow, both signatures, source/receipt and report bytes, original CLI records, 5 downloaded artifacts, SQLite integrity, and durable Node journal. An expired replay at actual time was rejected; twice replaying at the explicitly controlled original timestamp reused the saved result with zero executions/preflights/HTTP/downloads. No model or test was rerun to obtain these results. The corrected count check is in the current manual helper, after the freeze.
- Fresh login restored the same workflow; the existing Chrome 156.0.8078.80 independently restored it at desktop and 390px mobile widths and downloaded all 5 matching artifacts. No page errors, workflow mutations or human acceptance. The UI script takes a separate verified result path so it never replaces the original failed report.
- Earlier attempts retained: attempt 1 omitted the dispatch flag and made no model calls; attempt 2 completed real developer/reviewer and source reception, then the manually constructed worker rejected remote execution. Attempt 3 uses the configured original worker. Associated existing regression suites: 35 passed, zero failures or skips.
- Evidence root `/Users/mango/.cache/agent-fleet/verification/real-cross-node-20261111/`. Original `attempt-3/result.json` stays failed, independent `independent-audit-v2/verified-run.json` verifies the business run, and `ui-attempt-1/ui-result.json` records read-only recovery. Share only audited summaries/outputs; private runtimes and original evidence remain private. Product files match the freeze; subsequent changes are to verification helpers and records.

Still open: human acceptance, wider WF-04/05 failure/competition matrix on final source, complete Linux/browser release gate, production configuration, external integrations and overall publication.
