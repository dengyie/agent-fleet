# Native artifact acceptance integration plan

> Execute inline in the existing isolated feature worktree with evidence-driven-bugfix and verification-before-completion. Preserve prior failed reports and every source worktree; do not change production or host services. Return to the [development ledger](../../platform/development-completion.md).

**Goal:** Restore the missing native Windows and two-host acceptance dependencies to the current integrated code, then verify protected NTFS receive/export and actual Windows-to-Linux source handoff with independent fixed tests.

**Architecture:** Reuse the seven existing opt-in fixtures from the preserved workflow source tree. Product traffic still uses NodeRuntime, ArtifactExporter/Receiver, the original disposable Hub, persistent journals and existing Linux FixedTestRunner/bubblewrap. The controller passes private configuration on stdin and carries only evidence metadata. Source bytes travel through original artifact HTTP APIs.

**Stack:** Python 3.10-compatible source, installed home-win Python 3.12.7 private environment, existing SG Linux Python 3.11.2 environment and private bwrap; no downloads, package installs, global settings, services, commits or deployment.

## 1. Missing dependency and interface evidence

- [x] Run the documented native probe collection in the target tree before integration. `/tmp/native-fixture-integration-red-20261111.log` reports the receive probe missing; no tests can collect. The existing capability canary imports its `private_directory` helper on Windows.
- [x] Copy `windows_artifact_probe.py`, `windows_artifact_export_probe.py`, `windows_node_canary.py`, `windows_export_canary.py`, `two_host_producer.py`, `two_host_receiver.py` and `two_host_handoff_probe.py` from the preserved source worktree without replacing existing target fixtures.
- [x] Run current-interface checks before adapting demonstrated incompatibilities. Initial copied collection had two missing `hub.domain.control` errors; neutral imports now use existing fleet_core modules. The Windows remote command requires the explicitly selected working interpreter. Original receive receipts still use `post_json`, export receipts use bounded JSON; their existing distinct loss probes remain correct and both were observed during actual handoff.

## 2. Native NTFS and actual handoff

- [x] Freeze all current Git-visible source bytes in a separate clean evidence checkout, retain the full digest map and source bundle, then transfer that small bundle to new private source directories on both hosts. Do not alter earlier frozen checkouts.
- [x] Run the original neutral path suite, explicit Windows receive and export probes together on home-win using a short, fresh private basetemp. Preserve actual errors; do not replace native checks with mocks or mark them skipped.
- [x] Execute the original two-host controller against actual home-win and SG Linux. Verify producer/Hub/receiver byte size and SHA-256, original command/transfer/grant identities, one upload, one download, lost-response recovery and delayed coordinator reconciliation.
- [x] Independently validate the received original repository/tree/blob identities and run the two fixed synthetic pytest cases using existing Linux sandbox. Retain report/JUnit and launcher identity; do not equate synthetic source tests with real commercial CLI/model acceptance.

## 3. Fresh audit and scope

- [x] Run related portable artifact/Node/Workflow regression, lint, Python 3.10 AST, UTF-8/no-BOM and diff checks. Run source hash/status audits on all frozen checkouts after native tests.
- [x] Update this plan, ledger and acceptance matrix with exact revision, commands, failures and final results. The earlier 68-failure Windows report remains intact; a corrected native probe is a separate scope.
- [x] Keep complete Linux/browser release evidence, real CLI/model, production isolation and remaining functionality open until separately verified. Existing large-download permission remains pending and is not inferred from goal continuation.

## Verified results — 2026-11-11

Evidence root: `/Users/mango/.cache/agent-fleet/verification/native-handoff-20261111/`. Immutable revision `a9da2c29ace2c9fa06b6faad8c58989b9ed62c91` contains 1142 source files. Its 7350600-byte source bundle has SHA-256 `d6eab0b6c6354a10f7124bfb539a3a51122826ae3b59245ed52991ee1985d0c5`. All source digests stayed unchanged and Git status stayed clean on the local freeze, home-win and SG Linux.

- Native Windows combined neutral/receive/export suite: **131 passed, zero failures/errors/skips**, 10.30s. Existing private Python 3.12.7 on Windows 11/NTFS; includes actual kernel handles, locks, write/rename/mapping exclusion, four process-exit windows and original journal recovery. Command/log/JUnit retained in `native/`.
- Portable artifact/Node/Workflow/boundary regression: **313 passed, zero failures/errors/skips**, 24.52s, existing `/usr/local/bin/python3.13`. An initial controller launch explicitly selecting a different Homebrew Python 3.13 failed before pytest collection because that runtime lacks pygments; its command/log remains in `two-host-a9da2c29/`. No dependency was installed or global interpreter changed. The actual controller then used the already working Python 3.14 runtime.
- Actual controller case: **1 passed, zero failures/errors/skips**, 9.08s, in `two-host-native-2/`. Controller `mangodeMacBook-Air.local`, producer `DESKTOP-VST7EIE`, receiver `hcss-ecs-3ed7` are distinct hosts. Original Windows export lost an accepted part response and resumed without another upload; Linux receive lost an accepted receipt response and reposted the same receipt. One PUT, one download, two receive receipt posts. Original Workflow input stayed waiting_node until the original service reconciliation made it ready.
- Producer/Hub/receiver source: 10240 bytes, SHA-256 `c5de78025951b553bc165aa960e206c36288fc13044f6ea383b2fdf4335ce122`; original base/head/tree identities verified. Source contains only committed `src.txt` and `test_payload.py`; tracked synthetic `.env`, modified worktree bytes and untracked bytes do not enter the bundle.
- Separate FixedTestRunner validation on SG Linux sandbox: **2 fixed cases passed**, original report/JUnit copied independently from the receiver. Report SHA-256 `b7f4592559ededc155f66676e4314f6e2c5978a52d60904f40de30ca342696f4`; JUnit SHA-256 `0dd56d5713ef02d2b86e50880ae12898eb278c8215d9feb8b85efdf694c4c1cf`. Copy sizes/digests match `remote-evidence/copy-audit.json`. The two nested cases are not added to controller or full-suite totals.

This closes native artifact and narrow two-host handoff acceptance for this revision. Full WF-04/05, real commercial CLI/model, production sandbox and strict Linux/browser gate remain open. Historical full-suite counts and the broader failed Windows run retain their own scope. No production deployment, package download, service change or cleanup occurred.
