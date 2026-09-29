# Platform Review Remediation Implementation Plan

> **For agentic workers:** Execute this plan task by task in the current isolated worktree. The user has authorized all nine repairs; each requires failing evidence and fresh verification.

**Goal:** Correct the nine confirmed review defects without changing the source worktrees or deploying.

**Architecture:** Integrate the platform snapshot on top of `15c41a7`. Fail closed at execution and recovery boundaries; preserve runtime data during code rollback; coordinate artifact publication and backup; freeze conversation ordering; preserve delivery cursors and request identity.

**Tech Stack:** Python 3.13, SQLite WAL, Flask, vanilla ES modules, Bash.

## Global Constraints

- Original primary and platform worktrees remain untouched.
- No remote deployment, production access, push, or feature gate activation.
- Regression cases execute real workers, routes, repositories and browser modules with disposable fixtures.
- No automatic replay after a durable execution-start boundary; unresolved interrupted work becomes `unknown`.

## Tasks and verification

- [x] Integration: retain both session and platform deep links and tests; run the combined baseline suite.
- [x] Sandbox (`hub/application/run_worker_service.py`, `hub/config.py`, `hub/bootstrap.py`): bind local execution to `SandboxBackend`, pass an explicit administrator launcher. Regression: provider requests `cat` of a file outside the workspace; default worker must return `sandbox_unavailable`, without returning the file content. Preserve confined read/write support.
- [x] Crash recovery (`hub/infrastructure/platform_db.py`, `hub/application/run_scheduler_service.py`): treat durable `run_started` as the execution boundary. Expired work that crossed it becomes terminal `unknown`; a claim that never began execution may still be reclaimed. Regression: crash after a completed append effect, restart with a new repository and worker, assert the artifact is published only once and state is `unknown`; also fence stale writeback and cancelled interrupted work.
- [x] Backup (`hub/infrastructure/platform_backup.py`, `tools/platform/artifacts.py`): shared cross-process artifact barrier, database snapshot first, then inventory and copy under the barrier. Regression: publish an artifact and commit its Run reference at the old inventory/DB race boundary; restored DB must never reference missing content. Check both encrypted and plain backup, and barrier exclusion.
- [x] Rollback (`deploy/hk-overlay-sync.sh`): reuse code overlay preservation rules on rollback. Regression: committed SQLite rows, credentials, and hosts survive rollback through both rsync and minimal-image fallback while code returns to the old version.
- [x] Node signing (`tools/platform/node_runtime.py`): configure trusted Ed25519 public key and require verification. Missing key must fail closed. Regression: unsigned/tampered/wrong-key commands leave workspace and journal unchanged; signed command and receipt path succeed.
- [x] Conversation context (`hub/infrastructure/platform_db.py`): persist turn sequence and use the trigger sequence cutoff; serialize runs per conversation; project prior durable assistant results from Run records in turn order (one stored answer). Regression: two queued turns have equal timestamps and reversed IDs; first sees only first user, second sees first user/assistant then second user; replay/finalization cannot duplicate history.
- [x] SSE (`hub/http/observe_routes.py`, `hub/application/event_publisher.py`): capture a durable replay watermark, page bounded batches to it, deduplicate replay/live overlap, retain gap closure. Regression: more than 200 stored events plus concurrent live events arrive in order with no gaps or duplicates.
- [x] Turn identity (`frontend/views/assistant.js`): retain pending immutable payload/token until acceptance is confirmed; retry response-loss with the exact same payload even if controls change. Regression: execute the actual ES module using a minimal DOM and server fixture, simulate response loss after commit, assert one backend Run.
- [x] Model choice (`frontend/views/assistant.js`): send `overrides.model_profile_id`; show actual frozen Run selection. Regression: choose a non-default model, submit and verify the server snapshot and displayed result.
- [x] Final verification: focused tests for each repair, full combined pytest suite, Python AST/JS/Bash syntax and `git diff --check`; record outcomes and local commits. Deployment validation remains separate.

Test command: `PYTHONDONTWRITEBYTECODE=1 /Users/mango/.codex/worktrees/agent-fleet-platform-m1/agent-fleet/.venv/bin/python -m pytest -q -p no:cacheprovider`.


## Related boundary corrections found during verification

- An accepted turn retry now returns its original Run even if the selected model is later disabled; the HTTP regression first demonstrated a 409 response.
- An explicitly empty Node capability set no longer defaults to all workspace tools; a signed write regression first demonstrated unauthorized execution.
- Sandbox file operations reuse the existing DirectoryBackend implementation so root listing and disk quotas remain intact; the worker root-listing regression first demonstrated a failed Run after switching execution backends.

## Baseline evidence

The combined snapshot passed `2027` tests with `2` skips and `154` subtests before repairs. Original baseline review failures were promoted into tests and observed failing before their repairs. The Guardian async tests corrected by the latest root-fixes commits are included in this integration baseline.


## Final verification (2026-09-29)

- Full integrated suite: **2055 passed, 2 skipped, 154 subtests passed** (68.59 s), exit 0. This adds 28 passing regression cases over the 2027-test integration baseline.
- Python AST: **304** files; Node syntax: **17** JavaScript/ES-module files; Bash syntax: **13** shell files. All exit 0.
- `git diff --check`: passed. The remaining warning is the pre-existing invalid escape sequence in `tools/probe/discovery.py:230`.
- Full test output: `/tmp/agent-fleet-review-fixes-final-pytest.log` (local ephemeral log).
- Fresh evidence includes response-loss retry against a real local HTTP Hub, signed Node execution, cross-process artifact locking, encrypted backup race/restore, code rollback with a live SQLite connection, crash-before-receipt recovery through the scheduler, two-turn context ordering, and SSE replay across multiple pages.
- No deployment, remote operation, push, or production gate activation occurred. Original primary/platform/root-fixes worktrees were preserved. The integration and remediation are separate local commits for review.
