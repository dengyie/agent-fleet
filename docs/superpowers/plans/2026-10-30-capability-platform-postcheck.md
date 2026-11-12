# Actual Platform Capability Postcheck Implementation Plan

> **For agentic workers:** Execute inline with `executing-plans` in the existing isolated feature worktree. Preserve all existing source and evidence. Do not spawn agents, make development commits, merge, push, deploy, install software, change global settings/services or delete files. An independent evidence Git snapshot records source identity only. The fixed home-win/gz-cvm workspace capability fixture was explicitly authorized on 2026-10-30; prepare fresh private roots and retain evidence.

**Goal:** Verify original capability-bound workspace writes and independent read-only postchecks on actual Node platforms, retaining unknown execution and held/isolated reservations after every result and restart.

**Architecture:** Two explicit opt-in test files wrap existing NodeCapabilityService/Reporter, CapabilityRepository admission, RunStepExecutor, CommandDeliveryService, NodeRuntime and CommandPostcheckService. The controller first obtains a real platform/software report before fixing requirements and original binding. A fixed synthetic write completes then deliberately loses its local completion to produce original journal/Hub unknown; the existing test fixture sets the Workflow/Run unknown reservation state. Postcheck only observes current bytes. No product protocol, state schema or dependency changes.

**Tech Stack:** Python 3.10+, existing Flask/SQLite/cryptography/pytest, native private Windows directories, installed Python runtimes and bounded SSH reverse loopback. No process execution tool or sandbox permission is enabled.

## Global constraints

