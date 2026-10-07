# Development Completion Implementation Plan

> Execute inline in the existing isolated worktree, using evidence-driven regression tests. This is a continuing completion audit, not a declaration that all repository development is finished.

**Goal:** Finish all development tasks in the repository's current specifications, including their required failure paths and verification; preserve unresolved roadmap work until its actual acceptance criteria are satisfied.

**Architecture:** Preserve the existing Flask/SQLite control plane, signed outbound Node transport, native ES-module frontend, and pure domain contracts. Close correctness gaps in those boundaries before adding the documented browser and operations capabilities. External I/O receives explicit bounded contracts; internal orchestration receives no extra facade or framework.

**Tech Stack:** Python 3.10+, Flask, SQLite, cryptography, vanilla JavaScript, pytest and installed Playwright/Chrome. Runtime dependencies are `requirements.txt`; test dependencies are `requirements-test.txt`. No static type checker or frontend build framework is currently configured.

## Scope and source precedence

The owner's 2026-10-06 goal says **all development tasks**, not only browser submit. The audit began from integrated baseline `fe121de` and the locally verified result is merged into `main` at `3a76ce2`. The primary checkout remains protected and unchanged; historical evidence below retains the worktree in which it was produced.

Current requirements come from `docs/platform/agent-fleet-self-hosted-platform.md`, the applicable specifications/plans below, and the testing contracts. A completed checkbox alone is not acceptance evidence. An unchecked historical checkbox alone is not a new implementation task: the September 9 v4 completion plan expressly forbids redoing August Phase 1–4 from their stale checkboxes, and the September 1 convergence/recovery plans explicitly mark themselves historical/implemented. Those original records remain intact.

| Requirement stream | Authoritative sources | Current evidence and remaining work |
|---|---|---|
| Push-only observation, task/runner, session supervision, discovery/adoption | `docs/architecture-v3.md`, `docs/architecture-v4-control-plane.md`; August specs/plans; `2026-09-09-v4-initial-goal-completion-design.md` and matching plan | Implementations and tests exist; audit current code against replacement contracts, including pause/continue/confirm, patch/tests/files, credentials and source-control isolation. Historical deployment statements are not current runtime proof. |
| Thin client and native session continuation | `2026-09-07-thin-client-remote-control-design.md`, `docs/session-conversations.md` | Audit identity-bound resume, fixed actions, account separation, capture scopes and UI/receipt state. Never substitute arbitrary stdin/shell for continuation. |
| Assistant, durable scheduling, providers, remote delivery and reconciliation | Platform document; M1 plans from September 26–27; October 5 request metadata/contract/transport plans | Atomic Node admission/no-replay has crash and concurrency evidence in Task 1. Continue auditing source/version fencing, request metadata and operator recovery against current replacement contracts. |
| Service catalog, health, Komari, HTTP probes, budgets and backup | September 25 M3 and September 28–29 M2 plans; platform document | Local contracts exist. Audit source/version fencing, recovery and bounded collectors. Real Komari schema/export, test-node pilot, off-host durability and key escrow are not established by local fixtures. |
| Schedules and memory | September 29 M4.1–M4.4 plans; platform document | CRUD, explicit selection and scheduling exist. Audit missed/overlap handling, revision fences and UI recovery; do not infer automatic memory extraction or notification delivery from these implementations. |
| Accounts and frontend | `docs/account-management.md`, October 2 account plan, September 29 frontend plan, October 5 PromptQL/Windhub plan | Account/browser/permission journeys exist. Audit against current asset layout; actual sender configuration, migration and live account activation require separate runtime evidence. |
| Artifacts, task bridge, inbox, preview and execution windows | September 29 T1.5–T1.8 and T3.4–T3.5 plans | Implementations exist despite stale T1.5 checkboxes. Audit real browser success/failure and scope assertions, not source strings alone. |
| Browser T3.6/T3.7 and submit | October 4 browser plan/network spec, October 5 submit spec and October 6 submit plan | Default-off offline contracts are merged into local `main` at `3a76ce2`; numeric and escaped-string accounting have adversarial coverage in Task 2. This continuation adds bounded frame artifact events/rendering and admission fencing for operator writer leases. Real browser lifecycle/egress enforcement, crash cleanup, observation policy, Browserbase and credential integration remain incomplete. |
| Operations maturity / notifications | Platform M4 roadmap | Memory, backup and rate/budget slices exist. Notification triggers, channel selection and delivery policy have no executable specification yet. Keep this stream open; do not invent completion from existing events. |
| Packaging, CI and production acceptance | `docs/testing/README.md`, `journeys.json`, `test-chains.md`, `release-acceptance.md`; October 3–5 test/deployment plans | Local baseline has 29 journeys. Must verify changed behavior, Python 3.10 syntax, frontend syntax if changed, committed packaging, Linux-only requirements and independently applicable external acceptance. No local test count proves a deployment or external integration. |

The real-browser/notification specification decision has been requested from the owner. These streams remain in the full objective while independent, already-specified work proceeds. Missing implementation and missing evidence must remain visible; they cannot be marked complete by narrowing the goal to existing fixtures.

The reviewable [browser and notification decision proposal](../../testing/browser-notification-proposal.md) now names the remaining runtime, observation, takeover, credential, Browserbase and delivery decisions with required evidence. It is a draft, not approved implementation authority. The current host is Darwin and has no Docker/Podman/bubblewrap executable on PATH, so Linux process-level egress enforcement cannot be established in this local environment.

## Fixed invariants

- Protect both the primary dirty checkout and the main worktree; before integration rebase the feature branch onto current main.
- Commands cross the model boundary once. An interrupted or concurrently redelivered command cannot run again; terminal journal outcomes cannot be overwritten by a late completion.
- Browser limits remain 8 sessions, 16 KiB structured results, 256 KiB PNG, depth 8, 256 items per container and 2048 value/key nodes. JSON values must be serializable and preserve ID precision; large integer IDs must be strings, never truncated numbers.
- Keep the T3.7 network limits and the submit approval/POST limits unchanged. No production capability is silently enabled by new code.
- Use indexed point admission in SQLite with a short transaction, then release the transaction before the executor/network boundary. No global executor lock, new queue, blanket retry or duplicate transaction store.
- Traverse a browser result once with bounded depth/nodes, charge serialized bytes accurately, and fail before persistence/transport; do not serialize an unbounded tree merely to discover its size.
- Preserve typed error causes; all public errors and logs retain their existing secret-free policy.

## Task 1: Atomic Node execution admission and crash redelivery

Files: `tools/platform/journal.py`, `tools/platform/node_client.py`, new `tests/test_platform_journal_admission.py`; existing `tests/test_platform_delivery.py` and Node/submit integration suites.

Contract: `NodeJournal.begin(command_id, *, now)` atomically identifies the caller that inserted the running record. The Node executes only that admission. An existing running record is indeterminate and becomes durable `unknown`; completed records retain their result. `finish` updates only a running record and returns the authoritative persisted outcome so a late execution cannot turn unknown into success. All database connections close deterministically.

- [x] Reproduce restart with a pre-existing running row and assert zero executor calls.
- [x] Block the first executor with a test Event, deliver the same signed command through another client sharing the DB, and assert exactly one side effect and immutable unknown after the original returns. Use bounded waits and always release/join test workers.
- [x] Exercise direct concurrent journal admissions with independent connections and assert a single admitted writer; test terminal immutability and independent command IDs.
- [x] Implement atomic admission and result fencing; verify existing completion-idempotency, signature, HTTP receipt, browser approval attribution and crash recovery contracts.
- [x] Add the selectors to NODE-01 and record fresh evidence before committing.

## Task 2: Bounded, JSON-safe browser outcomes

Files: `tools/platform/browser_backend.py`, `tests/test_platform_browser_policy_backend.py`, browser/Node integration tests and the BROWSER-01/BROWSER-02 matrix.

Contract: finite safe JSON scalars, bounded UTF-8 strings including their JSON escaping, exact punctuation accounting and existing depth/item/node limits. Unsafe integer IDs require string representation. Reject malformed Unicode and unsupported binary structured results with bounded typed errors; PNG retains the dedicated binary artifact path.

- [x] Reproduce NaN/Infinity, unsafe integer, malformed Unicode and escaped-string aggregate overflows before changing validation.
- [x] Add exact-boundary success cases, bounded nested containers and valid large-ID strings; assert failed driver outcomes never reach journal or HTTP as successful invalid JSON.
- [x] Implement a single bounded traversal without a whole-tree serialization allocation; preserve screenshot/artifact behavior and original exception causes.
- [x] Run backend, signed Node HTTP, submit and artifact regressions, then update the contractual byte-counting evidence.

## Task 3: Finish the remaining specification and implementation audit

- [x] Read each current replacement specification and map its named artifacts/acceptance cases to current code and executable evidence. See [`current-spec-audit.md`](../../testing/current-spec-audit.md); external and conditional items remain explicitly open there.
- [x] Close any further reproducible gaps with one attributable failing test and repair cycle per root cause. The current clean gate and audit matrix contain no newly reproducible local gap; any remaining item is an external boundary or an unapproved product decision.
- [ ] Resolve and write the real-browser/observation/takeover/notification contracts without silently removing their roadmap requirements; implement every approved contract and test its true runtime boundary.
- [ ] Verify the production-environment requirements only against their actual configured environment. Record unavailable prerequisites precisely; never substitute fixture success for external proof.

## Task 4: Completion gate

- [ ] Every explicit current development requirement has implementation plus matching current evidence; every remaining item is still open until satisfied or explicitly removed by the owner.
- [x] Run the full test journey gate with browser prerequisites; run Python 3.10 syntax/compile checks and changed frontend syntax checks; verify packaging from the final committed revision.
- [x] Rebase before the authorized local main integration and preserve the original dirty worktree. Push/deployment is not inferred from this development goal.
- [ ] Only then complete the continuing goal. Partial progress, a green subset, historical checkmarks or this plan itself are insufficient.

## Verified checkpoint: journal admission and browser outcomes (2026-10-06)

- Before repair: seven journal replay/terminal-state cases and fourteen browser JSON-boundary cases failed. Additional tests exposed three invalid late-result serialization cases, two malformed signed-submit cases and three invalid screenshot-type cases; each was repaired and rerun.
- Full current checkout: **2663 passed, 2 skipped, 154 subtests passed**, 29/29 journeys, in 329.82 seconds. The two skips require Linux `/proc`; the existing `tools/probe/discovery.py` docstring warning is unrelated. External checks remain `not_run`.
- Python 3.10 AST parse and compile checks passed for all five changed/new Python files; `git diff --check` passed.
- Evidence: `/tmp/agent-fleet-completion-evidence/results.xml`, `/tmp/agent-fleet-completion-evidence/journeys.json`, and `/tmp/agent-fleet-completion-evidence/browser/`.
- Command: `FLEET_PLAYWRIGHT_MODULE=/tmp/pr6-review-browser/node_modules/playwright FLEET_BROWSER_CHANNEL=chrome FLEET_SCREENSHOTS=/tmp/agent-fleet-completion-evidence/browser PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /Users/mango/project/codex/agent-fleet/.venv/bin/python -m pytest tests -q --tb=short -rs -p no:cacheprovider --require-journeys --journey-report=/tmp/agent-fleet-completion-evidence/journeys.json --junitxml=/tmp/agent-fleet-completion-evidence/results.xml`.

This verifies Tasks 1 and 2 only. The full requirement audit, real-browser/notification contracts and external acceptance remain open.

## Audit repair A: Durable schedule lease lifecycle

Source: `2026-09-29-m4.1-durable-schedules.md` Task 2 and platform M4.1 crash/reclaim contract. A deterministic disposable SQLite repro showed that a `running` trigger with an expired lease is skipped forever, while an executor completing after its lease can persist success using its start time. These are service orchestration gaps; `claim_trigger` and `finish_trigger` already provide atomic lease fencing.

Files: `hub/application/platform_schedule_service.py`, `hub/infrastructure/platform_schedule_repository.py`, new `tests/test_platform_schedule_recovery.py`, SCHEDULE-01 selectors and verification docs.

- [x] Reproduce expired-trigger recovery under both missed policies, live duplicate ticks, late success/failure and completion timestamps with an injected clock.
- [x] Preserve the current occurrence while its lease is active; reclaim expired occurrences through the existing atomic claim method. Apply missed policy only to occurrences that never started.
- [x] Read completion time after executor return, write with the current lease, and return bounded lease-mismatch evidence without attempting a second finish under a lost lease. Preserve newer workers' results.
- [x] Re-run the original schedule suite and restart/lease-recovery cases; indexed trigger lookups remain O(log n), with no new queue, thread pool or global lock.
- [x] Run the full journey and committed-package verification after all audit repairs.

The lifecycle repair also exposed a previously ignored `advance_to` argument and starvation behind the first 100 catalog entries. New failing tests cover both. Atomic advancement now preserves an independently discoverable expired trigger; indexed, bounded pending selection batches due definitions and expired occurrences in one query. Full-journey rerun remains pending after this audit batch.

Fresh focused validation: 89 passed, 2 subtests passed, including all 13 schedule recovery cases and existing schedule, memory, scheduler, config and packaging contracts. Python 3.10 AST, compileall and whitespace checks pass. Packaged startup at this point covers committed `c61da3f`; repeat it after committing the schedule repair.

## Audit repair B: Schedule and MemoryItem JSON validation

Sources: M4.1 fixed-field operator API and M4.2 bounded fields/optimistic revision contracts. New real Flask/SQLite regressions produced 26 failures: unhashable enum fields and malformed Unicode escape validation; invalid numeric conversion and missing status mapping become 500/503; non-boolean enabled values are accepted; boolean/fractional revisions are converted to the current integer and mutate state.

Files: existing schedule/memory repositories, their HTTP routes, new `tests/test_platform_schedule_memory_validation.py` and matrix/documentation. No dependency, schema or service abstraction is needed. Scalar type checks are O(1); UTF-8 checks run only after existing field length bounds.

- [x] Reject non-boolean enablement and non-string enum values before conversion/membership checks.
- [x] Preserve Unicode/overflow error causes in the existing typed errors and return stable client-error status codes.
- [x] Permit integer JSON revisions and existing quoted/unquoted integer If-Match forms; reject boolean/fractional/non-finite JSON revisions without mutating rows.
- [x] Re-run malformed-input failures plus existing schedule CRUD, memory CRUD/search/context and revision-fence tests.
- [x] Run the full journey and committed-package checks after all audit repairs.

Validation: 26 initial malformed-input failures plus 8 additional falsey-default/error-chain failures were reproduced. Fresh focused schedule/memory/context/conversation/config verification: **91 passed**. The 36 new HTTP/error-chain cases include 34 repaired failures and 2 positive compatibility cases. Full-journey and final committed-package verification remain pending.

## Audit repair C: v4 patch byte budget and Hub redaction

Source: September 9 v4 completion design §3–7 (redacted patch at most 100 KiB, bounded operator retrieval). Seven new tests reproduce marker overflow, multibyte storage/read overflow, raw runner credential text persisted as a patch, and malformed patch types/Unicode mapped to success or 503. The HTTP byte-overflow repro sends actual UTF-8 JSON, so it passes the existing whole-request size gate without weakening it.

Files: new pure `hub/domain/task_result.py` byte-bound function; existing runner redactor, task repository/domain projection, runner ingress service and operator diff read service; new `tests/test_task_patch_bounds.py`. One shared scalar contract replaces inconsistent character slicing. Redaction remains the existing redactor. Byte truncation allocates at most a bounded prefix, O(min(input characters, budget)); no new dependency or I/O interface.

- [x] Keep the truncation marker within the 102400-byte budget and preserve valid UTF-8 and repeated-processing stability.
- [x] Revalidate/redact the authenticated runner's patch before persistence; reject non-text/invalid Unicode with `invalid_diff_patch` and preserved error cause.
- [x] Enforce byte bounds at storage and read/projection boundaries, including historical multibyte rows, and preserve the visible truncated flag.
- [x] Re-run patch/runner/task/domain/frontend/bridge regressions; current focused evidence is recorded below.
- [x] Run the final full journey and committed-package checks after all audit repairs.

The broader v4 audit also identified an unbounded test-summary file read and separate scalar/JSON-bound normalization concerns; these are addressed in repair D below.


Full verification of committed `6aee71f` (before patch changes): **2712 passed, 2 Linux-only skips, 154 subtests**, 29/29 journeys, 328.07 seconds. Evidence: `/tmp/agent-fleet-completion-evidence/results-final.xml` and `journeys-final.json`. This closes the full-suite gate for repairs A and B; it does not include repair C.

Repair C focused evidence: 235 task/runner/store/service tests plus 162 frontend/bridge/release tests and 2 subtests passed. Python compile and whitespace checks passed. The seven new patch cases fail on the previous implementation and pass after repair.

## Audit repair D: Bounded adapter test-summary collection

