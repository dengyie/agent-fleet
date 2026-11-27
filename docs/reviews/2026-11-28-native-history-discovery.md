# Native history discovery review

Scope: all six registered executable Agent families, from default and explicit
native roots through vendor projection, metadata invalidation, durable capture
and HTTP upload. Backend/node only; no frontend product or release implementation
changes. Authority and limits are in the
[runtime contract](../native-provider-runtime.md#native-history-discovery-and-hermes).

## Confirmed issues and repairs

| Priority | Reachable failure | Evidence | Repair |
| --- | --- | --- | --- |
| P1 | Windows Agy histories are absent despite native SQLite files | Installed Agy 1.5.18 has three stores in `.gemini/antigravity/conversations`; registry covered only `antigravity-cli`; failing default-worker HTTP test | Discover both exact native namespaces, reuse same schema reader, summary lookup and receipts |
| P1 | Hermes existing history has no independent source | Local state.db contains 217 sessions/116125 message rows; profile has no JSONL archive; failing SQLite -> worker -> HTTP test | Indexed finite session catalog, bounded exact-session projection, private compaction identity ledger and durable receipts |
| P1 | Custom data homes supported by managed continuation are ignored by archive discovery | Six fail-first root variants across Codex, Claude, Pi, ZCode and Agy | Honor existing named local environment roots; explicit values replace defaults |
| P1 | A Codex archive move loses discoverability and would get a second path identity | Actual archived directory contains dated rollouts; failing move/import continuity test | Discover archived_sessions and canonicalize the native dated filename to its original active path/identity/checkpoint |
| P2 | Two first native captures race creating the shared encryption key | Concurrent real captures fail O_EXCL before spool ownership | Reuse the existing cross-platform owned lock around key initialization/read; never replace a valid key |
| P1 | ACKed active tails are sealed only after compaction, permanently occupying the shared quota | Live Mac cache at 128 MiB; 499 fully acknowledged sealed tails total 14 MiB; fail-first close/reopen test | Compact after sealing under owner lock, only after durable checkpoint; retain uncertain ACK and unacknowledged segments |
| P1 | Stable event IDs retried under a new source sequence permanently strand contiguous ACK | Live original at seq1237 retried at1490; seq1490 missing while later frames persisted; four fail-first repository/HTTP tests | Atomically persist verified duplicate delivery aliases; reject changed body/stream or occupied sequence; preserve public ingestion cursor |

| P1 | A recovery index can survive failed initialization; empty sessions vanish and other sessions lose family | Six fail-first family variants plus empty Agy reproduce missing public session | One durable native identity observation after backlog flush; atomic version marker, stable retry identity, no lifecycle/prompt replay |

## Reviewed family matrix

| Family | Discovery / reader | Cursor / publication | Result / boundary |
| --- | --- | --- | --- |
| Codex | Exact active/archived JSONL roots; native display metadata and bound state_5 | Existing byte checkpoint and encrypted spool; archived moves reuse identity | Existing normalization captures visible messages/tools; arbitrary rewrites of old JSONL are not a mirrored-view protocol |
| Claude Code | projects JSONL, including nested child transcripts; explicit config home | Per-source byte checkpoint; current native title/model/version metadata | Visible text/tool blocks retained; thinking/image binary content excluded |
| Pi | sessions JSONL; explicit agent data directory | Stable message blocks and byte checkpoint; native session_info/model changes | Native storage version is not reported as client version; custom per-invocation session-dir is managed-binding scope |
| ZCode | rollout JSONL plus official bound desktop message listing and exact SQLite tools | Desktop stream has one owner; running tool rows block the native cursor until completion | Desktop source is not duplicated by archive; metadata DB/WAL invalidation already exists |
| Agy | Both observed flat SQLite/protobuf directories under selected Gemini home | Existing per-step active snapshots, durable append receipt, spool writer ownership | Windows actual stores parse; unknown versions/fields fail closed; private account profiles not automatically scanned |
| Hermes | Selected state.db, indexed immutable session ID and message ID pages | Native compaction keys stored privately; first eligible row gives public event IDs; receipt recovery precedes replay ACK | Visible compacted history retained; rewind-only/summary/display carriers excluded; post-import undo is outside append archive semantics |

## Architecture and robustness

Vendor parsing remains on the node. HTTP/Hub receives the existing public event
DTO and uses its ingestion cursor. Shared digest/tool field shaping and uncertain
append recovery are small focused modules; Agy and Hermes own their source schema
and checkpoint validation. Hermes' private ledger reconciles native generations;
it does not implement a second message queue. The Hub migration adds only verified
delivery receipts for old stable events retried at a different sequence.

Sensitive system/reasoning/auth/native control fields are excluded before
spooling. Full visible tool content is secret-redacted before chunking; previews
remain bounded and normal chat excludes source chunks. Read-only vendor queries
are finite, SQLite failures preserve native data, and uncertain writes/HTTP replies
retain recovery position. Original Agy v1 checkpoints and encrypted spool format
remain compatible.

## Verification record

Fail-first artifacts, actual platform read projections and fresh test results
are retained privately under the `native-history-review-20261128` verification
directory. No conversation bodies, database copies or credentials are committed.
Fresh local related suite: 464 passed / 6 subtests. Real Windows: 192 passed with no skips or errors. All 217 local Hermes sessions passed bounded first-page projection; the largest history traversed 794 pages through temporary encrypted spool and HTTP, with native hashes unchanged. Windows actual Agy stores yielded six user and five assistant records and preserved hashes. The new suites are selected by Windows/macOS CI and SESSION-01; runnable commands
are in [testing](../testing/README.md). CI/deployment evidence is recorded after
the exact candidate completes the existing release gates; local success alone is
not production acceptance.

Explicit limits: no real Agent/model/OAuth call is part of this repair; Windows
has three existing Agy histories but no Hermes state.db. Historical token timing
cannot be reconstructed from final rows. Native custom display carriers, later
edits/undo of imported rows, arbitrary relocated Codex files and unknown vendor
schemas require separate evidence before additional projection support.
