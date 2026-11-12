# Native Video Inputs Implementation Plan

> **For agentic workers:** Execute inline in this task using the existing evidence-first workflow. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pass complete, bounded MP4 ArtifactRefs from an explicitly registered local Native Workflow to a video-capable OpenAI-compatible Chat Completions provider, with original Run recovery and backup behavior.

**Architecture:** Extend the existing scoped verified media reader, immutable Native memory snapshot and attempt-only provider parts. Video uses schema7/reader15 and the pinned `video_input is True` capability. Preserve schema1–6 byte formats and all current image/audio behavior; no frame extraction, new runtime, alternate queue or media persistence.

**Tech Stack:** Python3.10+, stdlib base64/JSON, original SQLite Run/Workflow repositories and existing pytest provider transport fixtures.

## Global Constraints

- Exact MIME `video/mp4`; complete payload `{"type":"video_url","video_url":{"url":"data:video/mp4;base64,..."}}`. Format confirmed against [vLLM's documented multimodal Chat Completions example](https://docs.vllm.ai/en/stable/examples/generate/multimodal/), not inferred from image support.
- Existing2MiB/item,4logical references,4MiB total raw input and8MiB serialized request limits remain. Repeated positions retain ordering/count/budget but read each immutable artifact once per attempt.
- Only local provider-backed Native targets with`inline_media_inputs=True` and pinned`video_input=True`; no remote/CLI/review/test or mixed-modality mode in this slice. Other contexts and real video model trials remain full-project requirements.
- Full video bytes/base64/data URLs exist only during one attempt and its provider request; snapshots/checkpoints/events store scoped/hash-bound metadata only.
- Admission, worker reads, post-read binding/cancellation/deadline checks, safe-ready recovery, pending-unknown recovery, history closure and plain/encrypted backup use their original authorities.
- No dependency installation, production configuration, merge or deployment in this implementation.

### Task1: Domain and provider contract

**Files:** `hub/domain/workflow_media_inputs.py`, `hub/domain/workflow_native_memory.py`, `hub/platform/providers/openai_compatible.py`, `tests/test_workflow_native_video_inputs.py`.

**Interfaces:** immutable`WorkflowVideoInput(ref_key,reference,content_type)`;`video_input_value`;`parse_video_inputs`;`build_native_memory(...,video=(),video_mode=False)`; schema7`video_inputs`; provider exact`video_url`data URL.

- [x] Add failing tests for actual Native admission/body and direct provider video serialization; save the unmodified-current-code red log/JUnit.
- [x] Add the video metadata/parser/builder, logical aggregate bounds and canonical schema7. Preserve existing schema1–6 exactly.
- [x] Extend provider serialization with strict part/URL/MIME/base64 and per-message/request budgets; invalid/oversized parts fail before transport.
- [x] Run the focused video tests and original image/audio/provider tests.

### Task2: Original admission, worker and historical recovery

**Files:** `hub/application/workflow_media_inputs.py`, `hub/infrastructure/workflow_run_executor.py`, `hub/infrastructure/workflow_native_memory_repository.py`, `hub/application/run_worker_service.py`, `hub/infrastructure/workflow_schema.py`; same video test module and existing startup test module.

**Interfaces:** `WorkflowMediaInputs.collect/authorize` accepts video only with pinned capability;`load_video` returns exact complete-video parts. `NativeWorkflowMemoryGuard.load_video` rechecks current authority before/after bounded read; reader15 covers new snapshots.

- [x] Extend original verified-descriptor reads to video; reject mixed types or unavailable capability before binding.
- [x] Store only ordered metadata in the original Run transaction and raise minimum reader to15 atomically; no upgrade for old/no-video Runs.
- [x] Add worker video part insertion and current-authority guards. Preserve original pending-provider unknown and safe-ready reread behavior.
- [x] Verify missing/corrupt history, downgrade reader, changed manifest/scope, cancellation/pause/deadline during read, repeated refs and plain/encrypted backup with actual repositories.
- [x] Verify normal file bootstrap and Run recovery use the pinned video capability even when a catalog profile changes.

### Task3: Full scope record and publication preparation

**Files:** execution contract§5.4.4, Workflow roadmap, development ledger, Workflow index, journeys.json, this plan.

- [x] Add exact new selectors and the opt-in transport contract; retain real video models, non-Native contexts, remaining UI/automatic summary/archive/production TODOs as open.
- [x] Run associated media/provider/Run/history/backup/startup/package/architecture tests, required Ruff, Python3.10 grammar, UTF-8/noBOM anddiff checks.
- [ ] Record actual red/green commands and outcomes. Commit only verified code; publish a separate exact-tree increment onto reviewed PR44 with its own complete CI and keep its release evidence separate.

Return to [Workflow index](../../platform/workflow-platform/README.md), [roadmap](2026-10-20-workflow-platform-roadmap.md) and [development ledger](../../platform/development-completion.md).

## Verified implementation — 2026-11-13

Original current-code red:3failed,zero errors/skips in`native-video-20261113/red.xml`; failures are unsupported video target, rejected video wire and missing immutable video type. First92-case association has91passed/1failed because `_existing` omitted the new video mode when recovering the committed Run; exact mode check fixed. The later150-case run has149passed/1failed from a test endpoint fixture using `/` instead of the provider's full `/v1/chat/completions` URL; no product endpoint behavior was changed. All reports are retained separately.

A1103-byte16×16MP4 generated once by existingffmpeg contains2frames verified withffprobe;SHA-256`10df2ff54573bdd08f65e7f7775bf4a3c2c457e0e58f5cffec3e1e9bc710e6b2`. The test embeds its complete base64 and requires no codec runtime. Actual local HTTP receives the full video part from the original Native worker. Normal file bootstrap/restart retains the original video capability after a catalog model edit. Current execution changes during video read block provider creation; repeated logical inputs read once; ready recovery reads again, provider-pending recovery isunknown and does not reopen or resend. Old readers, gate-off history, corrupted MIME/marker/ref/missing Run and plain/encrypted frozen restore are covered.

Final20-module association:`associated-final.xml`,**652passed,2passing subtests,zero failure/error/skip,67.52s**. Exact command:

```bash
PYTHONPATH=.:tests /Users/mango/project/codex/agent-fleet/.venv/bin/python -m pytest \
  tests/test_workflow_native_video_inputs.py tests/test_workflow_native_audio_inputs.py \
  tests/test_workflow_native_media_inputs.py tests/test_workflow_native_startup.py \
  tests/test_workflow_native_memory.py tests/test_workflow_native_text_inputs.py \
  tests/test_workflow_native_text_recovery.py tests/test_workflow_native_code_inputs.py \
  tests/test_platform_provider.py tests/test_provider_transport_boundaries.py \
  tests/test_native_run_checkpoints.py tests/test_workflow_summary_inputs.py \
  tests/test_platform_backup.py tests/test_platform_backup_encrypted.py \
  tests/test_workflow_repository.py tests/test_workflow_dispatch.py \
  tests/test_workflow_boundaries.py tests/test_release_layout.py \
  tests/test_runtime_dependencies.py tests/test_auto_deploy.py -q
```

Earlier19-module610pass and publication association202pass/2subtests remain their own scopes. Required Ruff and all rules for the new test module pass. Local runtime isPython3.14; available standalonePython3.10 lacksFlask, so it did not run this suite. Source3.10grammar and the actualWindows3.10CI are independent gates, not inferred runtime compatibility.

The vault's unrelated rebase has now completed. Itsmaster branch and clean relevant files were checked; existingcanonical runbook, project/development indexes and two router twins have been updated onmaster. Other vault work remains untouched. Native video publication/fullCI, realvideo provider and the complete projectTODO remain open until their own actual evidence.

Vault navigation/governance scan completed with exit0/OK,zero errors/warnings,366 inspected documents and49 route rows. This confirms its declared router/one-hop scope, not full-vault facts or anchor certification.
