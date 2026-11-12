# Postcheck Current-source Full Gate Verification Plan

> **For agentic workers:** Execute inline with `executing-plans` in the existing isolated worktree. Keep the overall platform goal active. No agents, development commits, merge, push, release, deployment, installation, global settings/service changes or Obsidian edits. Cleanup is limited to the two exact fixture directories separately confirmed below; retain all other source/evidence. Independent evidence Git snapshots supply verification identities only.

**Goal:** Verify the complete current source, including the atomic postcheck fix and eight newly registered selectors, through the original strict Linux journey/JUnit/browser gate.

**Architecture:** Freeze a complete Git-visible source copy after the native evidence documentation update. Reuse installed Linux Python/Node/Playwright/Chromium/bubblewrap dependencies in a fresh private test root. The original test collector and release verifier decide status; no alternate gate or product code is introduced.

**Tech Stack:** Existing Python 3.10.21/3.13.15 locally, Python 3.13.16 on gz-cvm, Node 22.21.1, Playwright 1.63.0, Chromium 153.0.8010.12 and test-owned bubblewrap/libseccomp.

**Recorded scope:** The completed invocation below is the immutable **pre-terminal-guard-baseline**. A later original-owner/unknown read bug requires a separate corrected freeze and native/full run under the [terminal guard plan](2026-10-31-postcheck-terminal-guard.md). The baseline's documentation-only comparison to the native source applies only to that earlier freeze.

**Current verification:** The corrected revision `85488105cd2d3b610fcbfa18fc2273bd02618db1` subsequently completes the unchanged original full gate with 5845 passed, zero failures/errors/skips, and both original verifiers passing. The earlier baseline, storage failure and pending-authorization paragraphs below are historical checkpoints; the final section records the authorized recovery and verified rerun.

## Global constraints