Source: v4 completion design §3/§6 (test summary at most 20 KiB; optional adapter result file) and task-result collector's existing symlink/non-UTF8 contract. Four new failing cases show that oversized files are fully read and accepted, while malformed present JSON/UTF-8/non-object summaries are silently treated as absent.

Files: `tools/result_files.py`, shared byte constant/error in `hub/domain/task_result.py`, new `tests/test_task_summary_collection.py`, matrix/docs. Bound source bytes to 20480 before parsing; verify the opened descriptor matches the initially observed regular file; use no-follow/nonblocking flags where available and verify identity on every platform. The read is O(min(file size, 20481)), with deterministic descriptor closure. Missing files and intentionally skipped symlinks remain optional; malformed present files produce a bounded typed error, allowing the existing runner failure path to report collection failure rather than claim missing test evidence.

- [x] Preserve missing/alternate-name behavior, exact byte limit and ordinary allowlisted JSON.
- [x] Reject over-limit, malformed present files and changed descriptor identity; retain original I/O/JSON/Unicode causes.
- [x] Verify bounded growth/race paths and closed descriptors, then runner/task regressions.

The summary audit reproduced 20 additional contract failures: negative/fractional/boolean/non-finite counts, non-finite/negative/overflowing durations, non-text/invalid-Unicode failed names, unsafe public projection, and JSON cut in the middle of an escape before persistence. The repair shares one pure normalizer between runner, repository and public projection. Present known fields must have their documented types; missing summaries and unknown keys retain their existing semantics. Failed names remain at most 20 entries/200 characters, additionally admitted under the serialized 20 KiB budget. Persist complete valid JSON only, never a sliced JSON string. Runner HTTP translates invalid evidence into a bounded client error before storage. These scalar and collection cases are verified together because both guard the same v4 result boundary.

Fresh verification in this continuation: 102 summary/patch/file/store tests passed. The later runner/workspace regression passed 167 tests. Full-journey checkpoints are recorded below; the numeric-string schedule changes and additional cause assertions remain uncommitted pending ownership confirmation.

## Audit repair E: Streaming Git result collection

Source: v4 completion design §3/§6 and the completion objective's bounded large-output requirement. Three failing real-Git tests showed 25,206,456 bytes of parent Python allocation for an 8 MiB source patch and 135-byte results under a 120-byte patch/stat budget. The collector previously captured the entire output before truncating it.

Files: `tools/worktree.py`, `tests/test_worktree_diff_streaming.py`, SESSION-01 and testing documentation. Drain nonblocking stdout and stderr under one absolute monotonic deadline, retain only the bounded prefix, and check the final exit status. Kill/reap the owned process group on interruption or timeout; preserve timeout/OS causes. Reuse the shared UTF-8 truncation contract, including its marker budget. Work is O(total emitted bytes), with O(output limit + fixed chunk) Python memory; no new dependency, service layer or background thread.

- [x] Reproduce unbounded memory and marker overflow using actual Git, then re-run the same regressions.
- [x] Add simultaneous large stdout/stderr, invalid UTF-8, timeout with open/closed pipes and process-reaping cases.
- [x] Complete runner regressions and the full journey/package gate on the final revision.

At the time of this October 6 note, concurrent edits to schedule numeric-string validation and summary cause assertions had not been attributed. The numeric-string validation was subsequently committed as `cf7dfcf4ed2d6c4902ef307142c444d1a01b08bd`; current `_finite()` rejects JSON strings and `test_platform_schedule_memory_validation.py` covers numeric-string rejection. The historical ownership note does not describe the current worktree.

Checkpoint before repair F: **2767 passed, 2 Linux-only skips, 154 subtests**, 29/29 journeys, in 334.19 seconds. Evidence: `/tmp/agent-fleet-completion-evidence/results-summary.xml`, `journeys-summary.json`, `browser-summary/`. This verifies the summary and initial Git collection changes in the dirty development checkout; packaging tests still read committed `eb8d8c1`, so it is not final-revision package evidence.

## Audit repair F: Workspace file and process output bounds

Source: platform M1 bounded workspace/runtime contract and the completion objective's streaming/lifecycle constraints. Three failing regressions use an 8 MiB file or actual child output: `DirectoryBackend.read` allocated 8,520,869 bytes and directory/sandbox execution allocated over 16 MiB before returning just 64 KiB. Both runtime backends inherited capture-before-truncate behavior.

Files: new `tools/bounded_process.py`, existing `tools/worktree.py`, `tools/platform/backends/directory.py`, `tools/platform/backends/sandbox.py`, new `tests/test_workspace_output_bounds.py` and matrix/docs. Reuse one external-process boundary for all three consumers; nonblocking dual-pipe draining retains bounded prefixes, one absolute deadline and process-group cleanup. Move session creation to `Popen(start_new_session=True)` while preserving sandbox resource limits and the required administrator launcher. File reads request only limit + 1 bytes. UTF-8 replacement output is bounded again before public receipts.

- [x] Reproduce the three capture-before-truncate memory failures with actual files/processes.
- [x] Verify exact limits, excess output, nonzero exit/stderr, invalid UTF-8, timeout/reaping and default-off sandbox behavior (167 focused tests passed).
- [x] Run the full journey gate, Python 3.10 syntax/compile checks and committed-package startup after the final changes.

This change bounds parent memory and child lifetime; it does not provide a production sandbox launcher, change gates, execute remote commands or establish external acceptance.

## Clean verification checkout and strict JSON follow-up

Owned repairs were committed at `c245003` and copied through the native worktree tool to `codex/development-verification`. The earlier worktree remains intact with unconfirmed concurrent edits. The clean committed gate produced **2777 passed, 1 failed, 2 Linux-only skips, 154 subtests**, 28/29 journeys, in 342.22 seconds (`results-committed.xml`, `journeys-committed.json`). Packaging/startup tests passed for `c245003`, but this run is not a passing release gate.

The failure is actionable: a present report containing `{"unknown":NaN}` bypassed the documented valid-JSON contract because Python accepts nonstandard constants before unknown fields are discarded. The collector now uses the shared strict string normalizer; `parse_constant` rejects NaN/Infinity before projection, while malformed UTF-8 preserves its decode cause. Missing files remain optional and empty present files remain invalid. Additional parameterized evidence covers NaN, Infinity and -Infinity in unknown fields. This repair is made and verified in the clean owned checkout, independently of the preserved concurrent changes.

## Verified local integration checkpoint (2026-10-06)

The clean `2f8119347592d61edffee4c935cb3d4b0aed1ba6` revision passed **2781 tests, 154 subtests and 29/29 journeys** in 352.29 seconds. Both extracted-release startup variants (`platform_enabled=False/True`) passed. The two skips require Linux `/proc`; the existing discovery docstring escape warning remains unrelated. All 28 changed Python files since `fe121de` passed the Python 3.10 AST check, and compilation/whitespace checks passed.

Evidence is under `/tmp/agent-fleet-completion-evidence/`: `results-verified.xml`, `journeys-verified.json`, `verified-full.log`, and `browser-verified/`. The report records `working_tree_dirty=false`, `ci_status=passed`, and matrix SHA-256 `c995e4bd774de5175a910e8433487792538adb3cee15534d0384401cd66a5174`. MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE remain `not_run`.

After fetching `origin/main` and confirming local main contains it, `codex/development-verification` was rebased onto main (already up to date) and fast-forwarded into the clean local main worktree. No push or deployment occurred. The original primary worktree and the prior development checkout retain their unrelated/unconfirmed changes. This checkpoint closes the local full-suite/package gates for repairs A–F and the strict-JSON follow-up; it does **not** close Task 3's whole-spec audit, the browser/notification decisions, Linux-only evidence or external acceptance. The continuing goal remains active.

## Audit repair G: Execution-window input failures

Sources: platform security/reliability gates, T3.4 bounded event/cursor and writer-lease contract, and the completion objective's safe JSON integers and preserved exception causes. The unchanged execution-window/frontend baseline passes 17 tests. Real Flask requests reproduce four unhandled 500 paths (oversized cursor, overflowing TTL, malformed Unicode event text and malformed Unicode credential); empty non-object payloads are also incorrectly accepted. The new regression set has 17 failures and 3 passing controls before implementation, recorded in `/tmp/agent-fleet-completion-evidence/window-input-red.log`.

Root cause: scalar conversion/UTF-8 failures escape repository validation, SQLite receives an unbounded cursor, and `payload or {}` discards malformed false-valued input. Application translation then discards the repository cause. Repair the existing repository validators and application exception translation; no runtime, schema, gate or protocol expansion is required.

Files: `hub/infrastructure/execution_window_repository.py`, `hub/application/execution_window_service.py`, existing `tests/test_platform_execution_windows.py`, WINDOW-01 matrix and this plan. Preserve all current TTL/byte limits. The numeric event cursor must fit `0..9007199254740991` so its JSON round trip through the existing JavaScript client remains exact. Input validation is bounded by request/scalar size; event reads retain the existing owner/window/sequence index and fixed page limit.

- [x] Reproduce with authenticated Flask requests, checking both response code and absence of event writes; verify the existing lease remains usable after rejected input.
- [x] Catch TTL overflow as its existing `invalid_*_ttl` error, UTF-8 errors as `invalid_event_payload`/`invalid_ticket`/`invalid_lease`, and preserve the original cause.
- [x] Reject out-of-range event cursors before SQL and distinguish omitted payload from an explicitly malformed false-valued payload.
- [x] Re-run the failing cases and existing execution-window/frontend/conversation tests, then the committed journey and package gates (see checkpoints below).

Run focused checks with `PYTHONPATH=. /Users/mango/project/codex/agent-fleet/.venv/bin/python -m pytest -q tests/test_platform_execution_windows.py tests/test_frontend_execution_window_contracts.py tests/test_platform_conversation.py`. Real browser, notification and external acceptance requirements remain open.

Fresh focused verification: **45 passed** in 1.19 seconds, including the same 17 previously failing cases and 3 added controls. All three changed Python files parse with Python 3.10 grammar and compile successfully; whitespace checks pass. WINDOW-01 now includes the six new parameterized test selectors. Full committed-revision verification remains pending.

Committed `64e9d26f1dedb901b163dd4004f333ae2a789521` subsequently passed **2801 tests, 154 subtests and 29/29 journeys** in 342.00 seconds, with only the two existing Linux-only skips. The report records a clean working tree, `ci_status=passed`, and matrix digest `3ce25759a50d18623903ebc8b2bf145ce7dfb1f55046a17a11b4ba2581aeaf79`. Evidence: `/tmp/agent-fleet-completion-evidence/window-full.log`, `results-window.xml`, `journeys-window.json`, and `browser-window/`. External MODEL/MAIL/NODE/DEPLOY checks remain `not_run`.

## Audit repair H: Idempotent task pause

Source: `2026-09-09-v4-initial-goal-completion-design.md` §5 explicitly requires an already-paused task to return its current row. Real Flask requests return 200 for the first pause and 409 for its replay. The new `TaskCrudApiTests.test_repeated_pause_is_idempotent_without_duplicate_events` fails with `409 != 200` on `64e9d26`.

The repository already handles the duplicate without mutation or another audit row. The defect is `TaskService.pause` treating every `changed=False` result as a conflict. Preserve 409 for other disallowed states; return the paused row with `changed=False`, emitting `task_paused` only on an actual transition. This is an O(1) service decision using the existing transactional state lookup; no schema, event type or control transport change.

- [x] Add the HTTP replay regression, including identical returned state, no duplicate event/audit and no runner dispatch.
- [x] Adjust only the pause service condition and transition-only event emission.
- [x] Run task API/store/cancel regressions and add the case to the SESSION-01 journey matrix without weakening its existing acceptance.
- [x] Verify the committed pause release (checkpoint below); repeat for subsequent code changes before integration.

Focused verification: **117 passed** in 1.51 seconds across task API/store/cancel-race/cancel-supervisor tests. The replay case checks real Flask and SQLite behavior, including unchanged state, one event, one audit record and no runner dispatch.

The same audit inspected v4 gate-before-poll, continue/new-attempt, stale completion and legacy result paths, plus thin-client private native identity and terminate/quarantine precedence in source and tests. These local checks do not establish live control issuance, old-database migration recovery, or the remaining browser/notification/external acceptance requirements.

Committed `a9a89ce519ce4c85bddf3a4e0bfce7ec0faf3b22` passed **2803 tests, 154 subtests and 29/29 journeys** in 338.71 seconds, with the two existing Linux-only skips. Both packaged-release startup variants passed. The report records a clean tree, `ci_status=passed` and matrix digest `7aebb6a329625e7dd70b9572b6bb00fa1a7d3b7ab7d17cabbc41b8f0b2d2abf9`. Evidence: `/tmp/agent-fleet-completion-evidence/pause-full.log`, `results-pause.xml`, `journeys-pause.json`, and `browser-pause/`. External checks remain `not_run`.

## Audit repair I: Atomic old-task-schema upgrades

Source: v4 initial completion design §3/§4/§10/§11 requires compatible schema changes and migration evidence; the completion objective requires transactional multi-entity mutations. The previous migration test only inspected a freshly initialized database. A new disposable database uses the exact schema from `182e0340bcbe8560274aa6808d5e2e51e984fcfd` and contains tasks, leases, results, files and audit rows. Injecting a failure before rename leaves only `tasks_v4`; a subsequent startup creates an empty `tasks`, so the old task becomes invisible and related results violate foreign keys.

Before implementation, the seven-case regression set produced **5 failures and 2 passing controls**, including a real child process exiting after DROP and before RENAME. Evidence: `/tmp/agent-fleet-completion-evidence/migration-red.log`. The root cause is autocommitted schema statements. Initialization now starts `BEGIN IMMEDIATE` inside the schema script (because `executescript` commits any preceding transaction), performs additive columns/table replacement under that same transaction, checks foreign keys after rebuilding, and commits only after success. The connection context rolls back exceptions and the owned connection always closes. Foreign-key enforcement is disabled only on that initialization connection for replacement; ordinary repository connections retain enforcement.

Files: `hub/infrastructure/task_repository.py`, new `tests/test_task_schema_migration.py`, frozen `tests/fixtures/task_schema_pre_v4.sql`, RELEASE-01 and testing documentation. No new abstraction or dependency. SQL performs the table traversal and B-tree rebuild in O(n log n) without loading rows into Python; schema inspection and concurrent initialization share a startup-only writer transaction. Existing task request paths are unchanged.

- [x] Reproduce copy/drop/rename failures, process-exit recovery, invalid foreign-key admission, successful legacy upgrade and concurrent initializers.
- [x] Implement one transaction across initial schema creation and all v4 migration statements; reject inconsistent references before commit.
- [x] Run fresh migration/task regressions and Python 3.10 grammar/compile checks.
- [x] Run the final committed journey/package gate before local main integration (checkpoint below).

Fresh verification: **124 passed** across migration, task API/store and cancellation suites, then **33 passed** across migration, journey gate and legacy routes. All five original failures now pass. Both changed/new Python files pass Python 3.10 grammar and compilation checks; whitespace validation passes.

This repair prevents new interrupted upgrades from publishing partial state. It does not claim to repair previously damaged production databases or establish a live rollback drill. The full requirement audit, browser/notification decisions, Linux and external acceptance remain open.

## Verified checkpoint: execution windows, pause and schema migration (2026-10-06)

Clean committed revision `7e8dfccd7e75032e11ae8bb223d9fd2903b4fc5a` passed **2810 tests, 154 subtests and 29/29 journeys** in 548.86 seconds. Both extracted-release startup variants (`platform_enabled=False/True`) passed. The only two skips are the real Linux `/proc` stop/resume checks in `RealStopResumeTests`; Darwin still has no Docker, Podman or bubblewrap executable. All seven Python files changed since local main passed Python 3.10 grammar and compilation checks; whitespace checks passed.

Evidence: `/tmp/agent-fleet-completion-evidence/migration-full.log`, `results-migration.xml`, `journeys-migration.json`, and `browser-migration/`. The report records `working_tree_dirty=false`, `ci_status=passed` and matrix digest `5d7d107b269648c6688f780b532e55276571089ad09d94f479f1d18b65fe81bf`. MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE remain `not_run`. The verification-record commit after this revision changes only this plan; the application, tests and journey matrix are identical.

The local-main integration scope is three fixes: `64e9d26` (window validation), `a9a89ce` (pause replay), and `7e8dfcc` (atomic migration), plus this verification record. `origin/main` was fetched and is already contained in local main; the feature rebase is up to date. The primary dirty checkout and prior unconfirmed development checkout remain preserved. This is a local verification checkpoint, not whole-goal completion, a push, or deployment.

Additional audit boundary: the task-page pause/continue/gate/patch coverage includes source-contract tests, while `console_browser.cjs` renders the task page and exercises other console workflows. These checks must not be reported as real installed-agent control acceptance. Native continuation, Linux isolation, remaining browser/notification decisions and the rest of Task 3 stay open.

## Audit repair J: Preserve healthy databases on initialization failure

