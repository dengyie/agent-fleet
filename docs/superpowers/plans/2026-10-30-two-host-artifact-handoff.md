# Two-Host Artifact Handoff Implementation Plan

> **For agentic workers:** Execute inline in the existing isolated feature worktree. Preserve all existing work and test evidence. Use only the authorized home-win/gz-cvm temporary environments and installed runtimes; no install, global configuration/service change, development commit, push, merge, deployment or cleanup.

**Goal:** Verify a source bundle produced and exported by the actual Windows Node, transferred by the original Hub to the actual Linux Node, then independently checked and tested on the receiver.

**Architecture:** Add explicit opt-in fixture adapters around the existing SourceExporter, NodeRuntime export/receive, artifact input service and FixedTestRunner. The controller carries only trusted test configuration and metadata; source bytes travel through original Node export/Hub publication/Node receive HTTP. Validation is a separate explicit operation after reception and original Hub confirmation.

**Tech Stack:** Existing Python/pytest/cryptography, Git, native Windows NTFS adapter and task-private Linux bubblewrap/seccomp. No dependency or product protocol change.

## Global constraints

- Authority: [artifact contract §4.2](../specs/2026-10-20-artifact-capability-integration-contract.md#42-固定-node-导出与本机交接) and [WF-04 acceptance](../../testing/workflow-platform-acceptance.md#6-wf-04真实文件交接).
- Different actual hostnames, node identities, machine credentials, workspaces and journals. Keep machine credentials only in private stdin and process memory, never files, command arguments, output or model input.
- The producer creates a synthetic fixed Git repository and exports its committed tree with SourceExporter. Uncommitted/untracked bytes and `.env` must not enter the bundle. A bounded local copy into an owner-protected Windows output is fixture preparation, never a replacement for inter-host artifact transfer.
- The original signed command selects only output_key. Export must create one original Hub transfer/artifact and output binding. A lost accepted part response resumes the original ID without a second PUT for that part.
- The Linux receiver starts only after the original artifact is bound. Original ArtifactInputService creates its signed grant. Wrong-node reads are rejected; the step stays waiting_node until the exact Node receipt is confirmed and original input preparation reconciles it.
- A lost accepted receive receipt response restarts the same journal, downloads once and posts the same receipt twice. Reception never executes or extracts content. The separate explicit validation operation verifies the received file's bytes/hash, original tree/blob/repository objects, then uses FixedTestRunner and the existing Linux test sandbox.
- O(n) hashing/copying with 64 KiB blocks; bounded synthetic source/archive (at most 1 MiB) and 64 KiB control JSON/reports. No unlimited reads or unowned background process. SSH operations have timeouts; temporary reverse listeners are scoped to each subprocess.
- All new text is UTF-8/no BOM. Test fixtures are explicit opt-in, not required Linux skips; retain reports independently from the previous export milestone. All product feature gates remain default false.

## Task 1: Producer preparation and original signed export

**Files:** Create `tests/fixtures/workflow/two_host_producer.py`.

**Interface:** `main() -> None` consumes at most 64 KiB JSON on stdin, explicit `operation=prepare|export`, a fixed temporary root/case, original Node registration and injected clock. Preparation returns source revision/hash/size metadata only; export returns original receipt and original transfer ID, without credentials or source bytes.

- [x] Implement the committed synthetic Git source, excluded credential sentinel and modified/untracked controls; create the registered output using the existing private native descriptor.
- [x] Execute original NodeRuntime, drop an accepted part response and restart its original journal; assert one PUT and original IDs/receipt.
- [x] Compile and run on actual home-win; preserve all files and report exact host facts.

## Task 2: Original receive and independent fixed validation

**Files:** Create `tests/fixtures/workflow/two_host_receiver.py`.

**Interface:** `main() -> None` consumes bounded stdin JSON, `operation=receive|validate`. Receive uses original NodeRuntime/ArtifactReceiver and returns original receipt/hash/size/restart facts. Validate receives the expected original CodeRevisionReference and performs only the explicitly requested fixed tree/blob/test operation against the already received source.

- [x] Create independent mode-0700 roots/journals, use original input_alias/grant and discard credentials after the operation.
- [x] Lose the accepted receive receipt response, restart, assert one download/two receipt posts and original ready journal.
- [x] Reopen the same pinned regular single-link source through the existing protected output handle; verify bytes/hash and original F3 tree/blob extraction.
- [x] Run existing FixedTestRunner with the existing task-private Linux sandbox; persist and verify its report/JUnit with two passing synthetic tests and no credential sentinel.

## Task 3: Controller, provenance and evidence

**Files:** Create `tests/fixtures/workflow/two_host_handoff_probe.py`; update contract/progress/roadmap/acceptance only after actual evidence.

**Interface:** Explicit environment `FLEET_WINDOWS_TEST_HOST/ROOT`, `FLEET_LINUX_TEST_HOST/ROOT/PYTHON`, and `FLEET_TWO_HOST_TEST_EVIDENCE`; the ROOT values are identical source checkouts on their respective hosts. Reuse original export_node/transfer/setup/Hub fixtures and original input preparation.

- [x] Prepare the actual producer, queue its original signed export, then verify original Hub bytes metadata/output binding and unchanged Workflow step.
- [x] Create a second Workflow/registered receiver input through original services; prove wrong-node denial and waiting_node before/after remote receipt until original reconciliation changes it to ready.
- [x] Execute the actual Linux receiver, then separately invoke validation. Assert all three hostnames differ, exact producer→Hub→receiver size/hash, same artifact/transfer/grant/command identities, and valid independent test report/JUnit.
- [x] Retain source maps/frozen revision, per-host before/after hashes, commands, reports, stdout/stderr and JUnit; run explicit fixture and dual-Python compileall. Preserve any failures and correct only their demonstrated root cause.
- [x] Record this narrow actual two-host pilot separately from the unchanged prior 5785-pass full gate. Do not infer full WF-04/05, real model/CLI or production readiness from this pilot.

Explicit controller command:

```bash
python -m pytest tests/fixtures/workflow/two_host_handoff_probe.py -q --junitxml=two-host.xml --tb=short
```

Expected result is one actual handoff case passed with original receive/export restart facts and two nested fixed pytest cases passed. No automated test can authorize publishing, deploying, sending messages or deleting retained evidence.


## Actual frozen verification — 2026-10-30

Frozen revision `8f63ddd660c07ddf9d55123132b2b26d2b4f2296` contains 890 source files. All hashes match the development worktree before these result-only document updates. Each actual host verified all 890 hashes before and after; the independent local evidence checkout is clean. The journey matrix remains SHA-256 `061fcdd9c41f229ca0d6efa733e5a48a37a29ae2b2bcd7151b97fd8095eb2682`.

The explicit controller command above passed **1 case, 0 failures/errors/skips**, 9.89 seconds, with process exit 0 (UTC 11:17:55–11:18:05). Controller `mangodeMacBook-Air.local`, Windows producer `DESKTOP-VST7EIE` (Windows 11 / Python 3.12.7), and Linux receiver `VM-0-7-tencentos` (Python 3.13.16) are distinct actual hosts. The Linux operation used the existing task-private bubblewrap/libseccomp runtime and `setpriv` with no DAC override/read-search capability and NoNewPrivs.

- SourceExporter used committed Git objects: base `db9b77d82da521bda6d19fa8c5691e0c723b1813`, head `90519ce12cb6262a61f6425bea487ccfb4301d6e`, tree `407b9018a2c24d9cdf934a28fe29d24c2a0b4c7c`. Only `src.txt` and `test_payload.py` entered the bundle; the tracked synthetic `.env`, later working-tree mutation and untracked file did not.
- The original export resumed an accepted part whose response was lost: **one PUT, one original transfer and output binding**, with one export receipt. Producer, original Hub content and receiver each retain identical 10,240 bytes / SHA-256 `c5de78025951b553bc165aa960e206c36288fc13044f6ea383b2fdf4335ce122`.
- The original receive resumed a lost accepted receipt response: **one download, two identical receipt posts**. The wrong-node read returned 404; the original input service retained waiting_node even after HTTP receipt, then reconciled the same generation to ready. The producer Workflow step was not completed by export.
- Separate authorized validation verified original repository/tree/blob identities and ran FixedTestRunner: **2 nested fixed pytest cases passed**, no failure/error/skip, sandbox backend and launcher SHA-256 recorded. The original report and JUnit were copied from Linux and their hashes independently matched the controller metadata. These two cases are not added to the controller case or previous full-gate totals.
- Python 3.10.21 and 3.13.15 compileall both exit 0 for the three complete fixtures; UTF-8/no BOM and complete function annotations checked.

Evidence root: `/Users/mango/.cache/agent-fleet/verification/two-host-20261030-czhi883m`. It retains `provenance.json`, source bundle/zip, `two-host-command.json`, `two-host.log`, `two-host.xml`, all four operation reports/stdout/stderr/commands, per-host source audits, `verification-summary.json`, compile records and eight remote evidence files under `remote-evidence-v2/`. Every copied file's size and SHA-256 matches its remote inventory. `pilot/handoff-summary.json` binds the original transfer/artifact/grant/command IDs; `full_workflow_completed=false` is explicit.

One evidence-copy audit failed because PowerShell removed JSON argument quotation. That retained failure changed no product or test fixture. A second audit passed the filename list as base64 and copied to a fresh evidence directory; the original actual handoff was not rerun or overwritten.

This closes the narrow actual two-host source handoff and independent fixed-validation pilot. It does **not** complete full WF-04/05, a coding-delivery Workflow, production isolation, real model/commercial CLI, MCP/Muse or other external checks. The previous 5,785-pass full gate belongs only to frozen revision `6ba4384adde2a01f44dd96b8ec9c85c2e9a2d839`; this later fixture-only pilot has its own evidence. No required journey or external-check status is forged or promoted. All feature gates remain default false.
