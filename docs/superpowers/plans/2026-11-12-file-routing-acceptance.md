# File handoff and capability routing acceptance plan

Execute inline with the existing `verification-before-completion` workflow. Return to the [development ledger](../../platform/development-completion.md), [WF-04/05 acceptance requirements](../../testing/workflow-platform-acceptance.md#6-wf-04真实文件交接) and [F4 roadmap](2026-10-20-workflow-platform-roadmap.md#6-f4文件与节点).

**Goal:** Audit each existing WF-04/05 requirement against executable assertions and current-source physical-host evidence, then register supported automated journeys without claiming production or unverified external capabilities.

**Architecture:** Reuse existing transfer, recovery, Node journal, capability coordinator and reservation implementations. Keep native host controllers opt-in and separate from normal CI. Use the preserved immutable product freeze `49a83f2313a0fed3c1139813065abd7b4b33c4c1` for current product evidence; later journey/document changes do not rewrite the executed matrix or historical reports. No new runtime abstraction or product behavior without failing evidence.

**Tech stack:** Existing Python 3.10+ code, pytest, SQLite, actual home-win/SG Linux, existing bubblewrap and private runtimes. No downloads, installs, global configuration, deletion, services, development commits, push, merge or deployment.

## Task 1: Requirement coverage audit

**Files:** `docs/testing/workflow-platform-acceptance.md`, existing artifact/capability test modules and `docs/testing/journeys.json`.

- [x] Map each §6 bullet and §7 invariant to named original tests, including maximum file bytes, process deaths, unified slots, fixed/dynamic physical capacity, original input pinning, restart/revocation and unknown postcheck.
- [x] Read the relevant assertions and distinguish controlled HTTP/OS evidence, real-host evidence and the final complete Linux/browser release gate. Identify gaps explicitly rather than marking tests as sufficient from names alone.

## Task 2: Fresh product evidence

**Files:** Existing `tests/fixtures/workflow/{windows_artifact_probe,windows_artifact_export_probe,two_host_handoff_probe,capability_hub_probe,capability_lifecycle_probe}.py`; no fixture changes planned.

- [x] Verify the preserved freeze hashes and clean status locally and on both hosts before execution. Preserve original directories and reports.
- [x] Run the mapped artifact/capability suites on Linux using its existing private Python/tools, writing new command/JUnit/log files outside source.
- [x] Run native Windows receive/export/kernel-lock/process-death probes with a short fresh private basetemp.
- [x] Run actual Windows→Hub→Linux source handoff and independent fixed validation, then actual Windows/Linux automatic lifecycle and held/isolated postcheck scenarios. Use original stdin configuration and new evidence roots; no external model/mail calls.
- [x] Inspect original Hub/Node journals, persistent identities, zero-replay counters and actual file bytes; compare all source hashes after execution and verify copied report hashes.

## Task 3: Register evidence and preserve remaining scope

**Files:** `docs/testing/journeys.json`, `docs/testing/workflow-platform-acceptance.md`, `docs/platform/development-completion.md`, this plan.

- [x] Register WF-04/05 automated selectors only after requirement audit and fresh execution. Keep native probes external opt-in, platform limitations explicit, and no skip exemption in the journey gate.
- [x] Verify all matrix selectors resolve and the existing journey/release-evidence gate regressions pass. Run UTF-8/no-BOM, Python 3.10 syntax, applicable lint and diff checks.
- [x] Record exact results and unresolved requirements in the original acceptance/ledger/roadmap. Same-version complete Linux/browser, production receiver/configuration/task acceptance and other project TODOs remain open until independently proven.

## Requirement audit

The automated selectors below are registered through the complete relevant test modules, with additional original fixed-target/control/publication selectors. They are not a substitute for the separate opt-in host controllers or final release gate.

| Requirement | Concrete assertion/evidence |
| --- | --- |
| Legacy 10 MiB vs V2 64/256 MiB, zero bytes, 256 parts | `test_artifact_transfer.py`: `test_large_file_is_streamed_and_legacy_limit_is_unchanged`, `test_default_limit_and_zero_byte_part_are_rejected`, `test_zero_file_needs_no_parts`, `test_protocol_hard_limits_cannot_be_configured_away`, `test_maximum_256_parts_are_out_of_order_resumable_and_memory_bounded` writes and commits actual 256 MiB in reverse order, verifies hash and <16 MiB Python peak. |
| Part conflict, bad lengths/hash, missing/overlapping bytes, interrupted/slow bodies | Same module: `test_bad_part_is_rejected_before_disk_write`, `test_conflicting_part_does_not_overwrite_original_bytes`, `test_reservation_conflict_and_missing_chunk`, `test_disconnected_body_has_stable_error_and_writes_nothing`, `test_slow_body_cannot_outlive_reservation`. WSGI's underlying blocking socket timeout remains the server/proxy responsibility; application checks bound reads before/after. |
| Publication death before rename, after rename, after DB-ready; fixed artifact ID | `test_artifact_transfer_recovery.py::test_actual_process_exit_after_rename_preserves_one_fixed_artifact` checks actual child exit 73 in all three windows, same artifact ID and exactly one published directory. Missing/hash/reference damage is quarantined by `test_ready_with_missing_or_inconsistent_content_is_quarantined`. |
| Quota and actual failed staging; dry-run cleanup; backup | `test_two_os_processes_cannot_overbook_last_capacity` covers node/global/workspace/workflow limits. Recovery `test_expired_reservation_keeps_actual_failed_staging_charge`, `test_partial_write_disk_full_remains_charged`, `test_reference_kept_in_dry_run_even_after_quarantine`, plaintext/encrypted round trips. No cleanup deletion performed by this acceptance. |
| Node identity, actual HTTP bytes, receipt before ready, source tree/blob, fixed validation | `test_workflow_two_nodes.py` complete module and `test_artifact_export.py::test_two_node_http_export_receive_and_fixed_source_validation`; actual-host `two_host_handoff_probe.py` separately proves Windows producer→Hub→Linux receiver, one upload/download, lost response recovery, original grant/transfer/command and two independently executed fixed cases. |
| Receive scope/path/link/overwrite, independent hash, extraction; portable and Windows kernel boundary | `test_artifact_receive.py` complete module, `test_workflow_two_nodes.py` grant/credential/receipt checks, source extraction in `test_received_git_bundle_is_rehashed_extracted_and_explicitly_validated`; explicit Windows receive/export probes exercise protected NTFS roots, pinned handles, mapping/share exclusion and original bytes preserved. |
| Receive restart, delayed/lost receipt, no automatic execution | `test_process_crash_recovers_original_target_and_receipt` checks four actual exit windows; `test_lost_receipt_response_restarts_from_durable_outbox_without_redownload` and the two-host probe require same receipt and zero validation until an independently authorized operation. |
| Export binding, fixed fd/hash, change detection and same-ID recovery | `test_artifact_export.py` complete module includes three lost-response stages, four actual child exits, tampered/expired/schema/attempt-budget journal failures, signed registered outputs and credential changes. Native Windows export probes verify actual locked source handles and metadata changes. |
| Unified upload/download node 2, Hub 4, cross-process final slot and process-death release | `test_artifact_capacity.py` complete module includes real spawn-process upload/download competition and exit 73; `test_runtime_send_and_receive_share_two_local_slots` checks the same Node pool; explicit Windows kernel two-slot/process-exit tests prove the OS-specific branch. |
| Authority/platform/software/revision/policy/resource filtering and reason priority | `test_capability_router.py` full module checks exclusion reasons and snapshots; Node self-annotations fail schema parsing. Legacy boolean capability alone has no offer and cannot satisfy selection. |
| Priority → held occupancy → node ID, independent of input order | Existing permutation test previously only chose a higher-priority/lower-ID winner, so occupancy was not independently decisive. Three added parameter scenarios isolate priority-before-occupancy, occupancy-before-ID and equal-key node-ID ordering. No product sorting change. |
| 120-second exact boundary, future/invalid timestamps, boot refresh, unchanged browser contract | Router accepts 220 for received_at=100 and rejects 220.001/now=99/other boot; malformed timestamps reject. `test_offer_restart_requires_a_new_authenticated_report_even_at_same_wall_time`, coordinator restart and real HTTP report checks cover persistence. Product change is zero, including the browser's separate freshness behavior. |
| Last-slot atomic admission and no partial intent/budget/binding | `test_capability_reservations.py::test_two_real_processes_cannot_oversell_last_node_slot`, coordinator real-process routing race, trigger-injected write rollback, original credential transaction recheck. |
| Fixed/dynamic shared physical target, held/isolated vs released, other owners, generation guard | `test_workflow_repository.py::test_isolated_reservation_counts_for_fixed_target_capacity` (state × owner), `test_two_process_fixed_admissions_include_isolated_slot`, `test_workflow_control.py::test_isolated_reservation_blocks_new_generation`, plus dynamic resource aggregation. |
| Input/target pinning, no drift after new offers/priority/software, restart/removal/revocation | `test_capability_coordinator.py` full module includes same-transaction receive target, actual HTTP receipt→original Run/Command, same-generation no-drift, new authenticated boot report, original dispatch after registration removal, revocation transaction and disabled gate/controls. |
| Original actual Node lifecycle and unknown retention | Local lifecycle/dispatch/Node full modules plus opt-in actual Windows/Linux eight automatic success/matched/mismatch/read_failed scenarios; twelve separately seeded held/isolated postcheck scenarios. Independent original journals show one write, at most one read-only digest, acknowledged outbox and zero replay I/O; unknown never releases reservation. |

## Product-freeze verification — 2026-11-12

Evidence root: `/Users/mango/.cache/agent-fleet/verification/file-routing-20261112-tvagu9m9/`.

- Preserved product freeze `49a83f2313a0fed3c1139813065abd7b4b33c4c1`, 1195 source files: Linux 18 modules **707 passed/0skip**, 301.75s; Windows neutral/native receive/native export **133 passed/0skip**, 11.26s. Before/after every source SHA and Git clean status match; copied XML/log/command SHA verified under `linux-results/` and `windows-results/`.
- Original same-source opt-in controllers: **21 passed/0skip**, 118.52s: one actual two-host handoff with two separately executed fixed test cases; eight automatic lifecycle and twelve deliberately held/isolated postcheck cases. The nested fixed cases are not added to controller totals. `native-controllers.xml`, raw operation reports and `handoff-summary.json` retain original identities.
- Independent read-only audit of 20 distinct original Hub databases verifies integrity/FK, one original Run/dispatch/binding, original command retry class, success settlement and digest release, unknown retention/no artifact fabrication. Initial inventory also followed pytest's `current` symlink aliases; the corrected inventory explicitly excludes aliases instead of counting repeated databases. Original tests/reports unchanged (`hub-audit.json`).
- Both physical-host journal/file audits independently verify ten cases each, original terminal state and acknowledged outbox, single write and original retained bytes. Source digests and clean state remain unchanged after controllers. Archive/report copy validation is recorded separately; final registered-matrix run remains to be executed on its own frozen source.

## Registered-matrix scoped gate — 2026-11-12

- New immutable evidence snapshot `bd2f0ea0e28e20812ea57f5f420c827f2c45ca7e`, 1196 files; source bundle 7573410 bytes. Product files are byte-identical to the actual-host `49a83f23` freeze (`product-equivalence.json`); changes are documentation, test fixture correctness, ordering assertions and journey registration. No development branch commit occurred.
- Full registered matrix: 46 journeys, 2440 selectors, 7286 collected instances, no missing selectors (`registered-collection.json`). Collection does not certify execution.
- Linux original journey plugin on the exact WF-04/05 rows, using an explicitly scoped matrix: **567 passed, zero failures/errors/skips, 2/2 scoped journeys, 263 selectors with every parameter/setup/call/teardown passed**, 215.26s. WF-04 has 116 selectors, WF-05 has 147. Original command, copied JUnit/log/journey/matrix and verified SHA-256 are in `registered-linux-results/`; all source hashes remained unchanged and Git checkout clean.
- Scoped matrix digest and full registered-matrix digest are independently checked, including exact row equality. This two-journey gate is not full Linux/browser release evidence; no release verifier was told to treat the remaining 44 journeys as executed.
- Local router/journey/release-evidence regression: 191 passed/0skip, 12.29s. An existing unused NodeOffer import in the touched test was removed after the freeze; current router module independently passes 57 cases/0skip, lint passes, assertions remain identical. `routing-order-mutation-check.json` verifies that the real router passes all scenarios while deliberate priority/occupancy/node-ID mistakes fail; the previous combined scenario accepted both priority/occupancy mistakes.
- Native journal/file archives contain 106 files / 910132 bytes; handoff artifacts/journals contain eight files / 83582 bytes. Archive SHA and each member size/SHA match (`native-copy-audit.json`, `handoff-copy-audit.json`). The Linux copied fixed report/JUnit match the original controller hashes and actual source bytes. An initial handoff audit selected the schema metadata table and failed before output; corrected helper explicitly queries original `artifact_exports`/`artifact_receives`, preserving the original controller result and failed diagnostic console.
- Full final Linux/browser release gate remains pending explicit approval for the >10MB Chromium/libllvm15 downloads. Production receiver/config/model/Node/task/recovery acceptance, external services and the overall all-TODO goal remain open. No service mutation, downloads, cleanup, development commit/push/merge or deployment.
