# Real Workflow CLI Pilot Implementation Plan

> **For agentic workers:** Execute inline with `executing-plans`; do not delegate or spawn agents. Preserve the complete development ledger and all failed evidence.

**Goal:** Exercise the current coding-delivery Workflow with a real authenticated commercial CLI developer and a separate real model review invocation, followed by the original sandboxed fixed tests and an honest awaiting-human-acceptance boundary.

**Architecture:** Reuse the existing loopback Cookie HTTP, WorkflowCoordinator, LegacyTaskBridge, Runner, CommandReviewAdapter and FixedTestRunner chain. A manual acceptance harness creates a disposable repository and private Hub; the CLI bridge confines each invocation to its assigned source and private runtime. No production configuration or account is changed.

**Tech Stack:** Existing Python runtime/test dependencies, Flask/SQLite WAL, installed Codex CLI, macOS sandbox-exec, original pytest recipe.

## Global Constraints

- Work only in `codex/functional-root-fixes`; preserve accumulated edits and the protected primary worktree.
- UTF-8 without BOM; source remains Python 3.10 compatible.
- Manual harness belongs in `tests/manual/`, outside automatic test collection and production package boundaries.
- Real model calls are explicitly opt-in. Do not add downloads, change global CLI configuration or print credentials.
- Use the existing selected provider/model, with separate developer/reviewer executor identities and fresh CLI sessions. A same-model separate-session review is identified honestly.
- Snapshot only the provider settings needed by the installed CLI into a private 0700 runtime; copied secrets stay out of Git and published evidence.
- Preserve each attempt and its failure. A timeout is not permission to restart a live invocation.
- The agent must not record human acceptance on the user's behalf. Passing automation ends in `awaiting_acceptance`.
- This pilot does not replace MODEL-LIVE, production isolation, WF-04/05, the complete Linux/browser gate or the remaining global scope.

## Task 1: Prepare bounded manual acceptance

**Files:** Create `tests/manual/workflow_live_pilot.py` and `tests/manual/workflow_live_codex.py`.

**Consumes:** `test_workflow_e2e.CookieClient`, `hub_server`; original platform application/repositories and Runner; `test_workflow_sandbox.isolated_test_backend`.

**Produces:** A private `result.json`, original Hub/Runner state, per-invocation event files, downloaded exact-hash artifacts and immutable seed-test fingerprints.

- [x] Register the fixed `coding-delivery@1` template and developer/review/test targets under a generated private account/workspace. Pin adapter configuration digests and the installed CLI/model identity.
- [x] Seed `TASK.md` and independent fixed pytest cases for `parse_port`; developer may create only `ports.py`. Compare seed bytes after source export and reject tampering.
- [x] Run the real developer through the original Runner task. The trusted bridge records one invocation and launches the existing CLI with no MCP servers, hooks or delegation and a bounded process lifetime.
- [x] Run the separate model reviewer through CommandReviewAdapter, with source read-only and scratch writable under a real sandbox-exec launcher. Return the original strict report schema; never synthesize a passing verdict.
- [x] Run the original fixed pytest recipe inside the existing real sandbox. Download all declared outputs and verify bytes, SHA-256, common subject and test backend evidence.

## Task 2: Execute and record real evidence

**Files:** Modify this plan, `docs/platform/development-completion.md` and `docs/testing/workflow-platform-acceptance.md`.

**Consumes:** The original private evidence directory and actual process results.

**Produces:** A bounded pilot result tied to source hashes and exact configured CLI/model, with explicit acceptance limits.

- [x] Validate both harness sources with Python 3.10 AST and Ruff; run existing E2E/sandbox/boundary tests before live calls.
- [x] Execute `python3 tests/manual/workflow_live_pilot.py --evidence-root /private/new/path --run-live`; retain process handle and stdout/stderr. Poll the same handle until terminal.
- [x] Inspect developer/reviewer invocation identity, real event records, immutable artifact hashes, strict review, JUnit, workflow state and restored HTTP session reads. Record failed or incomplete stages without promoting them to success.
- [x] Leave successful automation at `awaiting_acceptance`; save digest/revision and exact artifacts for human review, without calling the acceptance endpoint.
- [x] Update the canonical repository ledger and acceptance matrix with exact scope and evidence. Keep the overall goal open.

## Verification criteria

`result.json` must identify the input source snapshot, installed CLI version/hash, configured provider/model, two separate real CLI invocation records, original Workflow/Run identifiers and one shared CodeRevision subject. Success requires an actual strict review verdict of `passed`, an original sandbox test report and nonempty passing JUnit, byte-for-byte preserved seed requirements/tests, hash-verified HTTP artifact downloads and the original Workflow state `awaiting_acceptance`. No general release-ready claim follows from this manual pilot.

## Task 3: Restore the real task in the browser

**Files:** Create `tests/manual/workflow_live_ui.py`; preserve each UI attempt in its own private evidence directory.

**Consumes:** The original pilot database, private operator credentials and workflow identity; existing Playwright module and the user's already-running Chrome CDP listener.

**Produces:** Desktop/mobile screenshots, five original download hashes and `ui-result.json`; no model invocation, worker or Workflow write.