Sources: v4 optimization design §7.3 allows rebuilding a **corrupt** SQLite database; the frontend/backend separation plan requires each repository operation to close its connection; the completion objective requires explicit error propagation and data integrity. A real healthy pre-v4 database held under `BEGIN EXCLUSIVE` causes `_connect` to raise `SQLITE_BUSY`. Initialization catches every `DatabaseError`, renames the live database to `.corrupt`, creates an empty replacement, and returns success. The original task is then invisible. The fault injection only shortens SQLite's connection timeout to 50 ms; the file, lock and SQLite exception are real.

The first regression run produced **12 failures and 3 passing corruption controls** (`/tmp/agent-fleet-completion-evidence/database-init-red.log`). It also proves connections are leaked when journal-mode/foreign-key configuration or integrity checking fails. The repair classifies only SQLite CORRUPT/NOTADB primary codes as corruption, including extended codes. Python 3.10 lacks exception error codes, so it recognizes only the two exact SQLite corruption messages; other errors are re-raised unchanged. Configuration failures close partially opened connections, and integrity checking owns a closing context. Existing real-corruption quarantine/rebuild behavior remains compatible.

Files: `hub/infrastructure/task_repository.py`, new `tests/test_task_database_initialization.py`, RELEASE-01 and testing documentation. The classifier is O(1) and introduces no dependency, transaction store, retry loop or global lock. Initial schema migration retains repair I's atomic transaction.

- [x] Reproduce real lock-induced replacement, preserved exception identity/file bytes, and failed-connection leaks.
- [x] Implement precise corruption classification and deterministic closure; add Python 3.10, extended IO/corruption code and actual invalid-database compatibility controls.
- [x] Run initialization/migration/task/legacy-route regressions: **124 passed**. Python 3.10 grammar, compilation and whitespace checks pass.
- [x] Verify the final committed journey/package gate and integrate into local main after rebase; see the committed database/memory checkpoints and the reverified main baseline below.

The preceding goal turn made concrete progress through three verified fixes and local integration. This turn continues the full objective; it does not mark the remaining browser, notification, Linux or external acceptance work complete.

The clean `0514e91` full run exposed a concurrent-WAL setup failure: **2826 passed, 1 failed, 2 Linux-only skips, 154 subtests; 28/29 journeys** (`database-init-full.log`, `results-database-init.xml`, `journeys-database-init.json`). Multiple initializers can receive an immediate SQLite lock error while converting the old journal mode to WAL, despite the connection busy timeout. The database remains safe after the first repair, but startup still fails. Two deterministic lock-injection cases reproduce this remaining failure. Connection setup now retries only BUSY/LOCKED under one shared ten-second monotonic budget, closing each failed connection before waiting. No SQL transaction, task operation, command or executor is replayed. The real held-lock regression verifies bounded failure and unchanged data.

Fresh follow-up validation: **126 passed** across initialization/migration/task/legacy-route tests, including both new WAL-lock cases. A separate 32-round real-database stress check with four concurrent initializers per round passed all 128 initializations without quarantining a database or losing its task. Python 3.10 grammar, compilation and whitespace checks pass. The final full gate must be repeated; the failed committed run is not release evidence.

## Audit repair K: Explicit memory-context input errors

Source: M4.3 Tasks 1–2 specify strict selector, item/byte budget, revision and bounded HTTP validation before a turn is committed; the completion objective requires preserved exception causes. Actual Flask requests with invalid selectors, IDs, revisions, budgets or queries return 503 instead of client errors. Malformed Unicode is not translated by the resolver, and both search and conversation translation discard original causes. The expanded pre-fix suite records **22 failures and 2 passing controls** in `/tmp/agent-fleet-completion-evidence/memory-context-input-red.log`.

Files: `hub/application/platform_memory_context_service.py`, `hub/application/conversation_service.py`, new `tests/test_memory_context_input_validation.py`, MEMORY-01 and testing docs. Validate query character count before encoding and translate Unicode failures into `invalid_query`; all known context input codes map to bounded 400 responses. Preserve typed storage/context/validation causes across the existing boundaries while public error detail remains fixed. Search unavailability retains 503; stale selection and revision conflicts retain 409. No new state, dependency, gate or service abstraction is added. Query validation encodes at most 512 characters, and context item/byte limits remain unchanged.

- [x] Reproduce malformed HTTP inputs and cause loss before implementation; assert rejected inputs create neither messages nor Runs.
- [x] Restore stable client-error mapping, Unicode handling and original causes; preserve valid IDs/revisions, disabled context and exact 512-byte ASCII/Unicode queries.
- [x] Verify resolver, conversation, worker, memory and frontend contracts: **87 passed**. All three Python files pass Python 3.10 grammar/compilation; whitespace checks pass.
- [x] Run the final clean committed journey/package gate, then rebase and integrate the owned repairs into local main; see the committed memory/scheduler checkpoints and the reverified main baseline below.

M4 audit coverage in this continuation: the four current M4.1–M4.4 plans, schedule service/routes, memory repository/service/routes, context resolver, turn/worker handoff, picker implementation and test selectors were inspected. Before repairs L/M, gaps remained in worker snapshot validation and scheduler lifecycle. The admin browser journey already exercised memory search, selection, opt-in submission and reference-only payloads; this continuation adds explicit search-refresh retention and removal assertions. Canonical external documentation discovery is separately recorded below. This coverage does not close the remaining full-spec audit.

## Verified database initialization and memory-input checkpoint (2026-10-06)

The clean committed code revision `e021ee1be088bbf97c59230cf17e8bc1c50a49de` passed **2856 tests, 154 subtests and 29/29 journeys** in 340.08 seconds, including both extracted-release startup variants. The only two skips are the real pause/resume tests requiring Linux `/proc`. The journey report records `working_tree_dirty=false`, `ci_status=passed`, and matrix SHA-256 `93009b983c5bc2a6a218047a5eb42a6b7420ba7a883935d4c6797b74f0166733`, matching the checked-out matrix. This supersedes the failed `0514e91` gate for repairs J/K and includes the bounded WAL-contention follow-up `d5b9f91`.

Evidence: `/tmp/agent-fleet-completion-evidence/memory-context-full.log`, `results-memory-context.xml`, `journeys-memory-context.json`, and `browser-memory-context/`. MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE remain `not_run`. This checkpoint is documentation only; final committed-package startup and rebase onto current local main precede integration.

The external documentation audit also completed without modifying the vault: individual M4.1, M4.2, M4.3 and M4.4 lookups resolved through the canonical project index and project MOC, and the index has router/MOC backlinks. The stale-document scanner exited 0 with 889 notes scanned and zero stale, missing-status, missing-summary or stale-summary findings (`obsidian-doc-audit.log`). This establishes discoverability and scanner checks only, not implementation or production acceptance. Real picker interaction, the whole-spec audit and the explicitly open browser/notification/runtime requirements remain open.

## Audit repair L: Frozen memory-context worker validation

Source: M4.3 requires an immutable, bounded Run snapshot and a malformed/over-budget snapshot to fail before provider transport. A disposable persisted-Run and direct renderer regression reproduced 45 failures: false-valued or malformed present context was ignored; numeric and boolean fields were coerced; a swollen item was truncated again while its original byte count still matched; malformed item fields were rendered or surfaced as an unrelated exception. Evidence: `/tmp/agent-fleet-completion-evidence/memory-snapshot-red.log`.

The worker now validates the present snapshot before constructing the provider, with the renderer checking exact JSON scalar types, MemoryItem field bounds, unique IDs, item count and byte count. It renders the frozen item blocks verbatim and fails when the byte budget is exceeded; no second truncation is allowed. Shared pure-domain field bounds avoid different storage/worker limits, and the worker retains its existing typed failure event. Validation takes O(items + UTF-8 bytes) within the 20-item/32768-byte contract. The check does not change the default-off gate, stored schema, provider API or external authority.

The initial focused context/memory/conversation/worker/runtime/frontend run passed **139 tests**. MEMORY-01 now includes malformed persisted-snapshot and valid round-trip selectors. Full committed journey, Python 3.10 syntax and package checks are still required for this repair; this does not close the broader spec audit or browser/notification streams.

## Audit repair M: Durable schedule daemon fairness and lifecycle

Source: M4.1 requires durable owner-scoped scheduling with bounded work. Before repair, `start()` repeatedly queried the first 100 enabled owners in sorted order, so all later owners starved permanently; its loop swallowed every exception, and the returned bare Event did not let the caller join the daemon. A real SQLite/daemon regression with 101 due owners failed because owner 100 was never executed within five seconds.

The repository now pages owner IDs with an exclusive indexed cursor, and the daemon wraps to the first page after reaching the end. Each owner is guarded independently so one failure does not suppress later owners in the batch; failures emit the existing redacted structured diagnostic. The default returned stop handle preserves `Event.set()` and adds `join()`/callable signal-and-join behavior. An index supports bounded owner-page seek. Added cases prove all 205 owners appear once across pages, the 101st owner receives a background tick, failures remain secret-free while the next owner continues, and stop joins the thread.

Fresh schedule regressions: **24 passed**. Full journey/package verification after repairs L/M remains pending; the M4.1 gate is default-off.

## Audit repair N: Preserve MemoryItem storage error chains

Source: M4.2 assigns validation, optimistic revision, FTS and owner filtering to the repository, with stable application/HTTP errors; the completion objective requires retaining root causes. Three new regressions first failed: invalid IDs discarded their `ValueError`, SQLite failures lost their cause at the repository boundary, and malformed persisted `tags` silently became an empty list (`/tmp/agent-fleet-completion-evidence/memory-error-chain-red.log`).

The repository now chains validation and SQLite failures, treats malformed or noncanonical persisted tag JSON as `memory_store_corrupt`, and lets closing the owned SQLite connection roll back an unfinished transaction so a secondary `ROLLBACK` failure cannot replace the original error. The application maps storage corruption to a bounded 503, and the HTTP adapter returns stable error codes/details without cause text. Search failures are classified as storage failures with their original cause; query syntax is rejected by bounded input normalization before SQLite. No dependency, endpoint, schema, or public DTO changed.

Files: `hub/infrastructure/platform_memory_repository.py`, `hub/application/platform_memory_service.py`, `hub/http/memory_routes.py`, new `tests/test_platform_memory_error_chains.py`, MEMORY-01, `docs/testing/test-chains.md`, and this plan. The focused M4 memory/context/worker slice passed **167 tests** under Python 3.10; the original red cases plus rollback-failure and service/HTTP corruption assertions pass. The final full journey/package run and local-main integration remain pending.

## Audit repair O: Deterministic text artifact MIME

The first clean full run after repair N passed **2910 tests**, with 2 Linux-only skips, but failed 5 cases and covered only 27/29 journeys. Two FILES-01 failures reproduced independently: the host MIME database returns no type for `.md`, so `ArtifactStore` stores `application/octet-stream`; the preview reader then correctly rejects the artifact under its fixed text allowlist. The same type mismatch hid the preview toggle in the console browser acceptance.

`ArtifactStore` now assigns deterministic MIME types to the four supported text-preview extensions before consulting OS MIME data, while preserving caller-supplied types and the existing unknown-extension fallback. The MIME regression masks `mimetypes.guess_type` and verifies the `.md` download contract. The existing bounded preview test is now explicitly bound to FILES-01; it covers owner scope, truncation, binary rejection, and integrity. Focused artifact/API/frontend checks passed **16 tests**, and the console real-browser journey passed individually. The full gate is being rerun; the two separate assistant workspace timeouts from the first full run passed in an isolated repeat (**4/4 scenarios**).

## Browser fixture navigation follow-up

The fresh full gate on `efdf7b3` passed **2914 tests**, with 2 expected macOS skips and 154 subtests, but left AUTH-01 incomplete because `test_account_switch_clears_previous_identity` hit Playwright's 5-second `page.goto('/account')` `load` timeout. The server returned the page and all assets. An isolated rerun passed, confirming the timeout is intermittent; this was the remaining SPA browser fixture still waiting for `load`, while its readiness assertions already wait for the rendered account state.

Both initial navigations in `account_switch_browser.cjs` now wait for `domcontentloaded`, matching the other SPA fixtures. Existing login, rendered identity, session refresh, and stale-revision assertions remain the readiness and behavior checks. Re-run the focused browser case, then the clean committed full journey/package gate before local-main integration. This harness adjustment does not add product-level browser lifecycle or network isolation evidence.

## Verified snapshot and scheduler checkpoint (2026-10-06)

Committed code revision `1d4afc6adb7041f3f57d9cfc33ed5b170fb96e2e` passed **2911 tests, 154 subtests and 29/29 journeys** in 355.44 seconds. The two skips are the real process pause/resume cases requiring Linux `/proc`. Both packaged-release startup variants passed within the full suite. The required journey report records `working_tree_dirty=false`, `ci_status=passed`, and matrix SHA-256 `edaaf6836532ebb92c01ad8bd475f823f83225638ce0f3e2c7c6da03b438de82`, matching the current `docs/testing/journeys.json`. Evidence: `/tmp/agent-fleet-completion-evidence/final-lm-full.log`, `results-final-lm-rerun.xml`, `journeys-final-lm-rerun.json`, and `final-lm-rerun/`. MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE remain `not_run`.

One earlier full run had two browser load timeouts/one associated layout failure amid otherwise passing tests; the affected assistant admin journey and request-metadata case passed fresh individually, as did the memory-picker cases. The clean rerun above is the passing release evidence. This closes only the local gates for repairs L/M, not the full development objective. Real browser driver and three-layer network enforcement, notification decisions, external acceptance and the remaining whole-spec audit remain open.

## Verified browser-submit revocation race follow-up (2026-10-06)

The submit contract now has explicit in-flight revocation coverage in `tests/test_platform_submit_e2e.py`. The fixture waits until the admitted form POST has completed, revokes the already-consumed approval from a concurrent owner request, releases the driver, and verifies that the original command keeps its successful result. The existing pre-dispatch revocation case still verifies that no POST is sent. The focused approval/HTTP/E2E/transport set passed **216 tests**; the new scenario was repeated independently before the run.

The dirty-worktree full gate then passed **2916 tests, 2 expected Darwin `/proc` skips, 154 subtests and 29/29 journeys** in 340.27 seconds with installed Playwright/Chrome. Evidence: `/tmp/agent-fleet-completion-evidence/results-submit-followup.xml`, `/tmp/agent-fleet-completion-evidence/journeys-submit-followup.json`, and `/tmp/agent-fleet-completion-evidence/browser-submit-followup/`. The report is working-tree verification; a clean committed-revision gate remains required after these documentation/test changes. Python 3.10 parsing for the changed E2E file and `git diff --check` passed. Real browser process/network enforcement, notification decisions, Linux-only isolation, and MODEL-LIVE/MAIL-LIVE/NODE-LIVE/DEPLOY-LIVE remain open or `not_run`.

## Audit repair P: SPA auth fixture navigation readiness

The first committed-revision gate for `f85d385` failed only `tests/test_frontend_browser.py::test_assistant_auth_in_real_browser`: Chromium's initial `page.goto('/')` exceeded 5 seconds waiting for `load`, even though the Hub returned the document, all assets, and the expected auth/session requests. The remaining journey and all other tests passed (**1 failed, 2915 passed, 2 platform skips, 154 subtests; 29/29 journey selectors**). Five immediate isolated reruns passed, identifying a scheduling-sensitive harness wait rather than an application assertion failure. The account-switch fixture had already moved to `domcontentloaded` followed by explicit rendered-state assertions.

Root cause: the auth fixture waited for every SPA subresource's `load` event before checking login routing, although its contract is document routing and rendered auth state. Updated each SPA navigation and reload in `assistant_auth_browser.cjs` to wait for `domcontentloaded`; the existing URL, login heading, session, protected-request, error, and rendered UI assertions remain the readiness checks. After the change the original browser test passed **5/5 consecutive runs**. JavaScript syntax and whitespace checks passed. A fresh complete committed gate and package verification are pending.

Clean committed revision `92358a9` then passed the complete gate: **2916 tests, 2 expected Darwin `/proc` skips, 154 subtests and 29/29 journeys** in 344.16 seconds with installed Playwright/Chrome. Evidence: `/tmp/agent-fleet-completion-evidence/results-final-commit.xml`, `/tmp/agent-fleet-completion-evidence/journeys-final-commit.json`, and `/tmp/agent-fleet-completion-evidence/browser-final-commit/`. The journey report records external checks as `not_run`; MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE remain unavailable in this host. The final code revision has passed Python 3.10 compilation, all frontend/fixture JavaScript syntax checks, and whitespace validation. The remaining full objective is still open for the unapproved real-browser process/network boundary, notification contract, Linux-only isolation evidence and external acceptance.

The verified three-commit continuation (`f85d385`, `92358a9`, `bf4a12a`) was rebased against current local `main` at `18b6092` and fast-forward integrated into the separate clean main worktree. Local main now ends at `bf4a12a` and is 26 commits ahead of `origin/main`; nothing was pushed or deployed. The original primary checkout remains on `feat/t3.7-offline-browser-boundary` with its pre-existing dirty browser-submit development changes intact. Package startup and submit/auth regression tests after the documentation-only integration record passed **16 tests**.

