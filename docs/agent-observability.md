# Native Agent observation and protocol evidence

This is the development contract for the read-only `agent_evidence` projection.
The surrounding execution/authorization layers remain defined by
[Agent capability architecture](agent-capability-architecture.md), the public
DTO by [native session HTTP](native-session-api.md), and Agy ACP lifecycle by
[native provider runtime](native-provider-runtime.md). Platform/client acceptance
is tracked separately in [the acceptance matrix](testing/agent-platform-acceptance.md).
The design follows the fixed-source [T3 Code comparison](research/t3code-architecture-reference.md).

## What the browser can rely on

`GET /api/sessions` and `GET /api/sessions/<id>` include an additive
`agent_evidence` object. This object explains observations for that session,
not promises about every client version in that family. It is fixed and bounded:
no raw metadata, executable, native database path, token, RPC ID or authority
grant is present. Existing operator/account authentication protects both reads;
machine, Runner and ingest credentials cannot authorize them.

Three independent states are used for features:

| State | Meaning | Browser behavior |
|---|---|---|
| `observed` | Accepted persisted public events or explicit bounded metadata provide this fact. | May say “observed in this session”; does not enable a control. |
| `unknown` | The projection has no evidence yet. An old session or a source that omitted a field is common. | Display “not confirmed”; do not label the Agent incapable. |
| `unsupported` | Fleet's current public contract deliberately does not expose this field. | Explain the contract boundary; do not infer a vendor limitation. |

`adapter_support=supported` means Fleet recognizes that family. `installation`
is always `unknown` in this DTO: historical capture cannot prove a binary is
currently installed or authenticated on its VM. Existing node discovery and
platform acceptance records remain the installation evidence.

`protocol.name` is one of `acp`, `cli_json`, `native_transcript`,
`desktop_bridge`, `unknown`. It is `observed` only when a persisted
`runtime_protocol` metadata field or an exact native/desktop capture source
provides evidence. A generic `structured_stream` source does not distinguish
CLI JSON from ACP. The source is `session_metadata`, `capture_source`, or `none`.
Agy's owned worker emits ACP only after the runtime has negotiated that protocol.

`capture.source` is a fixed capture-source label; an unknown adapter string
becomes `unknown`. `capture.quality` uses the existing `exact`, `structured`,
`best_effort` policy and unmanaged sessions remain `best_effort`. The evidence
projection never upgrades quality or authorizes a source. `capture.history` is
`unknown`, `retained_source_from_checkpoint`, or `gaps_observed`. A persisted
capture-gap or a downward quality transition takes precedence over a retained
checkpoint. An equal-quality notification is not a gap. None of these states
claims that missing, encrypted, discarded or never-captured vendor history was
recovered.

## Event and metadata projection

| Public feature | Evidence | Provenance |
|---|---|---|
| `visible_text` | Nonempty user/assistant message text | `session_event` |
| `text_deltas` | Accepted message with `content_mode=delta` | `session_event` |
| `message_identity` | Accepted nonempty public `message_id` | `session_event` |
| `tool_calls`, `tool_results` | Corresponding accepted event kind | `session_event` |
| `interactive_questions` | Accepted `interaction_request` | `session_event` |
| `native_permissions` | Interaction request explicitly carries `delivery=native_permission` | `session_event` |
| `model`, `provider`, `native_title`, `client_version`, `native_started_at` | Explicit bounded metadata provided by the native source | `session_metadata` |
| `resume_identity` | Existing projected `native_resume` evidence | `continuation_identity` |
| `usage`, `hidden_reasoning` | Not in the current public session event contract | `public_contract`, `unsupported` |

Event facts are projected from the redacted rows already persisted by
TranscriptRepository, never the caller's duplicate payload. Reusing an event ID
with new text/delta/tool claims cannot change the original observation. Hub
ingestion cursor remains the event identity; native sequence and equal text do
not deduplicate updates. `message_id` only groups presentation updates.

The repository unions a fixed set of eight event fact names once per bounded
ingest batch. `BEGIN IMMEDIATE` protects concurrent unions from lost updates.
The fixed set is monotonic across restarts and transcript trimming: “observed”
means it occurred, not that every historical row remains retained. A newly
encountered gap also remains visible. Metadata continues to use per-field Hub
cursor fencing; replaying an older protocol update cannot replace newer data.

Retained-session recovery reads at most 256 KiB from the source prefix and 2 MiB
from its tail (4096 complete rows per region), or one exact row from the
read-only Codex v5/ZCode/Hermes SQLite store. Historical prefix facts use
`fill_missing:true`, which can fill a blank projection but cannot overwrite a
fact already stored from later ingestion. Current tail facts use normal Hub
cursor fencing. The recovery checkpoint contains only a version and
fingerprints/digests; malformed or future versions fail closed. It does not
scan full transcripts, issue an Agent/version command, alter vendor storage,
replay conversation messages or migrate the session database. Fields absent
from the bounded source remain unknown.

