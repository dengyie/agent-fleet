# Native Binary Media Inputs

Parent contract: [Workflow execution](../specs/2026-10-20-workflow-execution-contract.md#542-本地-native-run-的有界图片输入). Evidence indexes: [acceptance matrix](../../testing/workflow-platform-acceptance.md) and [development ledger](../../platform/development-completion.md).

**Goal:** Let explicitly registered local Native Workflow targets send a small, fixed set of verified image ArtifactRefs to an OpenAI-compatible vision model, while preserving immutable Run inputs and bounded provider requests.

**Non-goals:** Arbitrary binary files, audio/video, remote Node delivery, CLI/review/test execution, media decoding or resizing, OCR, browser preview, and security hardening beyond the input and request bounds required for this feature.

**Compatibility:** Existing Native snapshots (schema 1-4), workflow readers 1-12, Run config bytes, text-only provider wire, and targets with the media flag disabled must remain byte-for-byte compatible. New snapshots use schema 5 and reader 13. No base64 image data is persisted.

## Contract

- Only `kind=artifact` inputs on a trusted local, provider-backed `native_run` target with `inline_media_inputs=True` may be consumed as media. The flag is a strict boolean, defaults to false, and is part of the frozen target/config digest. It cannot come from the Workflow definition, operator request, or model input.
- Allow exactly `image/png`, `image/jpeg`, and `image/webp`; reject all other MIME values before provider I/O. Each image is at most 2 MiB; at most four logical input references and 4 MiB total logical raw image bytes per Run. Repeated references count against both limits, are read once per artifact, and remain distinct outgoing parts.
- The Native private `workflow_memory` schema 5 stores only ordered `{ref_key, artifact_id, sha256, size, content_type}` metadata. It stores no image bytes, data URL, base64, or generated preview. Current owner/workspace manifest and verified descriptor checks happen before use and again at the worker authorization boundary.
- The worker reads each unique image through the owner/workspace-scoped verified artifact port, checks exact size and SHA-256, then constructs OpenAI-compatible message content parts: text parts and `image_url` data URLs. Image bytes exist only in bounded attempt-local memory and the outgoing request body. A ready checkpoint re-reads the same immutable ArtifactRef; it never restores image data from checkpoint state.
- Add a hard 8 MiB serialized provider request-body limit. Requests exceeding it fail before transport dispatch with a stable request-size error. Existing text-only payloads remain unchanged.
- Provider capability is explicit in the frozen target/provider contract. A target must have media enabled and its pinned provider snapshot must declare image input support; unsupported providers fail before model I/O rather than silently stringify or omit media.

## Implementation Steps

- [x] Add media reference value types and canonical schema-5 parsing/building, preserving schemas 1-4 byte compatibility and all existing field/order checks.
- [x] Add a scoped media reader that verifies manifest scope, MIME, size, exact read length, and SHA-256, deduplicates physical reads, and enforces count/byte budgets without decoding the image.
- [x] Extend trusted target registration and dispatch admission for the opt-in flag and provider image capability. Reject mixed/unsupported contexts, remote targets, wrong MIME, missing readers, and incompatible provider snapshots before binding a Run.
- [x] Extend Native worker preparation/authorization and Run creation so only metadata is frozen; re-open verified bytes on each real attempt and `ready` checkpoint restoration, after existing lease/deadline/current-authorization checks and before provider creation.
- [x] Extend the provider message serializer to preserve only the validated multimodal part shape and enforce the exact serialized request-body byte limit before transport dispatch. Keep existing plain-string and tool-message handling unchanged.
- [x] Complete local regression coverage for the captured provider transport body (including exact data URL bytes), MIME/scope/hash/size/count/total-budget failures, opt-in admission, checkpoint replay, no media persistence in config/checkpoint/transcript, old snapshot compatibility, and zero Run creation on rejected inputs. Include duplicate logical references, readers 1/8/9/10/11/12, corrupt MIME/flag/reference/missing Run history, and plain/encrypted restore. This is bounded local coverage, not live vision or a frozen release gate.
- [ ] Complete source-frozen release verification and record the execution contract, workflow journey selectors, acceptance evidence, and development completion ledger. Local evidence does not close this release requirement.

## Verification

- Run focused Native media domain, artifact reader, dispatch/executor, worker recovery, provider serialization, and backup-history tests on the supported Python versions.
- Verify captured transport JSON contains exact OpenAI-compatible image parts and that over-budget bodies fail before transport invocation.
- Verify schema 1-4 snapshots and all existing text-only payload bytes remain unchanged; verify schema-5 references survive backup/restore without image bytes.
- Run the focused workflow journey selectors and applicable full test gate for the resulting source freeze. Do not claim production readiness from local tests; receiver dependency/configuration, complete Linux/browser gate, and actual production task/download/restart acceptance remain separate release blockers.

## Current Local Evidence — 2026-11-12

The initial red provider tests reproduced both defects: list content was stringified and oversized requests reached transport. The first Native worker run exposed that the global schema reader still stopped at 12; the inspector is now 13. Earlier 32/241-test results are historical subsets, superseded by the later runs below.

Additional failures reproduced loss of ordered legacy text validation, silently omitted media in a legacy snapshot, and provider creation after workspace/cancel/pause/deadline/manifest changes during image reads. Those fixes passed the seven-module 268-test regression set. The original worker now reaches the captured OpenAI-compatible HTTP transport for all three supported MIME types, attaches media to the exact Workflow trigger after an earlier conversation turn, and preserves `provider_pending` as unknown on recovery without reopening the image or resending the request. Captured transport uses a controlled responder, not a live vision service.

The next media-only run exposed a positional `RegisteredRunTarget` compatibility break and acceptance of boolean manifest sizes (2 failed / 21 passed). The flag now follows all legacy fields, and manifest sizes require exact integers. The first expanded rerun retained one test-fixture error (`target_key` differed from `execution_node_id`): 1 failed / 273 passed; correcting that fixture made both original regressions pass. A separate red diagnostic test reproduced `provider_message_invalid` being reduced to generic `provider_error`; the known diagnostic allowlist has been updated. Final combined verification of 13 modules passed **383 tests in 43.11 seconds**, exit 0, including the original worker/checkpoint/remote suspension/dispatch and architecture boundaries. Source remained an uncommitted local worktree; this is not a frozen release report.

The next boundary run reproduced an unsupported code-reference domain error escaping the dispatch layer (1 failed / 28 passed). Translate that validation failure into `WorkflowDispatchError` before artifact I/O or Run binding. Duplicate references now explicitly prove one physical read per artifact while preserving each outgoing logical part; five references and logical byte overflow fail before manifest/descriptor I/O. History checks reject reader downgrades and corrupt MIME/flag/reference/missing Run at startup and backup, and plain/encrypted restore preserve metadata and immutable artifact bytes. The first history runs had fixture import/foreign-key setup errors; those reports are retained and are not product failures.

Final local runs on Python 3.10 and 3.14 each passed **159 tests** in the four-module boundary set and **402 tests** in the expanded 13-module associated set (105.57s / 108.58s respectively). Reports: `/var/folders/6k/ywx2jbq15glcz4g_qr_wrmcc0000gn/T/agent-fleet-loopback-cause-ivwas9c0/media-associated-final-py310.xml` and `media-associated-final-py314.xml`; the earlier failing dispatch evidence is `media-boundaries-red-py314.xml`. WF-06 includes the six additional selectors. Formal `pytest tests --collect-only` collects 7434 cases; all 2495 selector entries across 46 journeys resolve. Ruff production checks and UTF-8/no-BOM checks pass. This collection result does not mean all journeys passed.

Reports have also been copied to `/Users/mango/.cache/agent-fleet/verification/native-media-boundaries-20261112-neh4d9jp/`. The separate full diagnostic run passed 7405 cases with 11 platform skips and 204 passing subtests, including the earlier ten loopback failures. Its source changed after collection and it collected 18 fewer cases than the final set, so it is not a source-frozen release gate; the intermittent loopback root cause remains unproven.

This slice remains unpublished. Complete Linux/browser verification, source-frozen full regression, production configuration and live task/download/restart acceptance remain open. Historical coverage is limited to the explicitly listed cases above; it does not close F5 or the project roadmap.
