# Native Audio Inputs Implementation Plan

> **For agentic workers:** Execute inline in this task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let explicitly registered local Native Workflow targets pass bounded WAV/MP3 ArtifactRefs to an audio capable OpenAI-compatible Chat Completions provider.

**Architecture:** Reuse the existing verified ArtifactRef reader and `inline_media_inputs` admission boundary. Freeze audio capability and ordered reference metadata in a new Native memory schema/reader version; produce the provider's `input_audio` part only inside the attempt after current authorization. Keep image schema 5 byte compatible and keep unsupported media/context rejected before Run binding.

**Tech Stack:** Python 3.10+, Flask application services, SQLite-backed Native Workflow memory, stdlib base64/JSON, pytest and existing captured provider transport tests.

## Global Constraints

- Audio MIME types are exactly `audio/wav` and `audio/mpeg`; provider wire formats are exactly `wav` and `mp3`.
- Reuse the existing 2 MiB per-reference, four-reference, 4 MiB aggregate raw-media and 8 MiB serialized request bounds.
- Audio target and provider capability default off; only local provider-backed `native_run` supports this slice.
- Audio bytes, base64 and data URLs never enter workflow definitions, Run snapshots, events, checkpoints or backup metadata.
- Existing text, code, image schema 1-5, reader 1-13 and text-only/image-only provider request bytes remain unchanged.
- Do not add dependencies, change production configuration or run live model requests.

---

### Task 1: Domain contract and Native snapshot version

**Files:**
- Modify `hub/domain/workflow_media_inputs.py`
- Modify `hub/domain/workflow_native_memory.py`
- Modify `hub/infrastructure/workflow_schema.py`
- Test `tests/test_workflow_native_media_inputs.py`

**Interfaces:** `WorkflowAudioInput(ref_key, reference, content_type)`; `parse_audio_inputs`; `audio_input_value`. Audio snapshots use schema 6 and ordered `audio_inputs`; image snapshots remain schema 5 with `media_inputs`.

- [x] Add regression tests for canonical WAV/MP3 metadata, unsupported MIME, wrong fields, booleans masquerading as sizes, ordering, duplicate references, and schema 1-5 byte compatibility.
- [x] Preserve a red-phase report proving the new tests fail against the pre-audio implementation. The `15b439c66aab540eb2f53dced80f5000bab86a9d` baseline snapshot lacks the media-input module; collecting the added audio tests fails with `ModuleNotFoundError` before any new API can be exercised. The full log and JUnit are retained at `/Users/mango/.cache/agent-fleet/verification/native-audio-red-20261112-retry/`.
- [x] Add the immutable audio reference type and schema-6 parser/builder. Preserve all existing media/image serialization paths.
- [x] Run the focused domain/history test set and verify schema-5 image snapshots are unchanged.

### Task 2: Scoped bounded artifact reads and admission

**Files:**
- Modify `hub/application/workflow_media_inputs.py`
- Modify `hub/infrastructure/workflow_run_executor.py`
- Modify `hub/infrastructure/workflow_native_memory_repository.py`
- Modify `hub/application/run_worker_service.py`
- Test `tests/test_workflow_native_media_inputs.py`

**Interfaces:** `WorkflowMediaInputs.collect` and `authorize` accept audio references only when the pinned provider capability is `audio_input is True`. `load_audio` returns `{"type":"input_audio","input_audio":{"data":<base64>,"format":"wav"|"mp3"}}` parts.

- [x] Add regression tests for absent capability, off target, unsupported MIME, manifest scope/hash/size changes, aggregate bounds, repeated references and no Run/reader I/O on rejected admission.
- [x] Extend the existing verified-descriptor reader to collect, authorize and load unique audio artifacts; repeated references count logically toward bounds but read once.
- [x] Store only ordered audio metadata under `workflow_memory` schema 6; set the existing media mode marker and minimum reader version 14 for audio, retaining version 13 for images.
- [x] Add current authorization checks before read and again before provider construction; safe-ready recovery rereads the immutable reference, while pending provider state remains unknown and never resends.
- [x] Run focused admission, worker, cancellation/deadline, artifact history and backup tests.

### Task 3: Provider transport and end-to-end evidence

**Files:**
- Modify `hub/platform/providers/openai_compatible.py`
- Modify `tests/test_platform_provider.py`
- Modify `tests/test_workflow_native_media_inputs.py`
- Modify `docs/superpowers/specs/2026-10-20-workflow-execution-contract.md`
- Modify `docs/testing/journeys.json`

**Interfaces:** `_message_content` accepts only the exact validated `input_audio` structure with strict base64 and `format` validation, then preserves it in the Chat Completions JSON body.

- [x] Add provider serializer tests for WAV/MP3 wire structure, invalid base64/format/extra fields, mixed text/audio ordering, and the existing 8 MiB pre-transport limit.
- [x] Serialize bounded audio parts without changing string, image, tool or text-only messages.
- [x] Exercise an actual Native Run worker through the captured OpenAI-compatible transport and assert the exact body, artifact bytes/hash, ordered context, no persistence of bytes, and no second provider send during recovery.
- [x] Add exact WF-06 selectors and document the closed audio subset and still unsupported video, CLI, review, fixed-test and remote contexts.
- [x] Run focused tests, formal collection, CI Python lint rules for changed modules, UTF-8/no-BOM and `git diff --check`.

### Task 4: Record remaining release state

**Files:**
- Modify `docs/superpowers/plans/2026-10-20-workflow-platform-roadmap.md`
- Modify `docs/platform/development-completion.md`
- Modify `docs/testing/workflow-platform-acceptance.md`
- Modify this plan