Ingestion and evidence projection use the existing durable transcript plus
recoverable application projection, not a new cross-store transaction. If the
transcript commits and the evidence write fails, the request returns bounded
`503 session_unavailable`. Replaying the exact upload re-reads the stored rows
and recovers observations without creating events again. No raw SQLite exception
or path enters the HTTP error.

Session SQLite schema v6 adds `runtime_protocol` (default `unknown`), its
`runtime_protocol_cursor`, and `observed_features` (default `[]`) in the existing
atomic migration. Old sessions remain unknown until normal accepted ingestion
or exact upload replay provides facts. There is no migration-time scan of vendor
or transcript history. Wrong declared types disable session management without
advancing the version; partial migration failures roll back. Invalid protocol
values, malformed/oversized fact JSON and unknown names are sanitized on read.
Future schema versions retain the existing bounded rejection behavior.

## Family-specific capture and upgrade boundaries

This table describes source adapters that exist in Fleet. Each row still needs
session evidence; it does not turn unobserved facts into `observed` or claim real
Windows/macOS acceptance.

| Agent/client | Current source and native identity | Observable content where provided | Next protocol change requires |
|---|---|---|---|
| Codex CLI/native rollout | CLI JSON and bounded native transcript; exact retained thread/cwd | Visible messages/snapshots/deltas, calls/results, explicit model/provider/header/title records | A separate app-server lifecycle spec, stable item/turn mapping, approval/cancel/reconnect and auth tests before replacing current CLI path. |
| Claude Code | CLI stream JSON or native JSONL; exact retained session/cwd | Visible text deltas/snapshots, block identity, tool calls/results, explicit model/title records, existing structured questions where enabled | Demonstrated SDK benefit, managed dependency/lifecycle, exact native resume and independent wire/control tests. SDK availability alone grants nothing. |
| Pi | Managed JSON output or native JSONL; exact retained session/path | Completed user/assistant snapshots, native block identity, tool-result normalization and explicit model/provider changes | A separately owned RPC session with literal method/identity/cancel/approval evidence before offering live RPC controls. Current collection does not promise token deltas. |
| ZCode CLI | Managed output plus bounded read-only native SQLite/model I/O; exact ID/cwd | Completed visible messages, tool records, explicit display metadata | A documented protocol or store-version change with bounded reads, wrong-cwd refusal and identity recovery; Runner-only controls remain guarded. |
| ZCode desktop | Its existing desktop bridge and native message pages | Completed message snapshots, explicit native title/model/provider and bridge events | Bridge protocol/version/authorization tests, retained source identity and revocation behavior; CLI evidence cannot authorize desktop operations. |
| Hermes | Managed output plus bounded read-only native SQLite; exact ID/cwd and task-private configuration | Visible messages, tool records and explicit source metadata | Independently proved live stream/control methods, private home/cwd isolation and identity recovery; missing Windows binary stays unavailable. |
| Google Antigravity (`agy`) | Owned `agy acp` stdio runtime; negotiated protocol and exact opaque native session resume | Visible ACP text chunks, invocation/message identity, merged tool patches/results, explicit session/model metadata, native permission requests | Existing ACP contract tests plus real per-platform client/auth/version acceptance. Never fall back to print or create a sibling session after resume failure. |

Internal reasoning/thinking blocks, system prompts, auth URLs and arbitrary vendor
fields remain excluded from conversation content. Visible text published by an
Agent is still captured, but Fleet does not manufacture private chain-of-thought
or expose vendor secrets to fill a capability label. Usage/cost projection is a
separate future public schema and attribution task.

The browser can replace the interaction surface for supported controls. The
execution VM still owns its Agent binary, authentication, protocol connection
and workspace. `allowed_actions` and live operator/node admission remain the
only control authority; an observed permission or retained identity can describe
history after authority has been revoked.

## Verification and extension checklist

`tests/test_agent_evidence.py` exercises real Flask/SQLite restart/replay,
operator auth, immutable duplicate payloads, metadata fencing, concurrent unions,
question/native-permission separation, gap transitions, old/corrupt migration,
failure recovery and the actual JavaScript DTO parser. It does not run external
models, install vendor binaries or prove every native store version.

For a new evidence field, first specify its bounded public state/provenance,
then add the appropriate native source fact, schema validation, persisted
projection and browser parser in the current layers. Add missing-source,
redaction, replay, concurrent persistence and revoked-authority tests. Do not
infer the field from a family name or add vendor branches to HTTP/JavaScript.
Run the commands at [the testing entry point](testing/README.md), plus installed
browser/platform gates. Record real client evidence separately, with native
version and OS. Updating this contract also requires updating the public API,
architecture links and testing entry point.
