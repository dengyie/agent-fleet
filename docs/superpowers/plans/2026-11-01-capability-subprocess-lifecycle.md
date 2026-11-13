# Capability Subprocess Lifecycle Implementation Plan

> **For agentic workers:** Use `executing-plans` inline in the existing isolated worktree. No delegation, commits, deployment, installation, cleanup or Obsidian edits.

**Goal:** Verify automatic admission, original Run execution and independent Node process journals without manually manufacturing an unknown execution.

**Architecture:** Reuse the existing lifecycle Hub assembly and bounded fixed-capability Node canary. The original worker waiter launches one Node child per operation over signed loopback HTTP. The child retains its own workspace/journal, while the Hub observes only normal receipts. This is an explicitly selected local probe, outside default test discovery and required journeys.

**Tech Stack:** Existing Python 3.10+, Flask, SQLite, cryptography and pytest; no new dependency.

## Global constraints

- Authority: [capability contract §6.4–6.5](../specs/2026-10-20-artifact-capability-integration-contract.md#64-hub-固定能力-run-与-command-派发子集), [WF-05 acceptance](../../testing/workflow-platform-acceptance.md), and [original lifecycle plan](2026-10-31-capability-workflow-lifecycle.md).
- Keep the original coordinator, Run worker, delivery service, NodeRuntime and both SQLite journals. No explicit admission, `Fixture.isolate_write`, direct terminal-row mutation or substitute executor.
- Declare both write/reconcile_before_retry and digest/read_only before admission. Unknown follows a completed fixed write and lost completion; original token/generation/binding, unknown budget and held capacity remain.
- Success releases capacity after terminal observation; absent required artifacts remain unavailable, with no fabricated validation or acceptance.
- Repeated command reads after process restart must use terminal journals with zero backend I/O. Child stdin contains credentials transiently; persist only bounded reports and command metadata without input values.
- Only local child processes in this slice; no automatic SSH or external provider access. Existing remote deletion and full-gate permission boundaries remain independent. UTF-8 without BOM.
- Identity queries use existing indexes; fixed-size observations are bounded, hashing O(n) in a small fixture file. Reuse concrete test helpers; no production port/facade/schema change.

## Task 1: Extend the existing Node canary

**Modify:** `tests/fixtures/workflow/capability_node_canary.py`.

**Interfaces:** Preserve prepare/write/postcheck defaults; write accepts optional boolean `interrupt` (default true); add `replay` accepting the original signed Command. Every report includes actual process ID.

- [x] Count and persist exactly one actual write for success and interrupted completion. Keep the default interrupted behavior used by existing Windows/Linux probes.
- [x] Add a restart-only replay operation: replace write/digest with rejecting counters, read the persisted original journal, handle the signed original command twice, poll the original outbox and assert zero I/O and no commands.
- [x] Keep the 64 KiB input/output boundary and close every runtime even on assertion failure.

## Task 2: Explicit local subprocess probe

**Create:** `tests/fixtures/workflow/capability_lifecycle_probe.py`.

**Interfaces:** Reuse `_setup`, `_admit_and_create_run`, `FixedProvider`, `Lifecycle.lifecycle_snapshot`, original Workflow control and the existing local `_worker`. Four cases: success and unknown with matched/mismatch/read_failed postcheck. Require an absolute, pre-created `FLEET_CAPABILITY_TEST_EVIDENCE` directory.

- [x] Launch prepare, write, replay/postcheck in separate processes, verify every reported PID differs from the Hub and retain hostname/platform/runtime facts.
- [x] Wait through the original worker boundary; only read Command rows after actual child receipt submission. Assert one Run/dispatch/binding and expected provider call counts.
- [x] Observe success release/unavailable outputs; for unknown verify retry rejection, signed helper binding, repeat HTTP idempotency, terminal audit and unchanged original lifecycle snapshot. Advance ordinary coordinator ticks and prove no new Run or Command.
- [x] Run the complete explicit probe on installed Python 3.10 and 3.13 with separate new basetemp/evidence directories and JUnit/logs. Run the old canary probe plus original lifecycle regression to protect backward compatibility.

## Task 3: Record precise evidence

**Modify:** this plan and `docs/testing/README.md`.

- [x] Compile the changed Python files on both installed runtimes; inspect JUnit counts and complete command exits; check UTF-8/BOM, document links and `git diff --check`.
- [x] Record exact counts and evidence paths. Keep this local process result separate from the prepared 900-file Linux freeze, prior full gate, real-host automatic lifecycle, full WF-04/05 and seven external checks. Do not register the opt-in probe as a required selector.


## Verified local result — 2026-11-01

Fresh final evidence: `/Users/mango/.cache/agent-fleet/verification/capability-child-final-20261101-gxvs0ach`. The explicit subprocess probe, existing local capability/postcheck probe and original lifecycle module passed **14 cases** on each installed Python 3.10.21 and 3.13.15 environment. The ten-module coordinator/dispatch/Node/worker/postcheck regression set passed **400 cases** on each runtime. All four JUnit reports contain zero failures, errors and skips. Both complete `compileall -q hub tools tests` commands exited zero. All 902 visible source files had identical SHA-256 values before/after the verification commands; the evidence repository was not used as a development checkout.

The new four-case probe observes an actual child Node process for prepare, write, postcheck and terminal replay. It verifies one original Run/dispatch/binding, exact signed helper context, one backend write, independent digest only, repeat-query audit idempotency, rejected unknown retry, unchanged binding/token/generation and unknown/held facts. Capacity is held before ordinary coordinator observation; success releases it with the canonical evidence digest tied to the original token, binding and observation. Missing required artifacts still leave outputs unavailable and validation/acceptance incomplete. Ordinary subsequent ticks create no replacement Run or Command.

Red evidence is retained at `/Users/mango/.cache/agent-fleet/verification/capability-child-red-20261101-k3bvnqic` (old Node report lacked PID). The added release-digest assertion initially omitted token/binding fields; its failed report and read-only database proof are retained at `/Users/mango/.cache/agent-fleet/verification/capability-child-release-check-20261101-66lqr_un`. The original product digest matched the canonical bound evidence; only the new test calculation changed. The fresh final run above includes the corrected assertion. No production file changed in this slice.

This opt-in local process slice is not registered in the required matrix. It does not prove two physical hosts, live model/CLI, production isolation or full WF-04/WF-05. Seven external checks remain `not_run` and all product gates remain default-off. The prepared 900-file Linux source `decb0966f39353c65b5efc62cf6970e3ab05dbf2` predates these fixture/document changes; its full gate has not started. The two previously authorized cleanup targets are already absent. The separate retained rerun-fixture directory requires its own deletion confirmation before that cleanup; no additional remote path was removed here.

To reproduce the local probe, use a new absolute evidence directory and a nonexistent basetemp:

```bash
FLEET_CAPABILITY_TEST_EVIDENCE="$EVIDENCE" PYTHONPATH=. python -m pytest tests/fixtures/workflow/capability_lifecycle_probe.py -q --tb=short -rs -p no:cacheprovider --junitxml="$EVIDENCE/results.xml" --basetemp="$EVIDENCE/fixtures"
```

The caller must create `EVIDENCE` privately before invoking this explicit test. It uses the selected Python runtime only, no SSH or external provider.

## Task 4: Authorized native automatic lifecycle

This task extends the completed local slice above. The user's existing authorization explicitly covers fixed workspace writes and independent read-only digest postchecks in new private temporary directories on home-win and gz-cvm, preserving execution facts and evidence without installation or global settings/services. The automatic coordinator/worker uses those same bounded fixed operations and a deterministic provider; it does not authorize a live model/CLI, production rollout or deletion. The preceding local-only constraints describe Tasks 1–3.

**Modify:** `tests/fixtures/workflow/capability_lifecycle_probe.py`, this plan and the testing/progress indexes after verified results.

**Interfaces:** Reuse the existing opt-in platform environment and `_worker` SSH adapter. Parametrize target × success/matched/mismatch/read_failed. Pin a fresh immutable route registration to the actual authenticated offer before the first coordinator tick, including OS, architecture, software and 200 bytes disk. All later execution and observations use the original services.

- [x] Extend the explicit probe to local/windows/linux; keep the default local, verify actual host/platform/PID facts, and retain distinct evidence names for every target/case.
- [x] Re-run the complete local probe and old fixtures on both installed runtimes, with original lifecycle and relevant regressions; inspect complete exits/JUnit and compile all source.
- [x] Freeze all current visible source in an independent private evidence checkout, retain development branch/HEAD/status and every file hash. Prepare exact new native checkouts from a source bundle smaller than 10 MB; use only installed runtimes and recheck capacity before any upload.
- [x] Run all eight actual-platform cases once on the frozen source. Preserve original signed Commands, normal coordinator/worker receipts, both Node journals, all stdout/stderr/JUnit and repeat/restart counters. No direct terminal-state writes or manual admission.
- [x] Independently audit original Hub state and remote journals, verify local and both native source hashes before/after and clean checkouts, and copy original native evidence with exact sizes/hashes. Record only the proven fixed automatic lifecycle slice, keeping full WF-04/05 and seven external checks open.


## Verified native automatic lifecycle — 2026-11-01

Independent frozen revision `719d8e7007ded5ef1ebe6322a4715ac30521e7da`, **902 files**, passed all eight original automatic lifecycle cases on actual home-win Windows/Python 3.12.7 and gz-cvm Linux/Python 3.13.16. Pytest reports **8 passed, 0 failures/errors/skips, 105.74s**; controller elapsed 106.04s. Each actual platform covers success, unknown+matched, unknown+mismatch and unknown+read_failed. The coordinator admits normally, creates the original Run, and the original worker submits a signed command to the independent native Node. No `Fixture.isolate_write`, explicit admission or terminal-row mutation is used. The fixed provider does not call an external model or commercial CLI.

Before the first coordinator tick the trusted route is fixed to the authenticated native OS/architecture/software offer and 200-byte disk requirement; the original binding retains that exact report digest. Independent read-only checks of all eight Hub databases verify one original Run/dispatch/binding/generation, normal receipts, exact write/helper retry classes, one backend write, correct success release evidence and unknown/held preservation. Native journal/file audits independently verify the terminal records and retained original bytes; repeat/restart I/O remains zero. Success still has outputs_unavailable, no artifacts/evidence and no completed validation/acceptance.

Evidence root: `/Users/mango/.cache/agent-fleet/verification/capability-native-auto-20261101-7sx6gzta`. `verification-summary.json`, `hub-audit.json`, source maps, exact commands/JUnit and native copy manifests are retained. Both native checkouts and the frozen local checkout match all 902 hashes before/after and remain clean. **42 copied native files / 363,814 bytes** match remote size/SHA-256; both complete archives are independently checked. Frozen-source related regressions are **400 passed** on each installed local Python 3.10/3.13, complete compileall exits 0; changed explicit/legacy/lifecycle probes separately pass **14 cases** per runtime.

Windows source preparation exceeded the initial SSH observation timeout. A fresh process inventory confirmed the original preparation process had exited; a read-only audit of the same checkout proved every original hash/revision and clean status. The checkout was not recreated and the native test was run only once. Temporary preparation script quoting failures are retained in the evidence history; neither product source nor acceptance assertions changed.

This closes the fixed actual-host automatic capability lifecycle slice. Full WF-04/WF-05, live model/CLI, production isolation and seven external checks remain open; gates remain default-off. The 900-file preparation is superseded for the next full gate by this 902-file source with identical production code/matrix and additional fixture/doc coverage.

## Task 5: Same-source strict Linux full gate

- [x] Receive the user's explicit confirmation for `/tmp/afcg-rerun-7_12st95/evidence/controlled-full/fixtures`; recheck complete local archive/gzip and all 42,072 remote entries, exact directory identity/ancestors/mounts/process references. Delete only that directory in one command; verify all outside files, permissions and inode identities unchanged. Preserve the complete local archive.
- [x] Fresh preflight: **2,899,787,776 bytes / 5,703,178 inodes** free, no conflicting full test process, all 902 hashes unchanged and clean checkout. The capacity budget includes the previous 2,008,150,016-byte fixture allocation plus 512 MiB headroom.
- [x] Start the original strict command with only its evidence RUN path changed, on the same 902-file freeze and original installed Python/Node/Chromium/Playwright/sandbox environment. The original process PID is **3699066**; observe that handle and start ticks, never restart on an observation timeout.
- [x] Inspect the terminal command, full JUnit, all required journeys and PNGs. Run the original remote release verifier, copy exact evidence with size/hash checks, and re-run the original verifier against the copied evidence and frozen local checkout. Record only actual results; a live process is not a passing gate.

Authorized deletion evidence: `/Users/mango/.cache/agent-fleet/verification/lifecycle-full-20261101-fdeguqmp/authorized-recovery-asam9fwe`. The old fixture directory is absent; all outside evidence remains. The complete strict run and its live/terminal records are in `/tmp/afla-7sx6gzta/evidence/controlled-full` and the local native evidence root above.

### First strict terminal result — 2026-11-01

The original process completed once, exit **1**, elapsed **1596.40s**. Pytest reports **5848 passed, 1 failed, 197 passing subtests, zero errors/skips**; JUnit has 5849 explicit testcase elements and summary tests=6046 including the subtests. All 42 required journeys have passed selectors, but overall `ci_status=failed` correctly preserves the non-selector failure. All 44 browser PNGs were produced; all 902 source hashes remain unchanged and the frozen checkout is clean. This is a failed full gate and cannot replace the preceding 5845-pass historical freeze.

The sole failure is `tests/test_frontend_browser.py::test_assistant_auth_in_real_browser`, waiting 5000ms for the login heading after a repeated protected-route redirect. Original terminal command/log/JUnit/journeys are copied to `full-terminal-original/` under the evidence root; the remote screenshots remain and the original fixtures were subsequently archived and removed under exact-path authorization. An instrumentation-only repeat of the same assertion also fails, with local script/style/config requests reporting `net::ERR_INSUFFICIENT_RESOURCES`, empty page text and no page exception. A separate process snapshot shows browser file counts far below their 524288 limit; host available memory and pressure do not establish memory exhaustion. This was the initial investigation checkpoint; the established root cause and verified capacity recovery are recorded below. No timeout, assertion, selector, browser version or product code changed.

### Root cause, authorized capacity recovery and fresh rerun — 2026-11-01

The syscall trace from `auth-syscall-enospc/attempt-1/syscall.log` records **13** Chromium `fallocate(fd, 0, 0, 2097152) = -1 ENOSPC` results. At the same failing assertion, the filesystem snapshot reports only **2,641,920 bytes** available on `/tmp`; the renderer already held hundreds of deleted 2 MiB `/tmp/.org.chromium.Chromium.*` backing files. Required local scripts, styles, configuration, and brand assets then failed with `net::ERR_INSUFFICIENT_RESOURCES`. Host memory availability, cgroup OOM counters, and the process file-descriptor limit do not support memory or fd exhaustion as the cause. Setting only `TMPDIR=/dev/shm` also failed because Chromium's default `--disable-dev-shm-usage` still directs its backing files to `/tmp`.

The complete 42,107-entry pytest fixture tree was archived and byte/mode/hardlink verified before the user confirmed deletion of the exact path `/tmp/afla-7sx6gzta/evidence/controlled-full/fixtures`. A fresh remote manifest and device/inode, ancestor, mount, process-reference, and outside-tree snapshot were checked immediately before the one-path removal. The tree became absent; every outside entry retained its inode/mode and every regular file retained its size/SHA-256. Available `/tmp` space increased from **2,266,075,136** to **4,281,339,904 bytes**. The complete local archive remains at `failed-retry-fixtures-archive/fixtures.tar.gz` with SHA-256 `342891fbb368949c80b8357bb25e1b7635292277084bf39834dab5f72436e87d`.

Using the same 902-file frozen checkout, exact authentication test, original 5-second assertion timeout, browser/Playwright versions, and sandbox, the focused test passed in **23.52s**; the checkout remained clean and all source hashes matched. The original strict full command then completed once in a separate evidence/basetemp root with unchanged test, assertion, timeout, selector, runtime, and sandbox: revision `719d8e7007ded5ef1ebe6322a4715ac30521e7da`, PID `3914193`, start ticks `204085717`, initial free space **4,276,719,616 bytes**; exit **0** after **1634.29s**, with **2,166,775,808 bytes** still available. Pytest reports **5849 passed, 197 subtests passed, zero failures/errors/skips**; JUnit has 5849 explicit cases and summary tests=6046. All **42/42 journeys** passed with **1751 selectors**, all **44 PNGs** passed the original verifier, and all seven external checks remain `not_run`. Both original remote and copied local release verifiers exit **0**. All 902 source hashes remain identical and the checkout is clean. Evidence root: `/Users/mango/.cache/agent-fleet/verification/capability-native-auto-20261101-7sx6gzta/full-rerun-1-copy`; 50 copied files / **7,933,413 bytes** match, archive **5,518,851 bytes**, SHA-256 `05e9a6a1d75d7f70d4b9baae63d154c3c660bf53b72c3d5e3e35582444861d99`. The original failed report remains separate and counts are not combined. Full WF-04/05, real model/commercial CLI, production isolation, and seven external checks remain open.