- [x] Record exact passing commands and reports; leave the current dirty source tree's full release gate open until a new immutable freeze passes.
- [x] Mark only local WAV/MP3 Native input implemented; retain video, non-Native contexts, production configuration, real model, task/download/restart and external integration acceptance as open.
- [x] Recheck the documentation link targets, UTF-8 without BOM and `git diff --check`.

## Current Verification — 2026-11-12

An isolated Python 3.10.21 venv now uses all 25 applicable wheels from `requirements-lock.txt`, downloaded after checking compatible tags and per-file sizes (largest 3,943,588 bytes) and verifying each SHA-256. Offline installation and `pip check` passed. The same 14-module association set passed **425 tests, 0 skipped, 48.05 seconds**, exit 0; all 1,221 worktree file hashes matched the source manifest before the subsequent documentation update. This resolves the earlier missing-dependency collection gap for the audio subset. Reports and dependency provenance are at `/Users/mango/.cache/agent-fleet/verification/audio-python310-20261112/`; no global Python environment was changed.

The earlier Linux `c6bbdc81` transfer snapshot was found to contain 1,221 unintended AppleDouble sidecars in addition to its 1,221 real files, and its runner omitted the existing private Node/bwrap/rsync directory from PATH. Its terminal **7,328 passed / 99 failed / 41 skipped** report is preserved independently. A Python-tarfile archive now contains exactly the named worktree files with per-member hashes and no sidecars; clean freeze `dc6a42b83de78c209b88c0cadea3b4c68281faf0` matches those 1,221 files. Product and test bytes match the earlier snapshot's real files; only three verification documents changed before this freeze. With the original private tool paths restored, Linux preflight passed **38 tests, 0 skipped** (71.13 seconds), including actual browser, rsync, sandbox and audio cases, with source hashes unchanged. The original full gate is running in unit `agent-fleet-exact-candidate-20261112.service`, evidence `/var/tmp/agent-fleet-runtime-corrected-20261112/`. This is not yet a full release pass. Details and correction of the earlier deadline-failure attribution are in the [ledger](../../platform/development-completion.md#精确快照与验收运行环境修正2026-11-12).

Fresh Python 3.14 run of the 14-module media association set passed **425 tests, 0 skipped** in 37.65 seconds. The set covers Native audio/image inputs, text/code/memory compatibility, provider serialization, checkpoints, history, backup, dispatch and recovery. A separate three-module media/provider run passed 89 tests; the focused six-module regression passed 278 tests. The exact module list for the 425-test run is the set recorded in the session's command output; the associated XML from the earlier image-only 402-test run does not include the later audio module and must not be cited as the audio result.

The initial `python3.10 -m pytest tests/test_workflow_native_audio_inputs.py tests/test_workflow_native_media_inputs.py tests/test_platform_provider.py -q` could not collect because that host interpreter lacked `cryptography`; no global package installation was attempted. That historical failure is superseded for the local association set by the isolated 425-test run above. Prior Python 3.10 image-only results remain limited to their original scope.

Red-phase evidence: the audio test file was copied unchanged (SHA-256 `49d887619225074af356ec962f53d240774dd3cc3775866399510bfe1de1c0fc`) into a `git archive` snapshot of baseline `15b439c66aab540eb2f53dced80f5000bab86a9d`. `python3 -m pytest tests/test_workflow_native_audio_inputs.py -q --junitxml=...` exited 2 during collection because `hub.application.workflow_media_inputs` did not exist in that pre-media baseline. This proves the baseline lacks the new feature surface; it is not a fine-grained assertion against an image-only implementation.

Formal collection with `--require-journeys --journey-report` found **7,461 test instances, 46 journeys, 2,508 selectors, 0 missing selectors**. `--collect-only` intentionally leaves every selector unexecuted, so the strict journey run exited 1 with 0/46 journeys passing; it is collection evidence only, not a behavioral or release pass. Python 3.14 CI-required lint rules passed for the changed production/test modules, 16 relevant source/doc files decoded as UTF-8 without BOM, and `git diff --check` passed. Broader Ruff rules still report pre-existing diagnostics in touched legacy files; a baseline comparison found one newly introduced import-order diagnostic, which has been fixed.

The current source tree remains dirty. A fresh full macOS/Chrome run of this worktree finished **7,449 passed, 12 platform skips, 0 failures/errors, and 204 passing subtests** (17:09). The JUnit counts 7,665 entries including subtests; the journey gate was **43/46**, with only Linux-only `NODE-01`, `RELEASE-01`, and `WF-01` selectors unexecuted. The strict run therefore exited 1, and `release_evidence` rejected the checkout as `dirty_checkout`. JUnit, journey report and 51 screenshots are preserved at `/Users/mango/.cache/agent-fleet/verification/current-candidate-macos-20261112/`.

A Linux source snapshot (`c6bbdc8130b3a6379862a421c8143642a2014965`, later proven to include unintended AppleDouble metadata) was started on the independent verifier. Its first run stopped at 10% because the host's 987 MiB `/tmp` tmpfs filled: the actual temp tree contained 452 MiB of generated multipart fixtures; the root filesystem still had 11 GiB free and cgroup OOM counters stayed zero. The 708 MiB pytest temp directory was removed after saving the report. The same frozen snapshot was then rerun with `TMPDIR` redirected to the candidate's dedicated scratch directory on the root filesystem; its failed terminal report and the replacement exact freeze are recorded above. The ENOSPC run remains infrastructure-invalid evidence, not a product test result.

Complete same-source Linux/browser verification, the full pinned CI runtime, real audio model behavior, production dependencies/configuration, and actual production task/download/restart acceptance remain open. The isolated Python 3.10 association result does not close these broader requirements.