## Current decision and environment boundary (2026-10-06)

The remaining browser/notification document is explicitly a proposal, not implementation authority. T3.7 authorizes offline policy/pinned-transport work only and forbids enabling external networking or adding an OS firewall/proxy/driver without a separately reviewed and approved design. The browser proposal still awaits a dedicated Linux isolation runtime and decisions on observation frame rate, retention, writer lease behavior, credential domains and Browserbase. Notification trigger types, channels/recipients, success behavior and retention are also unapproved product values. No values or external contacts have been inferred. The local Darwin host has no Docker, Podman or bubblewrap executable, so Linux process-level isolation and real Node/browser acceptance remain unverified. The release matrix truthfully retains MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE as `not_run`.

## Audit repair Q: PNG frame payload validation and browser writer fencing (2026-10-06)

The execution-window continuation adds a bounded `browser.frame` event linked to an owner/workspace/run/node/command/session/window-scoped consumed screenshot ticket. Ticket consumption and event publication share one SQLite transaction; any failure resets an uploading ticket and removes the just-created immutable artifact. Frame reads require the same owner/window ticket row, matching digest and `image/png` manifest, then verify content integrity before returning private, non-cacheable PNG bytes. Events expose only artifact ID, hash, dimensions, UTC capture time and a monotonic frame sequence.

Operator writer takeover now spans all attached windows for the same owner/Run. Acquisition rejects in-flight browser mutations and atomically fails queued ones with `writer_lease_active`; `CommandDeliveryService` checks active leases in the same transaction that admits subsequent mutations. Closing a window remains independent of Run cancellation. The assistant renders frames with DOM image elements, keeps the last frame while polling, supports explicit retry after load failure, and exposes explicit Take Control/Return Control actions.

Review found that `_png_dimensions` validated chunk checksums and IEND but did not validate IDAT's zlib stream, so a PNG-shaped payload with valid CRCs and invalid compressed data could be persisted as a frame. The parser now validates bounded IHDR encoding fields and incrementally decompresses IDAT in 64 KiB output chunks, discarding pixel bytes while requiring the exact scanline byte count implied by dimensions, color type, bit depth and Adam7 pass geometry. A regression covers malformed compressed data. Existing 256 KiB upload and 4096-by-4096 dimension ceilings remain unchanged.

Fresh validation after the repair: the three direct PNG/upload/frame tests passed, Python 3.10 AST/compile checks, changed JavaScript syntax checks and `git diff --check` passed, and the complete working-tree gate passed **2931 tests, 2 Darwin `/proc` skips, 154 subtests and 29/29 journeys** in 428.58 seconds. Evidence is recorded in `/tmp/agent-fleet-completion-evidence/results-submit-final-rerun.xml`, `/tmp/agent-fleet-completion-evidence/journeys-submit-final-rerun.json`, and `/tmp/agent-fleet-completion-evidence/browser-submit-final-rerun/`. The `browser.frame` test journey covers ticket publication, owner/window delivery and hash/manifest checks. Real browser lifecycle, OS-level egress enforcement, notification decisions and MODEL-LIVE/MAIL-LIVE/NODE-LIVE/DEPLOY-LIVE remain open or `not_run`.

## Audit repair R: Browser-submit synthetic marker sweep closure (2026-10-06)

The T3.10 acceptance record required submit marker exclusion across Node journals, wire receipts, Hub command receipts, Run events, model transcripts, artifact rows, Hub response bodies and captured logs. The E2E test already checked most planes, but did not serialize the wire responses captured by its in-process Node transport or inspect either the browser artifact-ticket registry or the content-addressed artifact manifest registry. This left P5 as incomplete evidence coverage even though each submitted form field is intentionally screened before upload.

Extended `tests/test_platform_submit_e2e.py::test_submit_end_to_end_contract` to scan those missing planes, selecting only non-secret ticket metadata and using the existing owner-scoped artifact listing. No runtime behavior changed. Focused submit approval/HTTP/E2E/transport verification passed **189 tests**. The first fresh full gate run had one scheduler-sensitive deadline assertion exceed its `<0.6s` test bound (`0.798s`); that parameter passed alone and the complete deadline module passed **19 tests**. A clean full-gate rerun passed **2931 tests, 2 Darwin `/proc` skips, 154 subtests and 29/29 journeys** in 443.60 seconds. Evidence: `/tmp/agent-fleet-final-marker-sweep-retry/results.xml` and `/tmp/agent-fleet-final-marker-sweep-retry/journeys.json`.

## Final local gate after repair R (2026-10-06)

The current committed revision `38b33bb` passed the complete required local journey gate: **2931 passed, 2 Darwin `/proc` skips, 154 subtests and 29/29 journeys** in 349.25 seconds. Evidence: `/tmp/agent-fleet-development-final/results.xml` and `/tmp/agent-fleet-development-final/journeys.json`; external MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE checks remain `not_run`. Python 3.10 grammar parsing covered 398 files, `compileall` passed for `hub`, `tools` and `tests`, changed frontend/fixture JavaScript syntax checks passed, and release/package startup regressions passed **50 tests and 2 subtests**. The existing `tools/probe/discovery.py` invalid-escape `SyntaxWarning` remains unrelated.

This closes the current local regression and packaging evidence for the implemented audit repairs, including repair R. It does not close the explicit whole-spec requirements that need a reviewed real-browser process/network boundary, approved observation/notification contracts, Linux isolation evidence, provider/mail/node environments, or authorized deployment.

## Audit repair S: Persisted service JSON integrity (2026-10-06)

Two new regressions reproduced a silent data-integrity failure: malformed or non-object `service_definitions.checks` JSON was projected as `{}` and a public service listing returned 200, while the repository discarded the original `JSONDecodeError`. The same permissive decoder also filtered malformed `allowed_actions` values instead of rejecting the persisted row.

`ServiceRepository` now rejects non-finite JSON constants, requires object/list shapes and string list members, and raises the existing bounded `service_store` error while retaining the decode cause. `ServiceHealthService` preserves the repository cause through its typed application error; HTTP details remain fixed and never expose persisted content. Added `tests/test_platform_service_persisted_validation.py` covers both direct cause retention and owner-scoped 503 responses with secret markers excluded. The service health/postcheck/log/incident/monitoring regression slice passed **34 tests**.

The full journey and committed-package gate must be rerun after this repair. It does not alter any production gate or claim external service acceptance.

The first post-repair gate passed **2933 tests, 2 Darwin `/proc` skips, 154 subtests and 29/29 journeys** in 379.91 seconds. After adding the `allowed_actions` regression to `MONITOR-01`, the final local gate passed **2934 tests, 2 Darwin `/proc` skips, 154 subtests and 29/29 journeys** in 341.93 seconds. Evidence: `/tmp/agent-fleet-development-final-s2/results.xml` and `/tmp/agent-fleet-development-final-s2/journeys.json`; external checks remain `not_run`. The service, monitoring, incident, HTTP probe and committed-package/startup regression slice passed **52 tests and 2 subtests**. Python 3.10 grammar parsing for the changed files, compileall, changed JavaScript syntax checks, whitespace validation and package/startup checks passed.

The parameterized persisted-JSON boundary tests were then rerun against the corrected `MONITOR-01` selector. The dirty-worktree gate passed **2937 tests, 2 Darwin `/proc` skips, 154 subtests and 29/29 journeys** in 354.64 seconds; after the verification record was committed, the clean revision `e4f653e` passed the same gate with **2937 tests, 2 Darwin `/proc` skips, 154 subtests and 29/29 journeys** in 373.46 seconds. Evidence: `/tmp/agent-fleet-service-json-repair-final/results.xml`, `/tmp/agent-fleet-service-json-repair-final/journeys.json`, `/tmp/agent-fleet-development-final-clean/results.xml`, and `/tmp/agent-fleet-development-final-clean/journeys.json`; external MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE remain `not_run`. The service, monitoring, incident, HTTP probe and committed-package/startup slice passed **130 tests and 2 subtests**; Python 3.10 AST/compile, changed JavaScript syntax and whitespace checks passed.

The clean committed-revision gate then passed **2937 tests, 2 Darwin `/proc` skips, 154 subtests and 29/29 journeys** in 371.50 seconds. Evidence: `/tmp/agent-fleet-service-json-repair-committed/results.xml` and `/tmp/agent-fleet-service-json-repair-committed/journeys.json`; the report records revision `e4f653ea8835afdf85cc60ad98f93dc53fb4c36d`, `working_tree_dirty=false`, `ci_status=passed`, matrix SHA-256 `56b56aa6c951f25445c3e01def97e3dd41f7a364a81f5ed00cc3e0ed3c7dc8ab`, and MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE as `not_run`. Python 3.10 AST/compile checks, whitespace validation and the service/package regression slice (**130 tests and 2 subtests**) passed on the committed tree.

## Audit repair T: Fail closed on persisted monitoring JSON (2026-10-06)

The monitoring audit found the same silent corruption boundary in three durable read paths. A malformed `incidents.evidence_ids` or `incidents.latest_detail` value was previously reduced to an empty projection, a malformed scheduler `platform_jobs.last_result` value was projected as `{}`, and malformed schedule `target`/`last_result` values used the same permissive fallback. The owner-facing Incident, Komari status and schedule routes therefore returned 200 while hiding persisted corruption; JSON decoding causes were discarded.

The Incident repository now uses strict decoders for the bounded string-array and object fields, rejecting wrong shapes and non-finite JSON constants as `incident_store` while retaining the original cause. The platform scheduler repository applies the same fail-closed object decoder to `last_result` and raises `scheduler_store`; the schedule repository now strictly bounds and validates its persisted object fields and raises `schedule_store`. Existing route translation keeps the response status at 503 with fixed detail and no persisted marker content. No scheduler lease, retry, or public payload contract changed.

Files: `hub/infrastructure/incident_repository.py`, `hub/infrastructure/platform_scheduler_repository.py`, `hub/infrastructure/platform_schedule_repository.py`, `tests/test_platform_incidents.py`, `tests/test_platform_monitoring.py`, `tests/test_platform_schedules.py`, `docs/testing/journeys.json`. The MONITOR-01 matrix now executes the Incident and scheduler owner-route corruption regressions. The SCHEDULE-01 matrix adds coverage for strict `target`/`last_result` decoding. Three consecutive monitoring focused runs passed **39 tests each** before the final parameterized scheduler cases; the current monitoring/service slice passed **47 tests**, and the schedule/recovery/memory slice passed **66 tests**. The complete journey/package gate and committed revision evidence remain pending.

The second working-tree gate, including the added schedule selector, passed **2950 tests, 2 Darwin `/proc` skips, 154 subtests and 29/29 journeys** in 346.38 seconds. Evidence: `/tmp/agent-fleet-monitoring-schedule-repair-20261006/results.xml` and `/tmp/agent-fleet-monitoring-schedule-repair-20261006/journeys.json`; the report records revision `79a72f335e3443e70639d46d8dda0c36b2d66959`, `working_tree_dirty=true`, `ci_status=passed`, matrix SHA-256 `100e7b0bd654e370bb3f4014085c6b71f37e4f5316f4f48e25542e521122ef2b`, and external MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE as `not_run`. Python 3.10 AST/compile, whitespace validation, and the package/startup regression slice passed **50 tests and 2 subtests**. A clean committed-revision gate remains required after recording and committing this evidence.

## Audit repair U: Strict JSON writes for monitoring records (2026-10-06)

The strict read-path repair exposed a matching write-path gap: Python's default `json.dumps` emits `NaN`, `Infinity` and `-Infinity`, even though those tokens are not JSON and the repaired readers reject them. A red/green regression cycle reproduced all nine non-finite values as committed rows followed by `incident_store`, `scheduler_store` or `schedule_store` read failures. The incident writer now returns `invalid_detail` before inserting; scheduler and schedule writers return `invalid_result` before finishing or advancing state.

The three bounded JSON encoders now use `allow_nan=False`; their existing typed validation errors and transaction boundaries handle the resulting `ValueError`. Regression cases verify that invalid data does not create an incident, complete a scheduler lease or mutate a schedule. The cases are bound to MONITOR-01 and SCHEDULE-01. Focused monitoring/schedule recovery verification passed **61 tests**. All six new parameter cases passed; a deliberate fix removal reproduced the expected nine failures, followed by a fresh **9/9** pass after restoring the strict encoders.

The first full gate on this worktree passed **2958 tests, 2 Darwin `/proc` skips, 154 subtests and 29/29 journeys**, with one intermittent assistant auth browser timeout. Its trace showed that `expectLogin` still waited for Playwright's default `load` event even though the page navigation now waits for `domcontentloaded`. Updating `expectLogin` then exposed the same default in the `login()` helper. All four `waitForURL` calls in this fixture now use the DOM-ready condition. The auth selector passed **5/5** consecutive fresh browser runs. A second full gate reached 29/29 journey coverage but had seven Komari loopback connection failures (`source_unavailable`); the same Komari module passed **25/25** immediately in isolation. No Komari code changed; repeat the full gate to determine whether this was transient under suite load.

The first clean run of `b42b69d` exposed the missing write-side guard as **9 failures**: non-finite detail/result values were persisted and later surfaced as `incident_store`, `scheduler_store` or `schedule_store` instead of the existing input errors. After the three encoder changes, the incident/monitoring/schedule focused slice passed **46 tests**; Python 3.10 AST/compile and whitespace checks passed.

The final clean-tree gate on `847fe5a` passed **2959 tests, 2 Darwin `/proc` skips, 154 subtests and 29/29 journeys** in 414.09 seconds. Evidence: `/tmp/agent-fleet-final-clean-20261006/results.xml` and `/tmp/agent-fleet-final-clean-20261006/journeys.json`; the report records `ci_status=passed`, matrix SHA-256 `b14ae3d219cdadce3f5a8ee9cadcc313fd8e680a414d28b6e5d9f3a4d0f63dce`, and external MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE as `not_run`. The run started from a clean `847fe5a` checkout; the report's dirty flag was set by this documentation-only evidence update made while the gate was running. Package/startup regressions passed **50 tests and 2 subtests**. The next clean rerun after this record is required to produce a final `working_tree_dirty=false` report.

## Audit repair V: Strict JSON writes for service records (2026-10-07)

The post-gate audit found the same write-side gap in `ServiceRepository`: Python's default JSON encoder accepted `NaN`, `Infinity` and `-Infinity` for service `checks` and health evidence `detail`. The row was written, then the repository's strict read projection failed with `service_store`, so callers saw a storage error after a partial write attempt instead of the existing `invalid_evidence_detail` input error.

The service JSON encoder now uses `allow_nan=False`, preserving the existing bounded error and transaction behavior. Six parameter cases cover service-definition and health-evidence writes and verify that no invalid data is persisted. MONITOR-01 includes both selectors once each. The six new cases failed before the encoder change and pass afterward; the combined service/Incident/scheduler/schedule focused slice passed **79 tests**. Python 3.10 compilation, JavaScript syntax, matrix JSON and whitespace checks passed.

The final clean committed-revision gate for repair V passed **2965 tests, 2 Darwin `/proc` skips, 154 subtests and 29/29 journeys** in 379.01 seconds. Evidence: `/tmp/agent-fleet-service-json-final/results.xml` and `/tmp/agent-fleet-service-json-final/journeys.json`; the report records revision `a2d54f406dddd60b940c77dc0e6ad53772b229ec`, `working_tree_dirty=false`, `ci_status=passed`, matrix SHA-256 `568c47402def50e8a8a37ca53087b0b7c89e8efc453869e8aa8e706c75cb67e2`, matching the current matrix, and external MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE as `not_run`. Package/startup regressions passed **50 tests and 2 subtests**; Python 3.10 compileall and the changed fixture JavaScript syntax check passed. Commits `adae324` and `f4db8a3` after this run only update the plan record; repair code and journey matrix are unchanged. This closes local verification for repairs T, U and V. Real-browser process/network enforcement, approved notification policy, Linux isolation and external MODEL/MAIL/NODE/DEPLOY acceptance remain open.

## Audit repair W: Strict JSON for execution-window metadata (2026-10-07)

Source: T3.2/T3.3 execution-window metadata contract and the completion audit's strict persisted-JSON boundary. Before the fix, repository and authenticated Flask regressions for NaN, Infinity, and -Infinity all failed: `ExecutionWindowRepository._json` used Python's permissive default encoder, so these non-standard JSON constants were accepted and written; the HTTP route returned 200 instead of the existing `invalid_metadata` client error.

The repository metadata encoder now sets `allow_nan=False`. Its existing bounded translation returns `invalid_metadata` before opening the write transaction, so no window or ticket row is created. The journey matrix binds both the direct repository and authenticated HTTP regressions to WINDOW-01; the testing chain and platform status document record the failure and corrected behavior.

