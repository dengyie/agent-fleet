# Deployable Linux Workflow fixed-test launcher

**Goal:** Provide a repository-owned launcher for the existing Hub and Node fixed-test recipe, so deployment no longer requires a test fixture or an unwritten wrapper.

**Architecture:** Move the existing tested Linux namespace/seccomp implementation from `tests/fixtures/workflow/linux_sandbox.py` into `fleet_core/workflow/linux_launcher.py`. Keep the old fixture as a compatibility entry point. Add `--fixed-test` to derive the current Python runtime, actual working directory and fixed JUnit destination; accept an explicit absolute bubblewrap path or resolve the installed executable. Preserve the existing explicit test/review mode contract, no new execution lifecycle or default opt-in.

**Constraints:** Python3.10; UTF-8 without BOM; protected primary checkout; no package/software downloads, production configuration writes, service changes, deployment or automatic human acceptance. Existing sandbox behavior remains unchanged. This work supplies the fixed-test launcher only; a real model reviewer and its launcher require their own configuration and verification.

- [x] Reproduce the missing production module via real CLI and config-normalization tests before implementation.
- [x] Reuse the original launcher implementation and retain the fixture CLI contract; derive the production fixed-test argv without importing tests or accepting a new recipe.
- [x] Exercise actual Linux fixed-test success/failure, source/network/environment boundaries and repeated independent work directories through `SandboxBackend` and the original validator; cover invalid command and missing bubblewrap before execution.
- [x] Verify Hub and Node startup configuration accepts the actual launcher argv; document absolute paths/runtime and dependency requirements, register implemented selectors and freeze/verify relevant local and Linux suites.

Files: `fleet_core/workflow/linux_launcher.py`, compatibility `tests/fixtures/workflow/linux_sandbox.py`, `tests/test_linux_workflow_launcher.py`, existing sandbox/runtime tests and [Hub startup](../../platform/workflow-platform/startup-registration.md)/[Node startup](../../platform/workflow-platform/node-startup.md).

Return to [development ledger](../../platform/development-completion.md), [acceptance matrix](../../testing/workflow-platform-acceptance.md) and [Workflow entry](../../platform/workflow-platform/README.md). Full Linux/browser release gate, production reviewer, real deployment and the remaining roadmap stay open.

## Verified results — 2026-11-11

- Initial missing-module evidence: 6 failed / 2 Linux-only skips. Final independent frozen revision `2570449a631925872e83e2276d161584a4efb4c0` contains 1178 source files.
- macOS related regression: 230 passed / 3 Linux-only skips / 2 passing subtests / 66.50s. Same Linux collection: 233 passed / zero skips / 2 passing subtests / 106.21s. Reports include subtests as JUnit testcases; counts are not added across platforms.
- Original signed Node receive, actual sandbox recipe, journal restart without execution, source/network/environment boundaries and reviewer fixture compatibility are covered. Five migrated sandbox functions have identical AST to the previous frozen implementation.
- Journey collection resolves 44 journeys / 2104 selectors / 7107 cases, with zero missing selectors. This is collection validation, not a complete release run.
- Original packager produces 1102 backend files / 6,685,869 bytes and 67 frontend files; all packaged bytes match, including the shared launcher. Both frozen checkouts remain clean with source hashes unchanged; copied Linux reports match remote SHA256.
- Evidence: `/Users/mango/.cache/agent-fleet/verification/linux-launcher-20261111-xmgq3zed/verified-summary.json`, `launcher-reuse-audit.json`, `package-verification.json` and copied JUnit reports. Subsequent documentation corrects the reviewer request-file protocol; it is not part of this code freeze.
- No production configuration or deployment changed. The earlier full macOS run belongs to `1d982fce`; this new revision has no complete Linux/browser release gate. A production model reviewer remains to be configured and verified.
