# Cross-platform Agent Adaptation Implementation Plan

> **For agentic workers:** Execute this plan inline, task by task, with failing evidence and fresh verification at each boundary.

**Goal:** Make the five registered Agent adapters usable and truthful on macOS and Windows, preserving native conversation identity and recording actual platform coverage.

**Architecture:** Keep the existing Runner → Supervisor → encrypted capture → Hub path. Resolve installed CLI entry points locally without executing Windows batch shims. Normalize observed native output and persist only proven continuation identities. Capabilities follow the installed interface; an absent CLI or unsupported model protocol remains explicit.

**Tech Stack:** Python 3.10+, existing Node CLI installations, SQLite, pytest, GitHub Actions, signed client releases.

## Global Constraints

- Protect the primary main checkout; use `codex/cross-platform-agent-adaptation` in the existing isolated worktree.
- UTF-8 without BOM. No application credentials in public evidence or argv.
- No unbounded process, output, retries, or implicit replay after an unknown delivery.
- Preserve existing sessions, defaults, native history, unique evidence, and rollback releases.
- No software download larger than 10 MB without explicit approval.
- Generic is observation-only. Desktop host processes are not single CLI conversations.
- Current registry: Codex, Claude Code, Hermes, Pi, ZCode. Live CLI coverage is independent from fixture and protocol coverage.

## Initial evidence

- Baseline: 104 tests and 101 subtests pass across profiles, probes, ZCode binding/bridge and dispatch capture.
- Pi defaults print plain text; its resume argv omits print/json mode. `message_end` and `tool_execution_end` stdout events are dropped by the current mapper. Runner continuation persistence only accepts Codex/Claude.
- Windows uses npm batch shims; unsafe instructions are correctly refused, but installed Node entry points are not resolved automatically. Windows Pi is explicitly disabled in local configuration.
- macOS ZCode bundles `zcode.cjs` without a PATH shim or adjacent provider file; the desktop runtime has a canonical provider configuration. Do not copy another endpoint's provider settings by guessing.
- Hermes supports oneshot and explicit resume in the installed CLI. The first direct read-only probe ran its terminal tool in a globally configured directory instead of the invocation cwd; no successful tool evidence may be claimed for that run.
- Windows Hermes has no installed CLI; an empty `.hermes` skills/cache directory is insufficient installation evidence.
- Private evidence root: `~/.cache/agent-fleet/verification/cross-platform-agents-20261121` on each participating host.

### Task 1: Resolve native CLI launchers without Windows batch parsing

**Files:** `tools/agent_command.py`, `tools/adapters/base.py`, `tests/test_agent_command.py`.

**Interface:** `resolve_command(family, command, environment)` returns a trusted local argv template. Explicit non-default commands remain authoritative; no Hub data selects filesystem paths.

- [x] Add regressions using temporary npm package layouts for Codex, Claude and Pi, preserving quotes, Unicode and shell punctuation as one argument.
- [x] Run `python -m pytest tests/test_agent_command.py -q` and retain failing output.
- [x] Resolve only known installed packages and family-specific native executables/scripts; use an existing Node executable, never `cmd /c`.
- [x] Verify missing installations fail explicitly, custom commands stay intact, and npm paths cannot escape their package roots.

### Task 2: Complete Pi structured capture and durable non-interactive continuation

**Files:** `agent_profiles.py`, `tools/adapters/pi.py`, `tools/session/bridge.py`, `tools/supervisor/native_resume.py`, `tools/supervisor/supervisor.py`, `tests/test_cross_platform_agents.py`, existing profile tests.

**Interfaces:** Keep `_native_resume_argv(family, exe, token, text)` and existing private continuation store. Preserve Node script prefix and allowlisted provider/model/policy arguments with `continuation_options`.

- [x] Test default and follow-up argv contain print/json mode and an exact session selector.
- [x] Test one complete Pi `message_end` maps assistant text/tool calls; one tool result maps once; incremental/thinking/system events do not become conversation text.
- [x] Test a completed real subprocess fixture restores the same Pi native ID in a new Supervisor and rejects ephemeral sessions.
- [x] Reproduce failures before implementation; run the same tests after minimal changes.
- [x] Verify actual FIRST/FOLLOW on both installed Pi versions, including real file contents, same native ID, unique follow-up and unchanged test files.

### Task 3: Complete ZCode/Hermes launch and capture at their actual interfaces

**Files:** family adapters/profiles, local CLI helpers, capture/native resume modules only where live evidence supports the interface, `tests/test_cross_platform_agents.py`, capability documentation.

**Interfaces:** ZCode's exact `sess_...` ID comes from its actual JSON/native record; desktop bridging retains its existing separately verified delivery protocol. Hermes tool evidence must come from native storage/structured interface, never inferred from an answer.

- [x] Inspect installed output and native storage, then add exact fixture regressions from redacted observed schemas.
- [x] Bind provider paths only from explicit local configuration or an unambiguous canonical installed runtime.
- [x] Correct Hermes workdir binding without rewriting user defaults; preserve configured provider authentication.
- [x] Verify safe process lifecycle/failure behavior on both OSes with actual fixture children, then test available installed CLIs independently.
- [x] Record absent installations and unavailable upstreams distinctly; do not turn skips into passes.

### Task 4: Platform gates, release, signed clients and production acceptance

**Files:** `.github/workflows/ci.yml`, `docs/testing/agent-platform-acceptance.md`, this plan.

- [x] Add the new adapter/continuation/probe suites to Windows CI and a macOS native process gate if supported by the repository workflow.
- [x] Run focused macOS tests and a frozen source copy on Windows; retain JUnit and source hashes.
- [ ] Run all required Linux/browser release journeys in CI. Resolve failures against the original evidence.
- [ ] Rebase onto fresh origin/main, publish the tested branch, create/attach PR, wait for exact PR CI, merge, then follow exact main CI through production deployment.
- [ ] Check manifest/release origin, served source/asset hashes, health/auth and database integrity. Keep deployment rollback evidence.
- [ ] Upgrade clients only if the signed content closure changed; verify signatures/hash coverage, drain existing work, preserve rollback and check running identities.
- [ ] Repeat actual available CLI FIRST/FOLLOW through the deployed Hub on Mac and Windows. Confirm defaults/history preserved.
- [ ] Update canonical operations notes and routing on the vault master branch, run doc-check, and report the capability matrix with all external limitations.

## Latest verified checkpoint

- macOS focused suite: 465 passed, 2 platform skips, 103 subtests.
- Windows frozen v4: 236 passed, 2 platform skips, 101 subtests; source hashes retained. Initial namespace collection failures and platform-fixture failures preserved.
- Actual pre-release native FIRST/FOLLOW: Mac Pi/ZCode/Hermes and Windows Pi/ZCode; exact native ID restored, real UTF-8 file tool output, unchanged probe bytes.
- Pi dual result events and ZCode filename-prefix token error reproduced red and corrected; arbitrary Hermes transcript cannot assert attached resume.
- GitHub merge, signed clients and deployed Hub acceptances remain pending until their exact receipts are verified.