- Authority: [roadmap release checkpoints](2026-10-20-workflow-platform-roadmap.md#9-发布与回退检查点), [original full test command](../../testing/README.md), [atomic audit plan](2026-10-30-postcheck-atomic-audit.md) and [native capability evidence](2026-10-30-capability-platform-postcheck.md#native-frozen-verification).
- The existing user-authorized Linux full-repository test scope persists. This verification uses only gz-cvm and installed runtimes in a new private root; no new model/CLI/MCP/Muse/production operation is authorized by it.
- Existing Windows/Linux native matrix revision `16f10ebd630d38fa53a839ce9d2c432719014120` stays immutable. New documentation changes must be explicitly identified, and every executable/test/matrix byte must match that native freeze.
- Full tests select `tests` without exclusions and retain `--require-journeys`, original journey matrix and JUnit. All testcase setup/call/teardown and all required journeys must pass, with zero skip/xfail/xpass. External checks remain `not_run`.
- Exact runtime paths, versions, process PID/start ticks, commands, environment, timestamps and source hashes are retained. UTC evidence time; UTF-8 without BOM. One original pytest child is bounded to 3600 seconds with its lifecycle owned by the foreground driver.
- Each invocation uses a previously nonexistent private basetemp. Do not reuse pytest basetemp, weaken selectors, patch a failing fixture without diagnosis, or concatenate counts from independent runs.
- Source hashes use bounded per-file reads and indexed maps; copying/hashing is O(n) in source bytes, with 64 KiB stream blocks. Existing verification code is reused without creating an I/O facade or new state engine.

## Task 1: Full source and runtime identity

**Files:** This plan and the existing development/testing indexes; evidence-only provenance, bundle, audit scripts and command logs outside the repository.

**Interfaces:** Complete `provenance.json` source map plus exact revision/bundle size/SHA-256; the existing `audit-source.py ROOT prepare|after GIT` checks every file and clean checkout.

- [x] Freeze all Git-visible files from `codex/workflow-platform-docs` into an independent evidence repository. Verify every copied digest and explicitly list only documentation changes versus the native freeze. Keep primary worktree and development Git history untouched.
- [x] Compile complete `hub`, `tools` and `tests` from the frozen checkout independently with the installed local Python 3.10 and 3.13; retain exact commands, exits and before/after hashes. Verify current documentation links, no BOM, exact NODE-01/WF-08 registration and unchanged external checks.
- [x] Prepare private `/tmp/afpc-full-j20pyod1` on gz-cvm, refusing an existing root. Upload only the small source bundle/provenance/audit/driver files. Verify original source hashes/revision and live installed runtime versions before any test run.

Evidence root: `/Users/mango/.cache/agent-fleet/verification/postcheck-full-20261031-j20pyod1`.

## Task 2: Original strict full gate

**Files:** Evidence-only complete driver copied from the retained `run-controlled-full.py`; original `tests/conftest.py`, journey matrix and all tests remain unchanged.

**Interfaces:** Original full command writes `results.xml`, `journeys.json`, `full.log`, browser PNGs and exact `full-command.json` under `/tmp/afpc-full-j20pyod1/evidence/controlled-full`.

- [x] Reuse the original complete driver with only its fixed RUN path updated and fresh `--basetemp` added. Preserve process-scoped installed runtime environment, removed DAC override capabilities and NoNewPrivs.
- [x] Run the original full command from `/tmp/afpc-full-j20pyod1/checkout`:

```bash
setpriv --bounding-set=-dac_override,-dac_read_search --no-new-privs /tmp/agent-fleet-linux-control-20261028.6uddg5ud/venv/bin/python -m pytest tests -q --require-journeys --journey-report=/tmp/afpc-full-j20pyod1/evidence/controlled-full/journeys.json --junitxml=/tmp/afpc-full-j20pyod1/evidence/controlled-full/results.xml --basetemp=/tmp/afpc-full-j20pyod1/evidence/controlled-full/fixtures --tb=short -rs
```

Required environment remains process-scoped: `LC_ALL=C`, `PYTHONPATH=.`, PATH=`/tmp/agent-fleet-linux-control-20261028.6uddg5ud/runtime/bubblewrap-local/usr/bin:/tmp/agent-fleet-linux-control-20261028.6uddg5ud/runtime/bin:/bin:/usr/bin`, `FLEET_PLAYWRIGHT_MODULE=/tmp/agent-fleet-linux-control-20261028.6uddg5ud/runtime/node_modules/playwright`, `PLAYWRIGHT_BROWSERS_PATH=/tmp/agent-fleet-linux-control-20261028.6uddg5ud/runtime/browsers`, `FLEET_BROWSER_CHANNEL=chromium`, `FLEET_SCREENSHOTS=/tmp/afpc-full-j20pyod1/evidence/controlled-full/browser`.

- [x] Read full exit/report/log evidence. If a failure occurs, preserve it, trace its cause and fix only the supported cause before a fresh source freeze/run. No success inference from partial dots or an old full report.

## Task 3: Original verifier, copies and truthful status

**Files:** Update this plan, implementation progress, roadmap and development/testing indexes only after original verified results.

**Interfaces:** Existing `python -m tools.testing.release_evidence --evidence DIR --revision SHA --matrix PATH --checkout PATH` validates this exact clean source, journey/JUnit and PNG set.

- [x] Run the original verifier on gz-cvm using the revision in the new `provenance.json`. Require exit 0, 42 complete journeys, all required selectors, complete JUnit and valid PNGs. Keep Python 3.13 Linux and CI Python 3.10 evidence boundaries explicit.
- [x] Audit every source file after the run, copy exact reports/logs/PNGs and their bounded manifest, verify every copied size/hash and every PNG through the original parser, then run the same verifier against the copied evidence and frozen local checkout.
- [x] Record this new full gate's independent revision, actual counts, runtime, exact source/report digests and remaining WF-04/05/NODE-LIVE/model/CLI/production/external scope. Do not sum its counts with the native twelve-case matrix or prior gates, open gates, commit development files or mark the complete platform achieved.

## Verified pre-guard baseline — 2026-10-31

Source `adf137251e21acbad036a3e51221d8cf6805731d`, **896 files**, completes the original strict Linux command: **5833 passed / 197 passing subtests / 0 failures/errors/skips**, 1583.05 seconds. All 42 journeys and 44 PNGs pass the original release verifier on gz-cvm and against the verified local copy. Every source digest is unchanged and both checkouts remain clean. Local full-source compileall exits 0 independently on Python 3.10.21 and 3.13.15.

`source.bundle` is 3,233,636 bytes / SHA-256 `844f550fd6d42ae6187e64c24599339e6c5cd82658f604f2c1a4a7842793a516`. The 50 copied reports/logs/PNGs total 7,943,776 bytes; archive and every member size/hash match. Local evidence is `linux-evidence/controlled-full/`, with exact copy/verifier/source checks in `baseline-preservation.json` under the evidence root above. Large pytest fixtures remained on gz-cvm at that preservation checkpoint; their later complete verified archive is recorded under the corrected-source capacity recovery below. The remote fixtures have not been deleted.

This run precedes the [terminal original-command guard](2026-10-31-postcheck-terminal-guard.md). Its scope is `pre-terminal-guard-baseline`; it cannot verify the later code/test/matrix change. Corrected native verification passes under that plan; corrected full verification remains open after the storage-exhausted invocation below. Seven external checks remain `not_run`, all gates default false, and full WF-04/05/NODE-LIVE/model/CLI/production acceptance remains open.

## Corrected-source full attempt — 2026-10-31

The exact corrected revision `85488105cd2d3b610fcbfa18fc2273bd02618db1`, 898 files, is independently cloned into `/tmp/afcg-full-t0mae74m`; its original strict command uses fresh `evidence/controlled-full/fixtures` and the same bounded foreground driver, installed Python 3.13.16/Node 22.21.1/Playwright 1.63.0/Chromium 153 runtime. Only the driver's fixed RUN path changes. The same revision's original twelve-case native capability matrix passes independently.

This full invocation aborts after 838.98 seconds with exit 120, no final JUnit or journey report. Before/after source hashes match and checkout is clean. `/dev/vda1` is 100% full with 28,672 bytes free; inode allocation is 100% full with 100 free. The partial full log stops at 4096 bytes, so no passing count, final failure count or per-failure diagnosis is inferred. The storage condition makes this invocation invalid and requires a fresh original rerun after sufficient disk/inodes are available.

Local raw evidence is `/Users/mango/.cache/agent-fleet/verification/postcheck-guard-20261031-t0mae74m/full/`: `failed-full-command.json`, `failed-full.log`, `failure-diagnosis.json`, disk/mount/inode inventories and fixture sizes. Reports, source and both remote fixture trees remain retained; no cleanup/deletion, global runtime/service change or product-code patch is performed. The original release verifier cannot pass without completed JUnit/journey/PNG evidence and is not bypassed. The full gate remains unchecked in the [terminal guard plan](2026-10-31-postcheck-terminal-guard.md#task-3-current-source-verification-and-scope).

## Verified archives and pending capacity recovery — 2026-10-31

The [complete preservation checkpoint](2026-10-31-postcheck-terminal-guard.md#verified-fixture-preservation-and-pending-capacity-recovery--2026-10-31) now verifies both full fixture archives under the corrected local evidence root's `full/baseline-fixtures-archive/` and `full/failed-fixtures-archive/`: 52,079 named members, 79,311,041 compressed bytes, exact regular content/modes/hardlink identities/symlink targets/FIFO metadata and unchanged remote manifests. Independent gzip/digest checks pass. The first evidence-verifier hardlink assertion failure remains preserved and is corrected only in the evidence script.

`full/capacity-recovery-proposal.md` and `.json` list the two exact remote directories, each verified archive, 3,076,734,976 allocated bytes / 52,075 du inode entries, and a read-only process/reference audit. At this historical proposal checkpoint no deletion is authorized or performed. AGENTS.md requires explicit confirmation for each listed directory and a separate command for each confirmed deletion. Both confirmations, rechecks, separate deletions and actual full verification are subsequently recorded below. All reports, source, journals, PNGs and runtimes remain retained; this archive checkpoint itself supplies no test count or release result.

## Verified corrected-source rerun - 2026-10-31

The user separately confirmed deletion of `/tmp/afpc-full-j20pyod1/evidence/controlled-full/fixtures` and `/tmp/afcg-full-t0mae74m/evidence/controlled-full/fixtures`. Fresh complete manifests, archive size/SHA-256/gzip, directory identity, mount and live-reference checks passed. Each directory was deleted in its own command, exit 0. Both exact roots are absent; every file outside each fixture subtree under its old run root retains its bytes, mode and inode. Local complete archives remain retained. `full/authorized-recovery-k5a1cb98/recovery-summary.json` records **6,126,874,624 free bytes / 12,011,729 free inodes** at this recovery checkpoint, before the rerun. No broader cleanup permission is inferred.

Unchanged corrected revision `85488105cd2d3b610fcbfa18fc2273bd02618db1`, **898 files**, ran once in new `/tmp/afcg-rerun-7_12st95` and a previously nonexistent basetemp. It completed the original strict command with **5845 passed / 197 passing subtests / 0 failures/errors/skips**, pytest **1730.57s**, controller **1732.42s**, exit 0. Python 3.13.16, Node 22.21.1, Playwright 1.63.0, Chromium 153.0.8010.12, sandbox environment, tests and assertions are unchanged except fixed evidence paths.

The original remote and copied-local release verifiers both exited 0: **42 journeys / 1747 selectors / 5845 JUnit cases / 44 PNGs**. All 898 source hashes match before/after; frozen checkouts stay clean. All **50 copied files / 7,940,720 bytes** match remote sizes/hashes; archive SHA-256 is `3eaeec70306e25075202b4a93a2ace343af1d61d2eb63dae8dd2bffcddcf8aef`. Exact commands, audits, logs, JUnit/journeys/PNGs and `corrected-preservation.json` are retained under `/Users/mango/.cache/agent-fleet/verification/postcheck-guard-20261031-t0mae74m/full/rerun-prepared-7_12st95/`. The local shell wrapper's read-only variable error is retained; direct original verifier execution independently exits 0 without rerunning pytest or changing assertions.

This later result supersedes the open corrected-source status at the historical checkpoints above and closes the [terminal guard full-gate item](2026-10-31-postcheck-terminal-guard.md#task-3-current-source-verification-and-scope). Baseline, failed invocation and native matrix retain independent scopes/counts. Full WF-04/05, NODE-LIVE, real model/CLI, production isolation and external acceptance remain open; seven external checks stay `not_run`, all gates default false and the overall platform goal stays active.