- [x] Restore the original private Hub with the saved key material and current frontend. Register the same catalog; set only the private account's workspace display default.
- [x] Attach to existing Chrome9222, create and close only a disposable context. Login with an actual Cookie, open the original workflow, download all five outputs through rendered links and verify their bytes/hash.
- [x] Reload at390px, verify validation passed and awaiting-human state, capture both views and inspect screenshots. Block all Workflow mutation requests and assert none occurred.
- [x] Verify logout revocation and independently query the original workflow after browser completion. Revision22, delivery digest and pending acceptance must remain identical.

## Verified results — 2026-11-11

All checkboxes in this plan concern this explicit pilot only. The complete roadmap and goal remain open.

- Before live calls, original E2E, real sandbox and architecture-boundary suites: **15 passed**, no skipped/failed/error cases. JUnit: `/Users/mango/.cache/agent-fleet/verification/real-cli-pilot-preflight-20261111.xml`.
- Real attempt6: **116.143s**, installed **Codex CLI0.160.0**, configured **newapi/gpt-6-luna / magpie / xhigh**. Both stages use the existing operator-selected configuration copied into distinct private CLI runtimes; no model override or global configuration change. Wrapper/native binary, provider snapshot, runtime config and adapter configuration digests are pinned. Same model, separate session/executor identities; this does not establish independent model families.
- Original Runner developer: **63.694s**; original CommandReviewAdapter reviewer: **38.424s**, strict verdict **passed**. Each original JSONL contains one thread/start and one completed turn, zero error/failed-turn events, exit0. The reviewer performed read-only source inspection; no synthetic verdict or source/identity repair.
- Original FixedTestRunner/SandboxBackend: **31 passed**, JUnit has zero failure/error/skip, launcher digest present. `TASK.md` and independent fixed tests exactly retain their seed hashes; only `ports.py` was added, alongside the platform's own source-manifest metadata. All steps share subject `4502ca1f9dc0ec7ca72aea9293c47145cf08d2bf24b82b1bfd48b61dd36686a4`.
- Source baseline: protected primary unchanged; dirty `codex/functional-root-fixes`, HEAD `301e259`, **1159 source-file hashes unchanged during the original run**. This is a working-tree snapshot, not a published revision or a clean development commit. Later additions are the UI-only harness and documentation; fresh product comparison confirmed503 Hub/core/tools/connectors/frontend files unchanged.
- Original workflow **wf-uQi-oCqS0XXBzn3EuVLvbRN0**, three original Runs all succeeded, three steps each generation1; SQLite integrity `ok`, no FK violations. **revision22**, acceptance **pending**, projected delivery **awaiting_acceptance**. Delivery digest `df0df2143553f21d6ffd337f46e84b9765acc325b4a951ec49617f834e5a9a0b`.
- Five original outputs were downloaded over HTTP and all bytes/size/hash verified. Logout invalidated the old Cookie (401); new login restored the exact same workflow detail. No second Runner invocation after completion.
- UI attempt2: existing **Chrome156.0.8078.80 /9222**, disposable context only. Desktop1440×1000 and mobile390×844/reload passed, all five rendered links downloaded exact hashes, zero page errors, zero Workflow writes, no horizontal overflow. Screenshots inspected; panel uses internal scrolling, so desktop and mobile captures show different portions. Original revision/digest and pending acceptance independently verified afterwards.
- Independent developer confinement canary denied host read/write. A reviewer probe initially ran after the original temporary source was already removed and retained its `FileNotFoundError`; a separate post-run copy of the original reviewer profile, with only the source path rebound to a verified artifact copy, denied host read/write and source write while allowing source read. This is separate launcher evidence, not production isolation acceptance.
- Failed attempts1–5 remain preserved. Attempt1 omitted service clock; attempts2/3 hit managed-preference shared-memory restrictions before model execution; attempt2 loop was interrupted after its original workflow was already failed. Attempt4 completed real developer/reviewer and31 tests but the harness incorrectly rejected the platform `.fleet-source.json`; that complete harness remains failed. Attempt5 correctly hit the product's strict review-config guard; the harness now pins config through the immutable adapter command digest rather than widening that contract. First UI attempt lacked Origin and got403; corrected UI-only execution used the original persisted workflow, no new model calls.
- CLI usage retained separately: developer input52642/cached45184/output1110/reasoning547; reviewer input43035/cached28112/cache-write14914/output1440/reasoning1027. These provider/CLI counters do not settle the platform ledger; cost and platform token settlement remain unknown. One timing sample is not a throughput or capacity benchmark.

Evidence root: `/Users/mango/.cache/agent-fleet/verification/real-cli-pilot-20261111/`; sanitized aggregate `verified-summary.json`, original attempt6 `result.json`, per-invocation original JSONL, source hash baseline and original DB retained. UI evidence: `attempt-6/ui-attempt-2/`. Private runtimes include provider secrets and operator access material: do not publish the full directory. No downloads, commits, push, merge or deployment occurred.

Remaining: genuine human acceptance, production sandbox/configuration, other CLI/models, full WF-04/05, complete Linux/browser release gate, external MCP/Muse/mail/deployment, remaining F5 extensions/stopping/archive and F6 real workload measurements. SG read-only probe found no installed/cached Chromium; Chromium195836009bytes and libllvm15 23115156bytes still require explicit approval under the user's download rule. The successful narrow pilot does not authorize these downloads or make the candidate release-ready.