Fresh focused verification after restoring the fix: **57 passed** across `test_platform_execution_windows.py`, `test_frontend_execution_window_contracts.py`, and `test_platform_conversation.py`. A deliberate removal of strict encoding reproduced all **6** new parameter cases (three values across repository and HTTP boundaries) as failures. Python JSON syntax validation, `git diff --check`, and focused verification passed. A full journey and clean committed-package gate after repairs W and later audit changes remains pending; repair W does not close Task 3's whole-spec audit or the real-browser, notification, Linux-isolation, and external-acceptance requirements.

## Audit repair X: Strict platform JSON values and Unicode (2026-10-07)

The same audit traced the shared `PlatformRepository._json` write boundary used by conversation overrides and Run snapshots. Before the change, authenticated `POST /conversations` returned 200 and echoed `NaN` in its JSON response for all three non-finite float values, then persisted the same non-standard token. Three HTTP regressions now require `invalid_value`/400 and no conversation row. The common platform JSON encoder now sets `allow_nan=False`, and `ConversationService` translates that existing repository error to a bounded client error. The test is bound to CONFIG-01 and documented in the platform contract and test chain.

Fresh targeted verification after the platform-repository fix: **3 passed** for the new conversation regression. The first full run without browser prerequisites produced **2965 passed, 14 skipped, 154 subtests and 24/29 journeys**; it is diagnostic only because AUTH-01, ASSIST-01, RUN-01, MEMORY-01 and UI-01 require Playwright. The complete rerun with Playwright/Chrome passed **2978 tests, 2 Darwin `/proc` skips, 154 subtests and 29/29 journeys** in 337.75 seconds. Evidence: `/tmp/agent-fleet-completion-evidence/results-current.xml` and `/tmp/agent-fleet-completion-evidence/journeys-current.json`.

Review of the full exception path then found that both strict encoders discarded their caught `ValueError`, and `ConversationService.create` discarded the typed repository error. Added three cause-chain regression selectors across CONFIG-01/WINDOW-01; before repair all five checks failed, and after retaining each exception with `raise ... from exc`, all **5 passed**. The chain remains internal; HTTP error codes/details are unchanged. The two additional CONFIG-01 selectors require a fresh complete journey/package gate before closing this local audit batch. Python 3.10 compilation, matrix JSON validation, changed-file syntax checks and whitespace validation remain part of the final gate. External MODEL-LIVE/MAIL-LIVE/NODE-LIVE/DEPLOY-LIVE, real browser process/network enforcement, Linux isolation, notification approval and whole-spec audit remain open.

## Local verification after repairs W/X (2026-10-07)

The fresh browser-enabled full journey gate passed **2980 tests, 2 Darwin `/proc` skips, 154 subtests and 29/29 journeys** in 332.50 seconds. JUnit records 3136 collected cases, 0 failures and 0 errors. Evidence: `/tmp/agent-fleet-completion-evidence/results-final-current.xml` and `/tmp/agent-fleet-completion-evidence/journeys-final-current.json`. The report records revision `a31a388f8278960978c1696fbefbed6a0f2b1529`, `working_tree_dirty=true`, `ci_status=passed`, matrix SHA-256 `077dda616b93ea49cbad7024cbdeb597ba98afe07e473c129eb3c087b01d535f`, and MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE as `not_run`. Python 3.10 grammar parsing passed for all five changed Python files; `python3.10 -m compileall -q hub tools tests` and `git diff --check` passed. The packaged release startup regression passed both `platform_enabled=False` and `platform_enabled=True` variants (**2 passed**).

Evidence audit note: `/tmp/agent-fleet-strict-json-20261007/journeys.json` is not valid for the current matrix; it reports `389c0140e0817e8fc813e1f4297453bfc1fdfda537d512dc547cb248629c1c3a`. Use `journeys-final-current.json`, whose SHA matches the current matrix and whose 346/346 selectors all passed. A fresh focused rerun of the changed conversation/window/frontend slice passed **66 tests**; a fresh packaged-startup rerun passed both feature-gate variants, Python 3.10 compileall and whitespace validation passed. A clean committed-revision full gate is still required after committing this batch.

This closes the local full-journey and package-startup verification for the current working-tree batch, including repairs W/X and their exception-chain selectors. The worktree remains uncommitted. Task 3's whole-spec audit, dedicated Linux/process-level browser isolation, approved observation/notification decisions, and live MODEL/MAIL/NODE/DEPLOY acceptance remain open; this passing local gate does not satisfy those requirements or complete Task 4.

## Audit repair Y: Reject explicit non-object conversation overrides (2026-10-07)

The CONFIG-01 contract stores request and conversation overrides as JSON objects. The existing routes and application service used `overrides or {}`, converting explicit empty arrays, strings, zero, false, and null into valid empty configuration. Eight authenticated Flask parameter cases failed before repair: conversation creation returned 200 and turn submission returned 202, silently creating rows/Runs.

Both HTTP routes now default only an omitted field; an explicitly provided value must be an object. `ConversationService.create` and `turn` enforce the same contract before writes for direct callers, and `resolve_run_config` rejects non-mapping request overrides at the domain boundary. The journey matrix includes the HTTP create/turn cases, the service guards, and the direct domain contract.

The original eight failing HTTP cases now pass. Expanded focused verification passes **46 tests** across the conversation/defaults modules, including HTTP, application, repository and domain boundaries. The final gate below supersedes the earlier diagnostic run that overlapped another checkout.

## Verified committed checkpoint after Repair Y (2026-10-07)

Committed revision `edb0de8deb6094423f5f4c5ee76ef6990fca2181` passed the browser-enabled full journey gate: **3006 passed, 2 Darwin `/proc` skips, 154 subtests, 29/29 journeys and 350/350 selectors** in 338.58 seconds. JUnit contains 3162 cases, 0 failures, 0 errors and 2 skips. Evidence: `/tmp/agent-fleet-completion-final-edb0de8-20261007/results.xml` and `journeys.json`. The report revision and matrix SHA-256 `7e6192d18e1c69fc77aadc35b211296de587263c7417b5ccd8edfe3d420c7d3b` match the committed code and current matrix; `ci_status=passed`, while its dirty flag is `true`. Post-run inspection shows only this plan file differs from HEAD; source, tests and matrix are unchanged. `MODEL-LIVE`, `MAIL-LIVE`, `NODE-LIVE` and `DEPLOY-LIVE` remain `not_run`.

The packaged startup regression passed both platform-gate variants (**2 passed**) in the suite and on a fresh focused rerun. Python 3.10 `compileall -q hub tools tests`, whitespace validation, and a focused schedule/memory slice (**38 passed**) passed. No frontend source changed. This closes local verification for repairs W, X and Y, including the repository mapping guard. The earlier numeric-string schedule concern is already closed by commit `cf7dfcf4ed2d6c4902ef307142c444d1a01b08bd`: `_finite()` accepts only JSON integer/float types, and the existing HTTP regressions cover numeric strings such as `"10"` and `"100"`. Task 3's whole-spec audit, Linux/process-level browser isolation, approved observation/notification decisions, and live external acceptance remain open; the local gate does not complete the continuing development objective.

## Audit repair Z: Tolerate incomplete request start timestamps (2026-10-07)

The request-metadata contract permits lifecycle fields to be absent in historical/incomplete events. A persisted `provider_request_started` event with no `started_at` makes the Run projection subtract `None` from the wall clock and return HTTP 503. String, boolean, array and an oversized integer timestamp also either break the query or leak an invalid timestamp into the public request record; the initial malformed-value cases reproduced the defect.

The domain projection now retains `started_at` only when it is a numeric, finite timestamp within the JavaScript Date range. The repository adds live `elapsed_ms` only when that validated timestamp exists. Missing or malformed start-time metadata no longer blocks the owner-scoped Run/Conversation read and never fabricates a duration; normal timestamps and terminal `unknown` conversion retain their existing behavior. USAGE-01, the request-metadata contract and the test chain include this regression.

Fresh focused verification of the final missing/invalid timestamp parameter set: **31 passed** across `test_request_metadata_api.py` and `test_request_metadata.py`. The current-worktree browser-enabled full gate passed **3012 tests, 2 Darwin `/proc` skips, 154 subtests, 29/29 journeys and 351/351 selectors** in 339.04 seconds. JUnit contains 3168 cases with 0 failures, 0 errors and 2 skips. Evidence: `/tmp/agent-fleet-current-worktree-f17aedb-20261007/results.xml` and `journeys.json`; report revision `f17aedb76ff4974df0c153830776c4c5ab83c523`, `ci_status=passed`, working tree dirty because Repair Z was still uncommitted, and matrix SHA `f40ad63c918d2e4d2a951517f92a58d18bc0cf491a37c19acd8807df303efc4a`, matching the matrix at run start and after completion. All four external checks remain `not_run`.

Python 3.10 `compileall -q hub tools tests`, whitespace validation, and packaged startup with both platform-gate variants (**2 passed**) also passed. The committed clean-revision gate remains required after this repair is committed.

## Current requirements audit boundary (2026-10-07)

The repository's current platform document and the October 4/5 T3.7 and submit specifications take precedence over the linked Obsidian browser-plan draft last updated October 5. The draft predates the repository's `browser.frame` event/artifact/UI and execution-window writer-fence implementation; those local contracts are present and covered by the committed BROWSER/WINDOW journeys. This does not change the requirements below.

- **Browser runtime / T3.6:** a production Chromium driver and complete lifecycle evidence against a real Node are still absent. Local Playwright frontend journeys exercise the console, not a browser-worker adapter.
- **Browser egress / T3.7:** offline policy/pinned-transport seams and loopback TLS fixtures exist, but the transport is not wired into `NodeRuntime`; browser-native interception, owned CONNECT egress, and OS/container denial of direct sockets must all be proven together. The Mac host has no Linux `/proc` environment or configured container isolation runtime for the two skipped process tests.
- **Observation, takeover, grants and cloud browser:** frame display and control-plane fencing exist. Real-driver races, product-approved observation cadence/retention, Browserbase equivalence, and credential/SecretBroker binding remain unverified or unimplemented. The draft proposes values but is not approval authority.
- **Notifications:** the platform roadmap names notifications but no executable trigger/channel/recipient/retention specification has been approved. No delivery policy should be inferred from existing events or account-verification email.
- **Production evidence:** live MODEL, MAIL, NODE and DEPLOY checks remain `not_run`; real Komari/test-node pilots, off-host backup/key escrow and production rollback also require their named environments and evidence.

The current local code checkpoint is committed and verified, but these entries keep Task 3 and Task 4 open. No external gate, deployment, or production browser capability was enabled by this audit.

### Current specification-to-evidence map (2026-10-07)

The following map records current replacement contracts rather than historical plan checkboxes. `docs/testing/journeys.json` is the executable regression index; the named test modules provide the detailed cases. A local pass establishes only its stated boundary.

| Current contract | Implementation boundary | Executable evidence | Remaining acceptance |
|---|---|---|---|
| T3.6 browser session, fixed tools, artifacts and lifecycle | `tools/platform/browser_backend.py`, `tools/platform/node_runtime.py`, `hub/infrastructure/browser_repository.py`, `tools/platform/artifacts.py` | `tests/test_platform_browser_policy_backend.py`, `tests/test_platform_node_http_e2e.py`, BROWSER-01/02 | Real Chromium driver, restart/crash cleanup against a real Node, browser lifecycle and production capability remain absent. |
| T3.7 URL/origin policy, redirects, DNS pinning, TLS and budgets | `tools/platform/browser_url.py`, `tools/platform/browser_transport.py` | `tests/test_platform_browser_transport.py`, BROWSER-01 | Offline seams and loopback TLS are covered; browser-native interception, CONNECT egress, bypass denial and OS/container socket isolation are blocked. T3.6 plan now points to this distinction. |
| Browser submit approval | `hub/domain/browser_submit.py`, `hub/infrastructure/browser_repository.py`, `hub/application/command_delivery_service.py`, `tools/platform/browser_transport.py` | approval/HTTP/E2E/transport suites, BROWSER-02 | Injected-driver contract is covered; real DOM and network runtime acceptance is unavailable. |
| Native session supervision and continuation | `tools/session/`, `tools/supervisor/`, adoption/control services | `tests/test_supervisor_process.py`, `tests/test_supervisor_control.py`, `tests/test_session_*.py`, SESSION-01/ADOPT-01 | Linux `/proc` and process-control cases require Linux CI; production installed-agent acceptance remains NODE-LIVE. Exact-capture source upgrade remains explicitly deferred pending a signed probe-side contract. |
| Runs, provider requests, Node delivery and recovery | `hub/application/`, `hub/infrastructure/command_repository.py`, `tools/platform/node_client.py`, provider adapters | RUN/NODE/USAGE journeys; `tests/test_platform_journal_admission.py`, `tests/test_request_metadata*.py`, `tests/test_platform_node_http_e2e.py` | MODEL-LIVE and NODE-LIVE require configured external environments and real credentials/nodes. |
| Schedules, memory, monitoring, actions and backup | schedule/memory/monitoring/incident repositories and services | SCHEDULE-01, MEMORY-01, MONITOR-01, ACTION-01, BACKUP-01 | Real Komari/test-node pilot, off-host backup/key escrow, retention operation and production restore/rollback remain external gates. |
| Accounts, frontend, artifacts, windows and release | account/auth services, `frontend/assets/`, release/deploy scripts | AUTH/MAIL/UI/WINDOW/FILES/RELEASE journeys; package/startup tests | MAIL-LIVE needs actual inbox evidence; DEPLOY-LIVE needs production deployment, backup and rollback evidence. |
| Notifications and browser observation/takeover/credentials/Browserbase | No approved executable product contract for the remaining behavior | `docs/testing/browser-notification-proposal.md` only | Proposal values are not implementation authority. Owner decisions and, for external capabilities, named environments are required before runtime implementation or success claims. |

Audit outcome: no additional local runtime defect was established by documentation/status inspection alone. T3.7 offline redirect and address-pinning cases are not deferred; real-browser enforcement remains blocked. The Linux `/proc` skips and live acceptance checks remain `not_run`, not complete.

## Audit finding AJ: Exact-capture control-plane/source boundary (2026-10-07)

The current adoption endpoint and its required `ADOPT-01` regressions verify an operator-only, audited control-plane transition: an adopted row can be labelled `exact` once, and the response/audit contain no probe-private identity. The implementation does not yet make that transition true for the managed capture source. `ControlClient._build_bridge_config()` deliberately carries `best_effort=True`, the `SessionBridge` therefore emits best-effort events, and the fixed supervisor action set has no signed probe-side quality-upgrade action. The session repository also refuses ordinary telemetry from upgrading a best-effort source.

This is a specification boundary, not a safe local bug fix. The thin-client specification keeps exact capture independent and explicitly says the UI must not call `capture-exact`; `HANDOFF.md` therefore records the production upgrade as `deferred-with-condition`. Do not remove the best-effort clamp, relax the quality monotonicity rule, or treat the HTTP 200 label as evidence of exact transcript capture. A future exact implementation needs an approved source/capability contract, an operator-authorized signed probe-side transition, and an end-to-end test proving subsequent events use the exact source while retaining AEAD/quota/retention/raw-read audit behavior.

The existing exact-capture HTTP/transcript tests and the concurrent revoke fence remain valid evidence for the control-plane state machine only. No runtime code changed in this audit finding; the unresolved source transition remains an explicit TODO rather than a silently claimed completion.

## Verified committed checkpoint: concurrent RELEASE-01 smoke isolation (2026-10-07)

The prior full gate exposed one RELEASE-01 failure: `test_smoke_verifies_uploaded_conversation` received HTTP 403 from `/api/ingest`. Running two copies of `deploy/e2e-smoke.sh` concurrently reproduced it deterministically. Both scripts announced readiness on fixed port 8799; the second request reached the first Hub, whose per-run ingest token correctly rejected it.

The local smoke harness now reserves a kernel-selected loopback port for each Hub and uses that port for readiness and API traffic. `test_concurrent_smoke_runs_use_isolated_hubs` starts two full smoke scripts concurrently and requires both to reach `SMOKE OK`; it was added to SESSION-01. The failing concurrency regression passed after the change, as did the smoke, release-layout and packaged-startup checks (**54 passed, 2 subtests**). Python 3.10 compilation, JSON validation, shell syntax and whitespace checks passed.

Committed revision `98afe850b3d257e2199bafa789724bc933cd3b06` passed the fresh browser-enabled required journey gate: **3013 passed, 2 Darwin `/proc` skips, 154 subtests, 29/29 journeys and all 352 selectors** in 331.46 seconds. JUnit contains 3015 cases with zero failures/errors and only the two documented `/proc` skips. Evidence: `/tmp/agent-fleet-completion-evidence/results-smoke-concurrency-20261007.xml` and `journeys-smoke-concurrency-20261007.json`; the report records this revision, `working_tree_dirty=false`, `ci_status=passed` and matrix SHA-256 `2bc1a59617122a4f3badbad3cb343cb39189703f112c9f144fbc1b2f660e2280`, matching the committed matrix. MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE remain `not_run`. The primary worktree remains untouched; no merge, push or deployment was performed.

