# Agy Native History Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task in this session.

**Goal:** Import existing local Antigravity CLI conversations through the established encrypted session channel, without starting an Agent or changing native storage.

**Architecture:** Register the exact Agy conversations directory in the existing profile registry. Isolate the observed SQLite/protobuf allowlist reader in `tools/session/agy_storage.py`; implement recoverable per-step publication in `tools/session/agy_archive.py`, called by the existing bounded archive worker. Preserve Hub ingestion cursors, redaction, quotas, operator API and unmanaged authority.

**Tech Stack:** Python stdlib, existing encrypted spool, Flask/SQLite and pytest; no new dependency.

## Global Constraints

- Backend/node only; preserve native files, credentials, active processes and settings.
- Keep `hub -> fleet_core <- tools`; no native CLI/model/OAuth calls or deployment implementation changes.
- UTF-8 without BOM. Work in the isolated `codex/agy-native-history` branch; rebase latest main before publication.
- Source schema is derived from local installed CLI descriptors, not guessed field strings. Never ship descriptor/binary dumps or user conversation content.
- Read only explicitly visible user/assistant/tool data; exclude system, thinking, signatures, credentials, generator/executor metadata and arbitrary embedded protobuf objects.
- Imported histories remain unmanaged, best effort and read only. Import does not grant ACP resume authority or infer an account binding.

## Reproduction and fix boundary

Local default sources contain Codex/Claude/Pi/ZCode only; production Mac directory has 528 rows and no Agy rows. Native Agy has 31 per-conversation SQLite databases under `~/.gemini/antigravity-cli/conversations`, with `trajectory_meta.cascade_id`, indexed `steps` and protobuf `step_payload`; a sibling summary SQLite holds native titles. The existing worker scans only `*.jsonl`. This is an absent native archive source/reader, not an HTTP rendering issue.

### Task 1: Read-only native projection

- [x] Add fail-first fixture shaped from observed schema, proving default discovery and actual HTTP import currently fail.
- [x] Add bounded SQLite snapshots, UUID/file/cascade identity checks, schema/format checks and nonrecursive protobuf wire decoding with explicit visible field tags.
- [x] Project native title (preview fallback), start timestamp and explicitly observed model names; visible text/tool/error content only. Preserve native timestamps and stable hashed step/message/tool identities.
- [x] Test identity mismatch, unsupported format, malformed/truncated protobuf, unknown fields, bounds, SQLite failure and unchanged native bytes.

Files: `agent_profiles.py`, `tools/session/agy_storage.py`, `tests/test_agy_native_history.py`.

### Task 2: Recoverable history publication

- [x] Integrate exact `*.db` discovery, sibling DB/WAL fingerprints and finite scan slices without scanning unrelated `.gemini` trees.
- [x] Atomically checkpoint only after a step's events enter spool; stable event IDs recover interrupted writes and lost HTTP responses. Invalid checkpoints fail closed.
- [x] Stop cursor at an in-place active step while publishing stable snapshot updates. Repeated unchanged scans do not append new snapshots. Completion permits later rows to progress.
- [x] Split large visible text into bounded stable snapshot blocks; redact full tool values before preview/source splitting. Unknown or oversized source data has explicit gap semantics.
- [x] Test quota/disk/HTTP failure, restart, active updates, titles changed without content changes, repeated scans and cross-platform paths.

Files: `tools/session/agy_archive.py`, `tools/session/sync.py`, `tools/session/bridge.py`, tests.

### Task 3: Verification and rollout

- [x] Document source support/limits and test command; add suite to existing platform CI and SESSION-01 selectors.
- [ ] Run specialist and related regressions, architecture/import/package gates, Ruff, UTF-8/BOM and diff checks.
- [ ] Read all 31 actual local databases in a temporary isolated capture and verify expected visible rows without native changes or Agent calls.
- [ ] Rebase/publish through sanitized clone; wait for PR/main CI and deploy. Upgrade signed affected node clients using established state-preserving updater.
- [ ] Verify original local Agy identities and detailed events through production operator HTTP; update canonical ops evidence.

## Fresh verification before publication

- Baseline new behavior tests: 2 failed on unmodified main; missing Agy default source and no HTTP-imported session.
- Final specialist suite: 43 passed; earlier specialist plus spool recovery: 72 passed. Wider native/ACP/capture/HTTP/architecture/import/journey/release checks: 368 passed, 2 subtests (overlapping suites, not added).
- Actual local read-only SQLite -> encrypted spool -> temporary Hub/HTTP: 31 sessions, 98 user-message blocks, 178 visible assistant blocks, 2233 tool calls and 2233 results, 1067 full-visible-source chunks; 203 local HTTP batches. Native database hashes unchanged. No real Agent/OAuth/model invocation. This proof does not yet prove production import.
- Review fixed an additional reachable recovery gap in the new importer: pending append is resolved by both archive/replay under spool ownership before ACK/compaction. No existing spool format change was required. Real quota-gap/replay, concurrent scanners and A→B→A snapshots have regression coverage.
- Names from the native summary store are retained; placeholder model enum IDs are intentionally not converted into guessed model names.