- Authority: [capability report/preflight §6.3](../specs/2026-10-20-artifact-capability-integration-contract.md#63-node-报告发送与本地执行前复核子集), [binding/postcheck §6.4](../specs/2026-10-20-artifact-capability-integration-contract.md#64-hub-固定能力-run-与-command-派发子集) and [original postcheck plan](2026-10-23-capability-postcheck.md).
- The independently registered `workspace.digest` helper must be in the original Workflow requirements and binding, Hub action registration and Node local allowlist. It is `read_only`; the write keeps original `reconcile_before_retry`. Neither original retry contract nor original target/generation/token/payload is changed.
- Actual OS/architecture/Python/disk facts come from `observe_local_capabilities`; unknown free memory stays zero. Requirements select only the reported platform/software, zero memory and 200-byte disk budget. Reporting uses the original authenticated HTTP port before admission.
- Per platform, cover both held and isolated reservation states × matched, mismatch and missing-file observations. Rename retained test files instead of deleting them; fixtures use fresh private directories and independent journals.
- Actual backend write runs exactly once. An intentional exception after its successful byte write produces original journal unknown. Postcheck must not call write; duplicate original command remains unknown, duplicate check returns its saved result, restart repeats no write/digest I/O.
- Original Workflow/Run/dispatch/budget/binding/reservation and original Command snapshots remain byte-for-byte unchanged while postcheck completes. The test-only unknown-state setup follows the existing `Fixture.isolate_write`; this is not a full Workflow or real-model execution claim.
- Control JSON/reports are at most 64 KiB, each operation timeout 120 seconds, SSH reverse listener scoped to its process. Test content is fixed and bounded; digest is O(n) under the existing 8 MiB cap; binding lookups are indexed or dictionary O(1), no new abstraction/interface or state engine.
- Credentials are stdin/process-memory only; never save request values, secret headers, command arguments or source content in public reports. Logs contain metadata and redacted receipts only.
- UTF-8 without BOM. Explicit opt-in fixtures do not add ordinary CI skips or change required journeys. All product gates remain default false; previous frozen runs retain their own evidence scope.

## Task 1: Fixed platform Node adapter

**File:** Create `tests/fixtures/workflow/capability_node_canary.py`.

**Interface:** `pilot_definitions() -> tuple[CapabilityDefinition, CapabilityDefinition]` fixes the two test definitions; `main() -> None` reads at most 64 KiB JSON on stdin with `operation=prepare|write|postcheck`, a fixed case/private root, original machine credential/signing public key, original command IDs and controlled clock. Return bounded platform/offer/receipt/journal/count metadata.

- [x] Create private platform directories; prepare original NodeRuntime with only fixed workspace.write and reconcile.workspace.digest registrations; report actual current facts to the original Hub and return the exact config digest.
- [x] Run original signed write once, preserve exact bytes, intentionally interrupt before completion, assert Node unknown and one original receipt. Store only non-secret test metadata.
- [x] Perform matched/mismatch/missing-file preparation within the same private fixture, preserving original bytes by rename for the two negative cases. Run the original signed helper only; duplicate and restarted original/check must perform zero additional I/O and retain original unknown.

Required terminal state checks:

```python
assert original_record.state == "unknown"
assert write_calls == 1
assert postcheck_write_calls == 0 and digest_calls == 1
assert restarted_write_calls == restarted_digest_calls == 0
```

## Task 2: Original Hub admission and evidence controller

**File:** Create `tests/fixtures/workflow/capability_hub_probe.py`.

**Interface:** Explicit `FLEET_CAPABILITY_TEST_PLATFORMS=local|windows,linux`, `FLEET_CAPABILITY_TEST_EVIDENCE`, and the established host/root/Python/path environment variables. Local mode launches the same worker as a bounded subprocess; remote modes require authorized targets and use ephemeral SSH reverse loopback. Pytest parametrizes platform × held/isolated × matched/mismatch/read_failed.

- [x] Prepare original Workflow/database/node policy/definitions and obtain the actual Node HTTP report before original freeze/admission; use its platform/software/config digest in the original requirements and binding.
- [x] Create original Run through RunStepExecutor and signed write through original delivery. Verify actual unknown receipt, then use existing test-only unknown/held-or-isolated setup; snapshot all original tables and command.
- [x] Request the postcheck through original HTTP; prove deterministic descriptor, original binding, helper identity/read_only contract, same target/generation and scoped redaction. Execute remote helper; query original service result matched/mismatch/remains_unknown, wrong-owner 404, exactly one audit.
- [x] Assert original snapshots remain identical after result/repeat/restart, original command never replays, one write/one digest, two original commands and one descriptor. Preserve per-operation records and case summary.

Required controller checks:

```python
assert after_snapshot == before_snapshot
assert original_command["status"] == "unknown"
assert reservation_state in {"held", "isolated"}
assert helper.capability_id == "workspace.digest"
assert helper.binding == original_binding
assert postcheck_state == expected_state
```

## Task 3: Native evidence, compatibility and documentation

**Files:** Update this plan, capability contract/platform progress/roadmap/testing entries only after verified results.

- [x] Compile both complete files on Python 3.10/3.13 and run six cases with the same actual local subprocess adapter, fresh private basetemp/evidence, no remote access.
- [x] Upon explicit fixed-capability remote authorization, freeze an independent source revision and run twelve cases on actual Windows/Linux; retain all source maps/commands/logs/JUnit/host facts and verify before/after hashes. No install or settings/service change. Exact result and identity are recorded in [Native frozen verification](#native-frozen-verification).
- [x] If any case fails, retain failing evidence, establish its root cause and correct only that cause before a new frozen fresh run. Never relax original state/capability/receipt assertions.
- [x] Verify bounded reports, UTF-8/no BOM/annotations/local document links/diff checks. Record this platform capability/postcheck slice separately from full WF-05, NODE-LIVE and production/external acceptance; do not reuse prior full-gate results for new code.

Explicit local command:

```bash
FLEET_CAPABILITY_TEST_PLATFORMS=local FLEET_CAPABILITY_TEST_EVIDENCE=/fresh/private/evidence python -m pytest tests/fixtures/workflow/capability_hub_probe.py -q --tb=short --junitxml=/fresh/private/capability.xml --basetemp=/fresh/private/fixtures
```

Remote command changes only the explicitly authorized platforms and target environment to windows,linux; expected twelve passing cases with no failures/errors/skips. Six local cases and twelve remote cases are independently reported, never summed into a release result. These tests cannot authorize arbitrary shell, real model calls, deployment or reservation release.


## Local implementation and verification — 2026-10-30

Both complete opt-in files are implemented. Actual macOS subprocesses use original authenticated HTTP reports, admission/binding/Run/signing, an interrupted completed write, original journal unknown and independent helper observation. The existing test-only unknown Workflow state setup is explicit; it does not claim model execution or independent stop evidence.

- First local run: six cases reached the final summary and failed because the new fixture accessed nonexistent `TargetBinding.digest`. Evidence `/Users/mango/.cache/agent-fleet/verification/capability-platform-local-9iicxv1_/results.xml` and `failure-analysis.json` is retained. The minimal fix computes SHA-256 of the existing canonical binding snapshot; no product code or assertion was changed.
- Python 3.13.15 fresh local run: **6 passed, 0 failures/errors/skips**, 6.35 seconds, command exit 0. Evidence `/Users/mango/.cache/agent-fleet/verification/capability-platform-local-fixed-6ya1m8wx`.
- Python 3.10.21 separate fresh local run: **6 passed, 0 failures/errors/skips**, 6.12 seconds, command exit 0. Evidence `/Users/mango/.cache/agent-fleet/verification/capability-platform-local-py310-bnxr9wx3`. Current fixture digests match that run's before/after map.
- Both compileall commands exit 0. UTF-8/no BOM, AST/function annotations and `git diff --check` pass. Each of the six summaries independently asserts one write, one digest, zero restart I/O, original snapshots unchanged, original command unknown and held/isolated reservation retained. Counts from separate Python runs are not summed.

Local evidence index `/Users/mango/.cache/agent-fleet/verification/capability-local-verification-summary.json` records exact fixture hashes, commands, summaries, original identities and evidence roots. This is a local fixture result, not a complete new all-repository gate. The previous 5785-pass export freeze and actual two-host artifact handoff retain their own scope.

**Historical scope:** At the local implementation recording point, the new remote capability test had not been authorized or run. The subsequently authorized Windows/Linux fixed workspace matrix is recorded below; full WF-05/NODE-LIVE, production and real external checks remain open, with all gates default false.


### Final local authority check

Before freezing the remote candidate, self-review made the fixture's risk declaration precise: `workspace.files` is `workspace_write`, and its independent `workspace.digest` helper is `workspace_read`/`read_only`. The explicit test node policy permits both exact risks. This changes only test authority registration, retains the original write retry, and avoids using a read-risk definition for a write action.

The resulting final fixtures were rerun independently: Python 3.10.21 **6 passed/0 failures/errors/skips (5.91s)** and Python 3.13.15 **6 passed/0 failures/errors/skips (6.27s)**. Both compileall commands exit 0, and fixture hashes before/after match. Final local evidence root `/Users/mango/.cache/agent-fleet/verification/capability-platform-local-final-3zpgs6qw`, exact commands/maps and six result summaries per version in `verification-summary.json`; earlier runs remain historical and are not combined. The later Windows/Linux twelve-case run has its own frozen identity below.

### Authorized next freeze and audit correction

The user explicitly authorized the fixed home-win/gz-cvm workspace write and independent read-only digest test on 2026-10-30. Installed runtimes were checked without installation or settings/service changes: home-win Python 3.12.7 and gz-cvm Python 3.13.16. The old authorization-pending paragraphs above describe their original recording point.

Local review then reproduced a separate descriptor/audit transaction gap in all three original postcheck kinds. The [atomic audit fix](2026-10-30-postcheck-atomic-audit.md#verified-local-result--2026-10-30) preserves original capability protocols, risk/retry, Workflow/Run/binding/reservations and helper registration while fixing completion persistence. Current Python 3.10/3.13 related regressions independently report **279 passed**, and each fresh local capability run reports **6 passed**, with no failures/errors/skips. Evidence is `/Users/mango/.cache/agent-fleet/verification/postcheck-atomic-20261030-zms34vh4/verified-py310/` and `verified-py313/`.

The next candidate includes this verified Hub fix and exact regression registration. Keep `3055506b8162c6dae99681728438341c2e57ed5e` as historical; do not use its hash for the changed Hub. The independently frozen and verified candidate below supplies the actual native-host evidence.

## Native frozen verification

Recorded 2026-10-31 in Asia/Singapore. The authorized original `windows,linux` command completes **12 passed / 0 failures/errors/skips**, pytest exit 0, 52.80 seconds. Controller Python is 3.13.15; actual home-win uses Windows/Python 3.12.7 and gz-cvm uses Linux/Python 3.13.16. Each platform contributes its six distinct held/isolated × matched/mismatch/read_failed cases to this one report; separate local runs are not added to its count.

Independent evidence revision `16f10ebd630d38fa53a839ce9d2c432719014120` contains **895 files**. Local and both native checkouts match every source digest before/after, have no changed source paths and remain clean. `source.bundle` is 3,226,411 bytes / SHA-256 `015598d38f5b5ed4163a4cb3dcb7b3bb3460f32296853cb38812de9872290bc6`; `source.zip` is 3,361,075 bytes / SHA-256 `8d333665dab7de3d4cd0578a11906382f8e714d24f66867dd6a3d476d9cfa751`. Neither artifact is a development commit.

Every original Node journal independently matches its saved unknown write and completed helper records, retains the fixed original bytes and contains exactly two commands. Read-only Hub database checks confirm original unknown Command/Run/Workflow/Step/dispatch, generation 1, unchanged binding, held/isolated slot, unknown budget settlement, exact write/helper retry classes and one owner-scoped canonical audit. The original fixture additionally asserts one write, one digest, zero duplicate/restart I/O, identical original snapshots and wrong-owner 404. Both helper and original remain bound to the predeclared capabilities and original node.

Evidence root `/Users/mango/.cache/agent-fleet/verification/capability-platform-20261030-edwzm7df` retains `verification-summary.json`, complete provenance, original commands/environment/logs/JUnit under `native-run-vre1ydys/`, six native-case audits per host and before/after source reports. `windows-native-evidence/` and `linux-native-evidence/` each preserve 32 copied files in verified ZIPs; all **64 files / 545,258 bytes** match their remote sizes/digests, including journals and retained original bytes. The independent Hub verifier's first comparison incorrectly expected a helper command id and `workspace_state`; its retained red script/logs and diagnosis show that the original contract requires descriptor evidence id and `workspace_check`. Only that evidence-only verifier was corrected; product code, fixtures and original assertions were unchanged.

This closes the fixed native workspace capability/postcheck matrix. The test-only `Fixture.isolate_write` setup remains explicit and **`full_workflow_completed=false`**; complete WF-04/05, NODE-LIVE, actual model/CLI, production isolation and external integrations remain open. Seven external checks stay `not_run`, all product gates default false, and all temporary evidence is retained.

## Corrected-source native follow-through — 2026-10-31

The native result above belongs only to `16f10ebd630d38fa53a839ce9d2c432719014120`. The later [terminal original-command guard](2026-10-31-postcheck-terminal-guard.md#corrected-native-result-and-full-run-blocker--2026-10-31) is independently frozen as `85488105cd2d3b610fcbfa18fc2273bd02618db1`, 898 files. Its unchanged original native fixture completes **12 passed / 0 failures/errors/skips** in fresh home-win/gz-cvm roots, pytest 49.85 seconds. All source digests before/after match; twelve read-only Hub checks, both journals and 64 copied files / 545,258 bytes pass their original audits. Every case preserves one write/one digest/zero restart I/O, original unknown and held/isolated reservation, with `full_workflow_completed=false`.

The same-source original Linux full invocation first aborts with exit 120 on a 100%-full root disk/inode allocation and produces no final JUnit/journey; that failure retains its own evidence. After the user's separate directory confirmations, verified fixture preservation/rechecks and two independent deletions, the unchanged corrected source completes the original full gate in a new root/basetemp: **5845 passed / 197 passing subtests / 0 failures/errors/skips**. Both original release verifiers pass, with 42 journeys, 1747 selectors and 44 PNGs; all 898 source hashes and 50 copied files / 7,940,720 bytes match. Evidence is under `/Users/mango/.cache/agent-fleet/verification/postcheck-guard-20261031-t0mae74m/full/rerun-prepared-7_12st95/`; see the [verified corrected full gate](2026-10-31-postcheck-full-gate.md#verified-corrected-source-rerun---2026-10-31). Native, baseline, failed and successful full runs retain separate scopes/counts. Complete Workflow/production/external acceptance remains open and all gates stay default false.