Task 3 and Task 4 remain open: the whole current-spec audit is not complete, real browser-driver and OS/container network isolation evidence is unavailable, observation/notification product decisions are unapproved, and production/live acceptance requires its actual environments. This checkpoint closes the local smoke concurrency repair and verification only.

## Verified committed checkpoint: assistant model catalog browser wait (2026-10-07)

The required gate immediately after `98afe85` reported one failure in `test_assistant_workspace_regressions[model]`: the saved model selector was empty at assertion time. The fixture's readiness predicate treated a missing `#assistant-model` element as success because `!undefined` is true. The failure trace identifies `workspace_browser.cjs:154`; four fresh focused reruns passed before changing the fixture, confirming the timing dependence.

`openHistory()` now waits for the model selector to exist, contain options, and have a non-empty selected value. The model scenario passed five consecutive focused reruns; all four assistant workspace scenarios then passed (**4 passed**). Node syntax and whitespace checks passed.

Committed revision `6f7c0b9798d68fb43ce2837b3fd693526e5bddf3` passed the fresh browser-enabled required journey gate: **3013 passed, 2 Darwin `/proc` skips, 154 subtests, 29/29 journeys and 352/352 selectors** in 331.26 seconds. JUnit records 3169 cases, zero failures, zero errors and two skips. Evidence: `/tmp/agent-fleet-final-6f7c0b9-20261007/results.xml` and `journeys.json`. The journey report records this exact revision, `working_tree_dirty=false`, `ci_status=passed`, and matrix SHA-256 `2bc1a59617122a4f3badbad3cb343cb39189703f112c9f144fbc1b2f660e2280`, matching the committed matrix. Python 3.10.21 `compileall -q hub tools tests` passed, as did packaged-release startup with both platform gate variants (**2 passed**).

MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE remain `not_run`. This closes the local browser-fixture timing repair and its committed verification only. The whole-spec audit, real browser-driver and OS/container network isolation, approved observation/notification requirements, Linux-only evidence, and production acceptance remain open; the continuing development objective is not complete.

## Audit repair AA: Require native session continuation coverage (2026-10-07)

The v6 thin-client specification §2.1/§6/Phase 4 requires continuation to use a complete private native identity, preserve the original cwd/environment, refuse a sibling process when identity is incomplete, retain identity after natural completion, and reject continuation after quarantine. Source and unit regressions already implemented and exercised these cases, but none were listed in the executable `SESSION-01` journey. The full `--require-journeys` gate therefore did not protect this approved behavior from being deselected or removed.

Added eight existing native-resume regressions to `SESSION-01`, covering attached sessions without identity, incomplete native identity, same-session Codex/Pi resume, no sibling creation, quarantine refusal, `DEVNULL`/cwd/env/token preservation, and resume after natural completion. Updated the test-chain description and corrected the M1/M2.6 roadmap entries to reflect the completed disposable encrypted backup drill while retaining the production workflow, escrow, off-host retention, and live-layout recovery gaps.

All eight native continuation selectors passed in a focused run; the journey-gate suite passed **20 tests**. Committed revision `590ecba61f520f0c9c65bd61cf8288a5b9bc9115` passed the browser-enabled required journey gate: **3013 passed, 2 Darwin `/proc` skips, 154 subtests, 29/29 journeys and 360/360 selectors** in 332.79 seconds. `SESSION-01` passed all **44/44** selectors. JUnit contains 3169 cases with zero failures/errors and two skips. Evidence: `/tmp/agent-fleet-final-590ecba-20261007/results.xml` and `journeys.json`; the report records the exact revision, `working_tree_dirty=false`, `ci_status=passed`, and matrix SHA-256 `87d8b5695c8f07f68a18d84dd0384f65fbbd1fa6481a0b2806f5ea7a196ce162`, matching the committed matrix. Python 3.10.21 compileall and packaged-release startup with both platform-gate variants (**2 passed**) also passed.

MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE remain `not_run`. The local journey now protects the native-resume contract; real installed-agent acceptance still belongs to NODE-LIVE. Whole-spec audit, Linux/process-level browser isolation, unresolved observation/notification approval, and production acceptance remain open.

## Audit repair AB: Require backup failure and retention coverage (2026-10-07)

M2.6/M2.8 define corruption, symlink/path traversal, no-overwrite, encrypted wrong-key/tamper, plaintext cleanup, and bounded dry-run/apply retention behavior. Those implementations and regression tests existed, but `BACKUP-01` required only four selectors; most failure and retention cases could regress without failing the required journey gate.

Expanded `BACKUP-01` to bind all **17** backup and encrypted-retention test functions and updated the test-chain contract. No runtime backup behavior changed. The focused backup modules passed **18 tests** (including parameter cases), and the journey-gate suite passed **20 tests**.

Committed revision `b07ba164488026f205950cb134bdf191cbb17ea8` passed the browser-enabled required journey gate: **3013 passed, 2 Darwin `/proc` skips, 154 subtests, 29/29 journeys and 373/373 selectors** in 334.36 seconds. `BACKUP-01` passed all **17/17** selectors. JUnit contains 3169 cases, zero failures, zero errors, and two skips. Evidence: `/tmp/agent-fleet-final-b07ba16-20261007/results.xml` and `journeys.json`; the report records the exact revision, `working_tree_dirty=false`, `ci_status=passed`, and matrix SHA-256 `bdb1b65127b0dc21a60bbd6e37508f2ea1ee85958a35d17fd0b347727286380f`, matching the committed matrix. Python 3.10 compileall and packaged-release startup with both platform-gate variants (**2 passed**) also passed.

MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE remain `not_run`. This closes local required-coverage mapping for the backup contract; it does not establish off-host durability, production key escrow, scheduled retention, or a live deployment restore/rollback window. The wider current-spec audit remains open.

## Audit repair AC: Fence concurrent native session controls (2026-10-07)

The Phase 4 contract requires terminate/quarantine to preempt an in-flight native follow-up. A blocking `GroupOps.create` regression reproduced the violation: terminate completed against the old handle while native resume was starting, then the late resume replaced the handle and changed the terminal manifest back to `running`. Review also found that stale wait/completion work could release a replacement handle and simultaneous follow-ups could create competing processes.

Each private managed entry now has a local reentrant control lock, monotonic control epoch, and one pending-resume admission. Follow-up prepares under the lock, starts outside it, and commits only if the entry, epoch, and state are still current; rejected late starts are killed, reaped, and closed. Terminate, quarantine, pause, resume, and detach invalidate pending epochs. Completion and timeout paths verify they still own the observed handle before changing state or terminating it. Paused and quarantined sessions refuse native follow-up; natural `completed` sessions retain the documented resume path.

Added SESSION-01 regressions cover terminate/quarantine/pause/resume during resume startup, failed stale-child cleanup, stale completion after handle replacement, duplicate concurrent resume admission, and paused-session refusal. The focused supervisor, attach, control, session chaos, runner-conversation, and adoption-control/service slice passes **267 tests, 2 Darwin `/proc` skips, and 2 subtests**; all concurrency selectors pass, and the journey-gate suite passes **20 tests**. Python 3.10 compileall, journey JSON parsing, and `git diff --check` pass. A fault-injected SIGKILL/reap failure returns the bounded `escape_unverified` outcome instead of the earlier `terminated` result.

The first full gate exposed an intermittent UI-01 fixture race: the model selector became ready before conversation history finished loading the executed-model label. `workspace_browser.cjs` now waits for the label before asserting it; the focused browser case passed. The committed revision `920963c3dced31243638e6d023aaf67b49d30c1d` passed the fresh browser-enabled required gate: **3020 passed, 2 Darwin `/proc` skips, 156 subtests, 29/29 journeys** in 384.70 seconds. The report at `/tmp/agent-fleet-session-control-final-20261007/journeys.json` records this revision, `working_tree_dirty=false`, `ci_status=passed`, and matrix SHA-256 `7817895843d780800eb080b006436b8bef8f39ecebf572433011bdda3cedb889`, matching the committed matrix. JUnit is `/tmp/agent-fleet-session-control-final-20261007/results.xml`. Python 3.10 `compileall -q hub tools tests` passed, as did packaged-release startup with both feature-gate variants (**2 passed**).

MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE remain `not_run`. These local tests do not establish cross-process runtime coordination, real installed-agent session behavior, Linux process isolation, or remaining external acceptance.

## Audit repair AD: Serialize machine spool quota admission (2026-10-07)

The session supervision contract sets a hard 128 MiB machine spool limit and requires exact/structured events to pause rather than be silently lost. Two independent session spools sharing one machine root previously performed the quota check and frame write without a shared admission lock. A deterministic barrier reproduction let both sessions accept one event against a one-event remaining quota; the machine footprint became `804` bytes against a `418` byte limit, so the bound was violated.

The repair keeps the existing encrypted segment and checkpoint format. A short root-scoped quota guard now uses an in-process reentrant lock plus a POSIX advisory lock file, covering only quota check, frame write, rotation and checkpoint persistence. Lock setup failures map to the existing bounded `spool_setup` error with the original cause retained. Public rotation uses the same guard without nesting the file lock from append's automatic rotation. No upload, read, ack, or executor work occurs under the guard.

The new `QuotaConcurrencyTests.test_machine_quota_check_waits_for_other_session_write` regression is bound to SESSION-01 and proves the second session waits until the first write completes, then receives the existing quota result; the final machine footprint stays within the configured limit. Fresh spool/uploader/bridge/chaos and journey-gate verification passed **140 tests**. Python 3.10 `compileall -q hub tools tests`, matrix JSON validation, `git diff --check`, and packaged-release/startup verification (**50 tests, 2 subtests**) passed.

The browser-enabled full required journey gate passed **3021 tests, 2 Darwin `/proc` skips, 156 subtests and 29/29 journeys** in 335.31 seconds. Evidence: `/tmp/agent-fleet-spool-quota-20261007/results.xml` and `journeys.json`. The report records pre-commit revision `60758c9086bd24e780fcdd9e02147296ba69d2fe`, `working_tree_dirty=true`, `ci_status=passed`, and matrix SHA-256 `4dda95609a038643ea64f9125d4ece79b2093047ced386575698c73097f378d7`, matching the matrix; the new SESSION-01 selector passed **1/1**. A clean committed-revision gate remains required after this batch is committed. Linux process tests remain unexecuted on Darwin, and MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE remain `not_run`.

## Audit repair AE: Serialize transcript raw-quota admission (2026-10-07)

The session transcript contract caps encrypted raw storage at 256 MiB per session. `TranscriptRepository.ingest` previously used SQLite autocommit: duplicate/sequence inspection, raw-byte accounting and raw insertion were separate commits. A deterministic concurrent ingest reproduction paused the first redacted write and let a second writer observe the same raw total; both returned `raw_written=True` against a one-event budget.

The repair wraps one event's duplicate check, sequence-gap calculation, raw encryption/quota decision, redacted row and raw row publication in `BEGIN IMMEDIATE`/commit. Rejected quota paths roll back; duplicate paths roll back; raw encryption or raw insert failures still commit the redacted stream and retain the existing `raw_write_failed` result. The connection rolls back any unfinished transaction before close. No external I/O or new dependency is introduced.

`TranscriptRetentionAndQuotaTests.test_concurrent_raw_ingest_cannot_exceed_session_quota` is bound to SESSION-01. Focused transcript/session/bridge verification passed **159 tests and 6 subtests**; the new concurrency test passes and its pre-fix reproduction showed two raw writes over the configured budget. Python 3.10 parsing, matrix validation and whitespace checks passed. Full required journey and committed package verification for this repair is pending.

The clean committed-revision gate for `f8bacde2eb1cab843c43b87963be27feb81b15a4` passed **3022 tests, 2 Darwin `/proc` skips, 156 subtests and 29/29 journeys** in 390.22 seconds. Evidence: `/tmp/agent-fleet-transcript-quota-committed-20261007/results.xml` and `journeys.json`; the report records `working_tree_dirty=false`, `ci_status=passed`, and matrix SHA-256 `281d3ca08116d9958ef27bc8892327ef2eb7a29039985b17fe7a0dbfe37e8143`, matching the matrix. Both new SESSION-01 selectors passed **1/1**. Python 3.10 compileall and packaged-release/startup verification (**50 tests, 2 subtests**) passed. The two Darwin `/proc` skips and external MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE `not_run` statuses remain unchanged.

## Audit repair AF: Fence adoption control admission against revoke (2026-10-07)

The adoption contract allowed `AdoptionService.control()` to read an `adopted` row and pause before `SupervisorService.enqueue()`. A concurrent `revoke_cas()` could therefore commit `revoked` and enqueue `detach`, after which the paused control still entered the queue. A deterministic barrier reproduction recorded `revoke_cas -> control_enqueue` and left a control command queued for a revoked seat.

The service now serializes the adopted-seat decision with one reentrant admission lock shared by operator control, explicit revoke, probe guard reconciliation, and drift auto-revoke. The lock covers only the existing status check, command enqueue, tracking and bounded audit bookkeeping; the adoption repository state machine, signed command format, and probe-side identity guard remain unchanged. This gives one in-process Hub service a strict order: a control admitted first may be followed by revoke, while a revoke that wins first prevents control admission.

Added `test_control_admission_is_fenced_against_concurrent_revoke` to `ADOPT-01`. The adoption/service, lifecycle, HTTP, E2E, attach and guard slice passed **98 tests**, including the new selector; the pre-fix barrier reproduced the reversed order and queued control. Python 3.10.21 `compileall -q hub tools tests`, journey JSON parsing, and whitespace checks passed.

Committed revision `bd2ad271efc69cb11d78f798061cf2c2d7a235a8` passed the browser-enabled required journey gate: **3023 passed, 2 Darwin `/proc` skips, 156 subtests and 29/29 journeys** in 333.43 seconds. Evidence: `/tmp/agent-fleet-adoption-fence-20261007/results.xml` and `journeys.json`; the report records this revision, `working_tree_dirty=false`, `ci_status=passed`, and matrix SHA-256 `2f4322e2c11ec562240cecc3e01ac8e9b7e3ad49812fbaec0f5d74c63bafa83a`, matching `docs/testing/journeys.json`. ADOPT-01 passed **4/4** selectors, including the new concurrency case. Packaged release/startup regressions passed **52 tests and 2 subtests**. A Python 3.10 full pytest attempt could not collect because that interpreter has no installed Flask, Werkzeug or cryptography dependencies; the full browser gate and focused tests used the repository's dependency-complete Python 3.14 environment, while Python 3.10 compilation passed. This repair does not establish cross-process Hub coordination or real installed-agent acceptance; those remain NODE-LIVE/external requirements.

## Audit repair AG: Fence exact-capture upgrade admission against revoke (2026-10-07)

The exact-capture contract permits promotion only for an adopted seat and requires one bounded `capture_exact` audit. The status check and `update_capture_quality()` previously ran outside the adoption admission fence, so a concurrent revoke could change the row after the check and still allow a quality upgrade on a revoked seat. A blocking repository regression now reproduces the interleaving and requires the upgrade write to complete before the revoke CAS; the final state is explicitly `revoked` with `capture_quality=exact`, which is the valid result when the upgrade was admitted first.

`upgrade_capture_exact()` now shares the service's reentrant admission lock with control, explicit revoke, probe reconciliation and drift auto-revoke. The lock covers the existing status check, one repository quality update and one bounded audit; it does not widen to transcript raw capture or unrelated repository operations. Added `test_exact_capture_admission_is_fenced_against_concurrent_revoke` to `ADOPT-01` and updated the test-chain contract so both control issuance and quality promotion are protected from a later revoke.

Red-green evidence: with the admission lock temporarily removed, the new regression failed on the observed order `revoke_cas -> capture_update`; restoring the lock made the same test pass. The adoption/service, lifecycle, HTTP, E2E, attach and guard slice passed **120 tests**; the five ADOPT-01 selectors passed **5/5**. Python 3.10.21 `compileall -q hub tools tests`, journey JSON parsing and `git diff --check` passed.

Committed revision `6413c8c601d101b7bb0b7e6bf3f0bc8b50f2d225` passed the fresh browser-enabled required journey gate: **3024 passed, 2 Darwin `/proc` skips, 156 subtests, 29/29 journeys and 856/856 parameterized selector instances** in 357.18 seconds. Evidence: `/tmp/agent-fleet-exact-capture-20261007/results.xml` and `journeys.json`; the report records this revision, `working_tree_dirty=false`, `ci_status=passed`, and matrix SHA-256 `f713996dcd59d9257f19fc904ff2c9d83e4e191f3f16cc42cc5eb6485aeee0ed`, matching `docs/testing/journeys.json`. All five ADOPT-01 selectors passed. Packaged release/startup, release-layout and auto-deploy regressions passed **88 tests and 2 subtests**.

The two `/proc` skips are platform-specific on Darwin. The local admission lock coordinates one in-process Hub service; it does not establish cross-process Hub coordination. Real installed-agent acceptance and MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE remain external requirements.

## Audit repair AH: Bind exact-capture contract tests to ADOPT-01 (2026-10-07)

The exact-capture implementation already had nine HTTP and transcript regressions covering operator-only authentication, successful audited promotion, revoked/pending/unknown/malformed state gates, idempotency, response redaction, and encrypted raw capture. They were not listed in `docs/testing/journeys.json`, so the required journey could pass while the whole endpoint contract was deselected.

Bound all nine existing selectors to `ADOPT-01` and expanded the test-chain acceptance text to name their auth, state, idempotency, redacted-stream and encrypted-raw guarantees. No runtime behavior changed. This mapping requires a fresh focused slice and a clean committed journey/package gate before it is considered verified.

Committed matrix revision `3a7af9eed3a606a22a39aac508f75a2e7f59b013` passed the fresh browser-enabled required gate: **3024 passed, 2 Darwin `/proc` skips, 156 subtests, 29/29 journeys and 865/865 parameterized selector instances** in 322.99 seconds. ADOPT-01 passed **14/14** selectors. Evidence: `/tmp/agent-fleet-browser-readiness-final2-20261007/results.xml` and `journeys.json`; the report records `working_tree_dirty=false`, `ci_status=passed`, and matrix SHA-256 `d45464805d50dffea1ec39fc5faeaef07b2c7708a7f90e4457b841eb00698795`, matching `docs/testing/journeys.json`. Python 3.10 compileall, changed fixture JavaScript syntax checks, whitespace validation and package/startup/release-layout/auto-deploy regressions (**88 tests, 2 subtests**) passed. External MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE remain `not_run`.

## Audit repair AI: Use DOM readiness for SPA browser fixture navigation (2026-10-07)

The first clean gate after the ADOPT-01 mapping exposed two unrelated UI-01 fixture timeouts: modal session-expiry recovery waited for a `/login` `load` event, and request-metadata navigation waited for the conversation document `load` event. Both pages had already fired `domcontentloaded` and rendered their required DOM/API state; the SPA's long-lived activity prevented the load wait from settling within the fixture timeout.

Changed SPA navigation and reload waits in the workspace, request-metadata and console fixtures to use `domcontentloaded`; each route retains its existing semantic DOM/API readiness assertions. The `modal` workspace case, request-metadata browser case and full console browser case passed individually; the combined workspace/request-metadata slice passed **5 tests**. The first clean rerun cleared the original two failures and exposed the same load-event issue in the console fixture, which this follow-up covers.

## Verified final local gate (2026-10-07)

The latest committed revision `3a7af9eed3a606a22a39aac508f75a2e7f59b013` passed the complete required gate with **3024 passed, 2 Darwin `/proc` skips, 156 subtests and 29/29 journeys** in 361.03 seconds. Evidence: `/tmp/agent-fleet-browser-readiness-final2-20261007/results.xml`, `/tmp/agent-fleet-browser-readiness-final2-20261007/journeys.json`, and `/tmp/agent-fleet-browser-readiness-final2-20261007/browser/`. The report records `working_tree_dirty=false`, `ci_status=passed`, and matrix SHA-256 `d45464805d50dffea1ec39fc5faeaef07b2c7708a7f90e4457b841eb00698795`, matching `docs/testing/journeys.json`; all four external checks remain `not_run`. Python 3.10 `compileall -q hub tools tests`, changed fixture JavaScript syntax, `git diff --check`, and 75 packaged-release/startup/layout/auto-deploy regression tests with 2 subtests passed.

This closes the local regression, syntax and committed-package evidence for the implemented repairs through AI. The two `/proc` checks remain platform-specific skips on Darwin. The whole-spec audit still cannot be marked complete: the approved offline browser contract does not authorize real browser lifecycle or process-level egress enforcement, the browser observation/takeover/credential/Browserbase and notification proposal has not been approved as an implementation specification, and MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE require their real configured environments.

## Audit repair AJ: Restore atomic submit assembly (2026-10-07)

The continuing submit audit reproduced a production-assembly regression that
the earlier fixture evidence did not expose. The current `SubmitApprovalService`
contract requires a Run lookup, but bootstrap passed only the browser store;
the worker also carried duplicate submit kwargs. Separately, the remote broker
consumed an approval before `CommandDeliveryService.enqueue_submit()`, so the
transactional admission path could attempt a second consume after the first
approval had already been burned.

The repair passes the platform repository to the service, removes duplicate
assembly arguments, exposes transaction-aware consume and terminal-expiry
delegation on the service, and lets `enqueue_submit()` perform the only approval
consumption while it signs and persists the final command. The E2E fixture now
enables all parent gates, declares `browser.submit`, supplies the matching Node
verification key, and grants through the public owner service before dispatch.

Fresh focused verification passed **139 tests** across submit HTTP, approval
store, remote delivery, Node HTTP E2E, Run worker and conversation suites. This is working-tree
evidence only; a clean committed full-suite/package gate is still required for
this repair. Real browser lifecycle, process-level egress, Linux-only checks,
and MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE remain open requirements.

The clean committed revision `3d4502222555704e933c12a311ea71e4ff9063dc` then
passed the complete required browser-enabled gate: **3027 passed, 2 Darwin
`/proc` skips, 156 subtests, 29/29 journeys and 865/865 parameterized selector
instances** in 328.36 seconds. Evidence:
`/tmp/agent-fleet-submit-wiring-final-20261007/results.xml`,
`/tmp/agent-fleet-submit-wiring-final-20261007/journeys.json`, and
`/tmp/agent-fleet-submit-wiring-final-20261007/browser/`. The report records
`working_tree_dirty=false`, `ci_status=passed`, and matrix SHA-256
`d45464805d50dffea1ec39fc5faeaef07b2c7708a7f90e4457b841eb00698795`, matching
`docs/testing/journeys.json`; MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE
remain `not_run`. Python 3.10 compileall, all changed fixture JavaScript
syntax checks, whitespace validation and 75 packaged-release/startup/layout/
auto-deploy regression tests with 2 subtests passed.

This closes the committed local evidence for the atomic submit assembly repair.
The approved offline contract still does not prove real browser lifecycle,
process-level egress isolation or production credentials; those remain outside
the local Darwin gate and require their named environments/decisions.

The clean committed revision `3d4502222555704e933c12a311ea71e4ff9063dc` then
passed the browser-enabled required gate: **3027 passed, 2 Darwin `/proc`
skips, 156 subtests and 29/29 journeys** in 328.54 seconds. The release,
layout and auto-deploy regression slice passed **88 tests and 2 subtests**.
Evidence: `/tmp/agent-fleet-submit-wiring-final-20261007/results.xml` and
`journeys.json`; the report records `working_tree_dirty=false`,
`ci_status=passed`, and matrix SHA-256
`d45464805d50dffea1ec39fc5faeaef07b2c7708a7f90e4457b841eb00698795`.
The two `/proc` checks remain platform-specific on Darwin. MODEL-LIVE,
MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE remain `not_run`, and the broader
whole-spec audit remains open.

## Reverified integrated local main baseline (2026-10-07)

The local main baseline `fad108459f227ad70ba5668df8caa2e1816ded76` was
rechecked from a clean isolated worktree before this audit's uncommitted
documentation edits. The required browser-enabled gate passed **3027 tests, 2 Darwin
`/proc` skips, 156 subtests, 29/29 journeys, 393 selector definitions and
865/865 parameterized selector instances** in 383.07 seconds. JUnit contains
3185 cases, zero failures, zero errors and two skips. Evidence:
`/tmp/agent-fleet-completion-todo-20261007/results.xml`,
`journeys.json`, and `browser/`. The report records revision
`fad108459f227ad70ba5668df8caa2e1816ded76`, `working_tree_dirty=false`,
`ci_status=passed`, and matrix SHA-256
`d45464805d50dffea1ec39fc5faeaef07b2c7708a7f90e4457b841eb00698795`,
matching `docs/testing/journeys.json`. The release-layout/auto-deploy slice
passed **73 tests and 2 subtests**, and Python 3.10 `compileall -q hub tools
tests` passed.

The audit matrix now distinguishes the approved offline T3.7 transport
contract from real browser/runtime egress enforcement, records the exact
capture source-upgrade boundary, and classifies historical unchecked plan
items separately from current requirements. External MODEL-LIVE, MAIL-LIVE,
NODE-LIVE and DEPLOY-LIVE checks remain `not_run`; Linux isolation, approved
observation/notification contracts, real-browser runtime acceptance and the
signed probe-side exact-capture upgrade remain open. This checkpoint closes
the stale audit baseline and local committed-main gate only; Tasks 3 and 4
remain open.

## Audit repair AK: Use DOM readiness for the full-flow SPA entry (2026-10-07)

The first full gate after the current audit state reproduced one real local
journey failure in `ASSIST-01`: Chromium received `200` for `/assistant` and
all static assets, but `page.goto()` timed out while waiting for the SPA's
`load` event. The same selector passed on an isolated rerun, and the other
SPA fixtures already use `domcontentloaded` followed by explicit route,
authentication and rendered-state assertions. This identifies the failure as
an unstable resource-load wait in the fixture rather than an application
contract failure.

Changed `tests/fixtures/frontend/full_flow_browser.cjs` to wait for
`domcontentloaded` on the initial `/assistant` navigation. The existing
`/login` redirect, login form, authenticated `/assistant`, `就绪`, Run,
artifact, recovery, failure and logout assertions remain the semantic
readiness checks; no product runtime or feature gate changed.

Failing evidence: `/tmp/agent-fleet-current-goal-20261007/results.xml` and
`journeys.json` recorded `3026 passed, 1 failed, 2 skipped`, with the failure
at `full_flow_browser.cjs:14` waiting 12 seconds for `load`. Fresh focused
verification passed once before the edit was recorded and four consecutive
runs after it. The fresh working-tree full gate then passed **3027 tests, 2
Darwin `/proc` skips, 156 subtests and 29/29 journeys** in 323.46 seconds;
evidence is under `/tmp/agent-fleet-current-goal-20261007/full-rerun/`, with
matrix SHA-256 `d45464805d50dffea1ec39fc5faeaef07b2c7708a7f90e4457b841eb00698795`.
The clean committed-revision gate and package slice remain required after
this repair is committed. External MODEL-LIVE, MAIL-LIVE, NODE-LIVE and
DEPLOY-LIVE remain `not_run`.

The committed repair revision `640e939efd102feb88d9e6fbdcfa92f40437eff3`
then passed the complete browser-enabled required gate: **3027 passed, 2
Darwin `/proc` skips, 156 subtests and 29/29 journeys** in 323.61 seconds.
JUnit contains 3185 cases with zero failures, zero errors and two skips. The
report records `working_tree_dirty=false`, `ci_status=passed`, and matrix
SHA-256 `d45464805d50dffea1ec39fc5faeaef07b2c7708a7f90e4457b841eb00698795`,
matching `docs/testing/journeys.json`. Evidence is under
`/tmp/agent-fleet-current-goal-20261007/committed-rerun/`. Python 3.10
compileall, changed fixture JavaScript syntax and the release-layout/
auto-deploy slice (**73 tests, 2 subtests**) also passed. This closes the
local AK regression. Linux isolation, approved browser/notification
contracts and MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE remain open.

After the AK repair, `codex/development-completion-todo` was rebased onto
the then-current local `main` (already up to date) and fast-forwarded into
local `main`, advancing it from `fad1084` to `93a41a1`. The primary
`feat/t3.7-offline-browser-boundary` worktree remained untouched. No remote
push or production deployment was performed.

## Audit repair AL: Reject user-supplied browser loopback URLs (2026-10-07)

The T3.7 contract allows loopback only under a test-only transport fixture override. The shared `browser_policy.validate_url()` instead returned every `localhost` or loopback IP unchanged, before checking `network_enabled`, allowlists, or address policy. This affected both Hub `RemoteToolBroker` admission and Node `LocalBrowserBackend` validation. A new regression failed before repair because `http://localhost:3000` was accepted with networking disabled; the same focused run reported **1 failed, 118 passed**.

The policy now rejects loopback URLs with `network_disabled` when the network gate is off and `origin_forbidden` when it is on, before any resolver call. The pinned transport retains its separate explicit `fixture_loopback` injection used only by transport tests. Fake-driver tests now use `https://browser.fixture.test` with an injected resolver mapped to a synthetic global address; no external request is made. `NodeRuntime` still forces browser network enforcement off. The stale T3.6 loopback sentence was aligned with T3.7, and the policy/Hub regressions were added to required BROWSER-01 coverage.

The focused browser, transport, submit, Hub/Node and runtime slice passed **283 tests** after repair. The current-worktree browser-enabled full gate then passed **3029 tests, 2 Darwin `/proc` skips, 156 subtests and 29/29 journeys** in 323.60 seconds. Evidence is under `/tmp/agent-fleet-current-goal-20261007/results-browser-policy-followup.xml`, `/tmp/agent-fleet-current-goal-20261007/journeys-browser-policy-followup.json`, and `/tmp/agent-fleet-current-goal-20261007/browser-policy-followup/`; the report records revision `4005e69ac969a30b72c854f57210cc435315c2b7`, `working_tree_dirty=true`, `ci_status=passed`, and matrix SHA-256 `9b000a877de1b92c9904739a9fa75d764718c3b9c4e8cff2a12372164e4406bb`. Python 3.10 compileall and `git diff --check` also passed.

The clean committed revision `51aabe3c667d7f83b45a428ed259e8c028a15f1c` then passed the same browser-enabled required gate: **3029 tests, 2 Darwin `/proc` skips, 156 subtests, 29/29 journeys and 395/395 selector instances** in 322.64 seconds. JUnit contains 3187 cases with zero failures, zero errors and two skips. The report records `working_tree_dirty=false`, `ci_status=passed`, and matrix SHA-256 `9b000a877de1b92c9904739a9fa75d764718c3b9c4e8cff2a12372164e4406bb`, matching `docs/testing/journeys.json`; Python 3.10 compileall and whitespace validation passed. External MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE remain `not_run`. This closes the local verification for AL only; it does not complete T3.7 runtime integration, Linux `/proc` evidence, or Task 3/4.

## Audit repair AM: Enforce release evidence against the actual checkout (2026-10-07)

The gate previously trusted `revision` and `working_tree_dirty` values written
inside `journeys.json`; a stale or dirty checkout could present a forged clean
report. Added `tools.testing.release_evidence` as the single release evidence
validator. It now compares the requested revision with `git rev-parse HEAD`
and requires an empty porcelain status, in addition to binding the report to
the journey matrix digest, every required selector/parameter result, zero
JUnit failures/errors/skips, a PNG screenshot signature and external checks
remaining `not_run`. GATE-01 covers clean evidence and adversarial invalid
evidence. The Git operations use bounded subprocess timeouts; no new dependency
is introduced.

CI runs this validator after the full journey test command and before the
package job becomes eligible. `test-chains.md` documents that command as the
single manual validator and removes the duplicate inline standard-library
implementation.

The same-checkout full gate initially found a UI-01 browser fixture race: after
reload it waited only for the sidebar history row before reading the restored
workspace selection. The app restores the conversation and workspace artifacts
asynchronously. The fixture now waits for the workspace artifact control before
checking the bound workspace. The original browser test failed at
`console_browser.cjs:89`; the focused real-Chrome selector and full required
journey gate passed after this synchronization change.

Fresh evidence for this dirty development checkout is recorded at
`/tmp/agent-fleet-release-evidence-worktree/results-rerun.xml`,
`journeys-rerun.json`, and `browser-rerun/`: **3042 passed, 2 Darwin `/proc`
skips, 156 subtests, 29/29 journeys**; JUnit has 3046 cases, zero failures,
zero errors and two expected Darwin skips. Matrix SHA-256 is
`9c740c0a8bc79580b32e6fabc9d5f71e4589ce37c4211084912d1c586359c52c`; all
external checks remain `not_run`. The validator-specific tests passed
**53 tests**; release layout/auto-deploy regressions passed **73 tests and 2
subtests**; Python 3.10 compile, browser-fixture Node syntax and
`git diff --check` passed. Because the full run is dirty and on Darwin, it is
development evidence only; the new actual-checkout validator and CI workflow
must be verified from a clean committed revision and Linux CI before Task 4's
local release gate is closed.

The actual-checkout check rejects the synthetic clean report used by older unit
fixtures, so those tests now create a small clean Git checkout and explicitly
cover mismatched HEAD and dirty porcelain state. The consolidated focused
evidence/journey gate passes **53 tests** after those additions. Every PNG
entry must have a valid signature; one valid screenshot cannot mask a corrupt
file elsewhere in the artifact.

The local release-evidence implementation and CI wiring are reviewable in the
development worktree, but this checkpoint does not complete Task 3 or Task 4.
The current UI-01 full gate passed locally on Darwin; Linux `/proc` evidence,
real Chromium/Node network isolation, browser product decisions, exact-capture
source upgrade and MODEL-LIVE/MAIL-LIVE/NODE-LIVE/DEPLOY-LIVE remain open.

The first complete run including the release-evidence validator's tests passed
in the current dirty development checkout: **3063 passed, 2 Darwin `/proc`
skips, 156 subtests, 29/29 journeys, 403 selector entries and 900 selector
instances** in 334.84 seconds. JUnit contains 3065 testcase elements, zero
failures/errors and two skips. The process skips remain a local platform
limitation. The matrix digest is
`e3c88626e08e026829762fb8b830de1787fcb76912d4a2babd66e465c8f09650`; the
report revision is `676672a46c0d66d9d47d0d4cf57653ac256d2df5` and correctly says
`working_tree_dirty=true`. Evidence is under
`/tmp/agent-fleet-release-evidence-worktree/{results-final.xml,journeys-final.json,browser-final/}`.
All external checks remain `not_run`. Python 3.10 compileall, changed browser
fixture Node syntax, and `git diff --check` passed. This dirty report is not
release evidence and the clean committed run plus CI/Linux validation remain
open.

The committed revision `ea186dacc4886101d588b1aa67832f33795358e3` then passed
the same browser-enabled required gate from a clean checkout: **3063 passed,
2 Darwin `/proc` skips, 156 subtests, 29/29 journeys, 403 selector entries and
900 selector instances** in 344.84 seconds. JUnit contains 3065 testcase
elements, zero failures/errors and two skips; the report records
`working_tree_dirty=false`, `ci_status=passed`, and the matrix digest above.
Running `tools.testing.release_evidence` against this report and the actual
checkout rejected it with `junit_incomplete`, as required for a Darwin report
with skips. Linux CI must provide the zero-skip artifact before this can be
release evidence. The package slice passed **73 tests and 2 subtests**; all
external checks remain `not_run`.

## Verified final development-branch gate (2026-10-07)

The final development commit `7797d5475a95c00d55027fcbf17ffa236838bf6f` was
verified from a clean checkout on Python 3.14.7 and Python 3.10.21, both with
real Chrome browser journeys enabled. Each run passed **3063 tests, 156
subtests, and 29/29 journeys**, with only the two expected Darwin `/proc`
skips; JUnit had zero failures/errors. The report revision and matrix digest
matched this commit and `docs/testing/journeys.json`, and all four external
checks remained `not_run`. The release evidence validator correctly rejected
the reports as `junit_incomplete` because Darwin skipped the two Linux process
tests. Linux CI evidence for this development revision is not available: the
latest remote `main` run is for `f6c35f9`, and this branch has no remote run.
The local branch is 95 commits ahead of `origin/main` (local `main` is 93
ahead, and this branch adds two commits); it has not been pushed.

Release-layout/auto-deploy regressions passed **73 tests and 2 subtests**;
Python 3.10 compileall, fixture JavaScript syntax, and `git diff --check`
passed. Current HEAD packages were built and inspected. This closes local
verification only. Real browser network isolation, approved browser and
notification contracts, exact-capture source upgrade, and MODEL-LIVE,
MAIL-LIVE, NODE-LIVE, and DEPLOY-LIVE remain open.

## Latest committed revision gate (2026-10-07)

After adding the worker artifact journey selector, clean commit
`69f9a1926135c59bd308938e7edd7fd371b62a9f` passed the full browser-enabled
Python 3.10.21 gate: **3063 passed, 2 Darwin `/proc` skips, 156 subtests,
29/29 journeys, 404 selector definitions and 901/901 selector instances**
in 320.61 seconds. The JUnit report has 3221 cases, zero failures, zero
errors and the same two platform skips. Evidence is under
`/tmp/agent-fleet-final-head-69f9a19/`. The report binds the clean checkout
to this exact revision and matrix SHA-256
`2a9f73a02e1774b10c9d5803ace9d072f74f77ee58006a964cf0b91eae65923b`, which
matches `docs/testing/journeys.json`; all four external checks remain
`not_run`.

The release evidence validator rejected this Darwin report with
`junit_incomplete`, as required because Linux `/proc` tests were skipped.
This is fresh local development evidence, not release evidence. On the same
commit, release-layout/auto-deploy regressions passed **73 tests and 2
subtests**. Python 3.10 compileall, browser fixture JavaScript syntax, shell
syntax, and `git diff --check` passed. Backend and frontend release packages
were built from this commit and inspected: the backend archive had 571
members, no runtime state/credentials/database or frontend files, and the
matching `RELEASE_ORIGIN`; the frontend manifest matched all 42 packaged files
with no forbidden paths. Artifacts are under
`/tmp/agent-fleet-package-861af/`.

The evidence-update commit `861af433d3a24d4ef280b437e16cdaca295ed754` then
passed the same browser-enabled Python 3.10.21 full gate: **3063 passed, 2
Darwin `/proc` skips, 156 subtests, 29/29 journeys, 404 selector definitions
and 901/901 selector instances** in 335.32 seconds. JUnit has 3221 cases,
zero failures, zero errors and two expected platform skips. Its report binds
clean HEAD and matrix SHA-256 `2a9f73a02e1774b10c9d5803ace9d072f74f77ee58006a964cf0b91eae65923b`;
all four external checks remain `not_run`. The validator again returned
`junit_incomplete` for the Darwin skips. Evidence: `/tmp/agent-fleet-final-861af43/`.

The current development branch has no Linux CI run for this revision. Task 3
and Task 4 remain open for Linux/runtime evidence and the unapproved product
contracts listed in `docs/testing/current-spec-audit.md`; no push, merge or
production deployment was performed.

## Rebased local integration checkpoint (2026-10-08)

Before local integration, `codex/development-completion-todo` was rebased onto
the latest local `main` commit `676672a46c0d66d9d47d0d4cf57653ac256d2df5`.
Conflict resolution retained main's 29-journey matrix and stronger model
readiness assertions, then added the FILES-01 worker-artifact selector. The
main worktree remained on `feat/t3.7-offline-browser-boundary` and clean.

Clean code commit `88b12483691bc41c630df68d0bdce00430d87572` passed the complete
Chrome-enabled gate: **3063 passed, 2 Darwin `/proc` skips, 156 subtests,
29/29 journeys, 404 selector definitions and 901/901 selector instances** in
323.19 seconds. JUnit contains 3065 cases, zero failures, zero errors and the
two expected platform skips. The report binds clean HEAD and journey matrix
SHA-256 `2a9f73a02e1774b10c9d5803ace9d072f74f77ee58006a964cf0b91eae65923b`.
Evidence is in `/tmp/agent-fleet-main-rebase-final/`.

Python 3.10 compileall and JavaScript fixture syntax checks passed. The
release-evidence and journey-gate suites passed **54 tests**, and release
layout/auto-deploy regression passed **73 tests and 2 subtests**. The release
validator returned `junit_incomplete` because Darwin skips the Linux `/proc`
tests; no local result is represented as Linux release evidence. External
MODEL-LIVE, MAIL-LIVE, NODE-LIVE and DEPLOY-LIVE remain `not_run`. The
unapproved browser/notification product contracts, Linux runtime isolation,
probe-side exact-capture upgrade, and external recovery/rollback evidence
remain open, so Tasks 3 and 4 are not complete.

## Guardian PID fencing checkpoint (2026-10-08)

The shell Guardian previously checked `/proc/<pid>` and then sent TERM/KILL by
numeric PID, leaving a reuse window that could signal an unrelated process.
`deploy/hk-web-process-control.py` now opens a Linux pidfd, checks UID/cwd/argv,
probes that bound process, and sends both TERM and any timed KILL through the
same descriptor. Unsupported pidfd APIs and unreadable live identities stop the
restart attempt; the numeric `agent-fleet-web.pid` format remains unchanged.

The new regression first failed against the direct numeric `kill`, then passed
with simulated PID reuse after pidfd open, same-pidfd TERM/KILL, and fail-closed
controls. Python 3.10 Guardian/runner-layout slice: **50 passed, 2 subtests
passed**; `bash -n`, Python 3.10 compilation, and `git diff --check` passed.
This checkout is Darwin, so the simulated pidfd tests are not Linux runtime
evidence. A Linux container exercise and clean committed package/startup check
remain open, alongside the existing Tasks 3/4 product decisions and external
acceptance requirements.

The same numeric-PID reuse pattern was found in the installed container deployer
and installer. auto_deploy_container.py now captures process descriptors and
signals only after comparing PID, kind, and start time against the process-table
snapshot. The installer routes old Hub, probe, and rollback candidate Hub stops
through the release pidfd helper. It rescans probe identities before deleting
the advisory PID file and stops the candidate probe before restoring LIVE.
Rollback avoids starting a replacement Hub when candidate stop or restored
process identity is ambiguous. New behavior coverage includes dead pidfds,
unsupported pidfd APIs, same-descriptor escalation, installer cwd/UID arguments,
probe rescan, and removal of numeric-PID signals from rollback.

Fresh Python 3.10 focused verification: **64 passed, 2 subtests passed** across
deployment pidfd, both Guardians, release-layout, rollback, and auto-deploy
tests; the Python 3.14 auto-deploy suite passed **38 tests**. bash -n, Python
3.10 compilation, and git diff --check pass. The shell Guardian stops its
sleep child through pidfd after checking the expected parent PID, and its new
Hub check relies on process identity rather than kill(0). The host is Darwin;
pidfd behavior is simulated and no Linux signals were sent.

package-release.sh was exercised against current HEAD, but git archive did not
include the still-untracked helper, so that archive is not evidence for the new
code. A clean tracked release archive/startup check and disposable Linux
symlink/overlay installation test remain open. Tasks 3 and 4 therefore remain
open, as do Linux browser/network isolation, unapproved product contracts, and
live external acceptance.

The complete browser-enabled gate then passed on the current checkout: **3079
passed, 5 skipped, 156 subtests passed, 29/29 journeys** in 324.23 seconds.
Three skips are the new Linux pidfd/`/proc` child-process tests and two are
existing supervisor `/proc` tests. The report binds HEAD
`d715e743cf176be3af52f40635703ea58270a555`, records
`working_tree_dirty=true`, matrix SHA-256
`2a9f73a02e1774b10c9d5803ace9d072f74f77ee58006a964cf0b91eae65923b`, and
keeps all four live checks at `not_run`. Evidence is under
`/tmp/agent-fleet-todo-continuation-20261008/`. This is local regression
evidence only; clean package, Linux runtime, and external acceptance evidence
remain outstanding.

The continued handoff audit found three local release/cleanup gaps: release
layout tests did not require the new pidfd helper in the tracked archive;
`stop_probe_loop` could read an unset `file_pid` under `set -u` when the PID
file was absent; and sleep-child cleanup hid helper failures. The release test
now requires `deploy/hk-web-process-control.py`, the installer initializes the
advisory PID value, and the Guardian logs failed child cleanup. Fresh focused
Guardian/deployment regression passed **65 tests with 3 expected Linux-only
skips**; Python compilation, shell syntax and whitespace checks passed. A
committed archive/startup check remains next, since `git archive` intentionally
omits untracked files.

After documentation was committed as `51dff02`, the final HEAD passed the full
Chrome gate again: **3079 passed, 5 skipped, 156 subtests, 29/29 journeys** in
323.76 seconds. Its report records `working_tree_dirty=false`, matrix SHA-256
`2a9f73a02e1774b10c9d5803ace9d072f74f77ee58006a964cf0b91eae65923b`, and 404
passing selector definitions. The evidence binds HEAD `51dff02`. The five skips
are macOS `/proc`/pidfd platform
tests, so the release validator correctly returns `junit_incomplete` rather
than treating this as Linux release evidence. The archive from this commit
contains `deploy/hk-web-process-control.py`; extraction, Python compileall for
`deploy`/`hub`/`tools`, and `hub.web.make_app()` import succeeded. Evidence is
under `/tmp/agent-fleet-todo-continuation-20261008/`; all four live checks remain
`not_run`.

The follow-up audit found one more local installer race: after an old probe
exited, cleanup could delete a new probe's PID file because it reused the
previously captured `file_pid` without rereading the advisory file. A failing
regression reproduced the missing recheck; cleanup now compares the current
file contents to the captured PID immediately before removal. The original
regression passes, and the focused Guardian/deployment/release-layout suite is
**101 passed, 3 expected Linux-only skips, 2 subtests**; compilation, shell
syntax, and whitespace checks pass. The committed release gate then passed on
`a7c936f`: **3080 passed, 5 skipped, 156 subtests, 29/29 journeys**. Its report
records `working_tree_dirty=false`, matrix SHA-256
`2a9f73a02e1774b10c9d5803ace9d072f74f77ee58006a964cf0b91eae65923b`, and 404
passing selector definitions. The five skips remain macOS `/proc`/pidfd tests,
so the release validator returns `junit_incomplete`; Linux runtime and all four
live checks remain unverified.

A further process audit found two remaining advisory-PID-file races: the stop
routine could delete a replacement instance's PID file after a re-read, and
startup polling could delete the newly launched instance's file while its
`/proc` identity was not yet visible. The stop path now leaves PID-file cleanup
to the next serialized start, and start removes the stale file once before
launch only. A failing source regression reproduced the polling deletion and
passes after removal. Focused Guardian/deployment/release-layout validation is
**101 passed, 3 expected Linux-only skips, 2 subtests**; Python compile, shell
syntax, and whitespace checks pass. Commit `c5081d1` then passed the complete
Chrome gate: **3080 passed, 5 skipped, 156 subtests, 29/29 journeys** in 364.43
seconds. The report records a clean checkout, matrix SHA-256
`2a9f73a02e1774b10c9d5803ace9d072f74f77ee58006a964cf0b91eae65923b`, and 404
passing selectors; all four external checks remain `not_run`. The archive
contains the pidfd helper, and extraction plus Python compile/import checks
pass. The release validator correctly rejects the five macOS Linux-only skips
as `junit_incomplete`; Linux/runtime evidence remains outstanding.

## Current-revision continuation verification (2026-10-08)

On clean code HEAD `14073662eaae21a4d1591d6af25ad2088b32072f`, Python 3.10
browser-enabled verification passed **3080 tests, 5 expected Darwin
`/proc`/pidfd skips, 156 subtests and 29/29 journeys** in 323.33 seconds. The
report binds the clean revision and matrix SHA-256
`2a9f73a02e1774b10c9d5803ace9d072f74f77ee58006a964cf0b91eae65923b`; all
four live checks remain `not_run`. Focused Guardian/deployment/release-layout
verification passed **101 tests, 3 expected Linux-only skips and 2 subtests**.
Python 3.10 compileall, shell syntax, whitespace, tracked release archive
contents, archive compileall and Hub app construction with a local-only test
token passed. The release evidence validator returned `junit_incomplete` for
the five platform skips, as required. Detailed paths and remaining acceptance
boundaries are recorded in `docs/testing/current-spec-audit.md`.

Commit `f3c92a5976d936067ef7e98434844859921a8b10` then added only this plan
checkpoint and the current-spec audit; no code or journey matrix changed after
the verified code revision. An attempt to push the private-history branch to
the public repository was rejected before any ref update. See the audit for the
exact guard result and the remaining publication prerequisite.

## Public-baseline snapshot candidate (2026-10-08)

The local `main` diff was applied in an isolated worktree based on the public
`origin/main` snapshot and committed as one local-only candidate,
`03569ba61a25d124ac994ca7321ddc9afef542ac`, with public `f6c35f9` as its parent.
Path/secret-pattern review found no credential or runtime-state paths; the only
high-entropy token-shaped match is an intentional fake GitHub token in a
redaction test. The clean candidate passed **3080 tests, 5 Darwin
`/proc`/pidfd skips, 156 subtests and 29/29 journeys**. Its tracked release
archive contains the pidfd helper and passes extracted compilation and Hub app
construction. The evidence validator correctly returns `junit_incomplete` for
the platform skips. This candidate has not been published; owner approval and
Ubuntu CI remain required before treating it as a public or Linux-verified
release.

This checkpoint does not close Task 3/4. The current revision still needs its
Ubuntu CI evidence and Linux runtime/network-isolation tests. Browser observation,
taking control, credentials, Browserbase and notifications remain unapproved
product contracts; MODEL-LIVE, MAIL-LIVE, NODE-LIVE, DEPLOY-LIVE and the named
backup/restore pilots require their actual environments and evidence.

## Ubuntu CI candidate verification (2026-10-08)

The public-baseline snapshot was published on `codex/public-ci-candidate` as
`a0b5372425a48a340f532203d044fc135d8a5ef1`, with public `f6c35f9` as its parent.
GitHub Actions run [37695537251](https://github.com/dengyie/agent-fleet/actions/runs/37695537251)
passed on Ubuntu 24.04 with Python 3.10: **3085 passed, zero skips, 156
subtests, 29 journeys, 404 selectors, and 3085 JUnit cases**. The release
evidence verifier passed and the evidence artifact is ID `11514843558`. One
existing invalid-escape deprecation warning remains in
`tools/probe/discovery.py`. The workflow skipped `package` and `deploy` due to
its conditions. This is Linux test evidence for the public-baseline candidate,
not CI evidence for the final development revision or live production
acceptance; re-run against the final development snapshot after integration.
