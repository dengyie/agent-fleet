# Native provider runtime development contract

Status: implementation on `codex/native-provider-runtime`, 2026-11-24; full release
gates and publication are pending. This contract is the implementation successor
to [the T3 Code source comparison](research/t3code-architecture-reference.md).
Implementation progress and executed evidence live in
[the execution plan](superpowers/plans/2026-11-23-native-provider-runtime.md).

VM execution, incremental stdio, bounded dispatch and process-tree completion
follow the [vm2api source comparison](research/vm2api-architecture-reference.md)
and its [reliability plan](superpowers/plans/2026-11-27-vm-native-stream-reliability.md).

## Product outcome and topology

Machine-local Google personal accounts, signed remote login/logout, credential
isolation and immutable task binding are defined in the
[provider-account contract](native-provider-accounts.md). This is an additive
account dimension over the existing ACP runtime, with no automatic rotation.

The public workspace must expose an accurate native conversation, observable
provider capabilities, native questions/permissions and the results of each
managed continuation. The browser communicates only with authenticated Hub HTTP.
Nodes own executables, working directories, provider authentication and protocol
connections. Existing operator authorization, signed commands, node journal,
encrypted capture spool and Hub ingestion cursors remain authoritative.

Antigravity uses `agy acp`, JSON-RPC 2.0 over owned stdio, following T3 Code's
Antigravity ACP implementation at commit `1bc44f2732ce0a24c0608626d59409f1a0893db4`.
The headless print protocol is retired for new Agy execution. An ACP failure must
not silently create a print conversation. Native identity is retained privately;
every later connection resumes that exact identity or fails. It never selects
the newest conversation or creates a replacement when resume fails.

Fleet retains its existing managed execution ownership: an owned ACP connection
remains open throughout a turn, including pending native requests and background
tool settlement. A later supervised execution reconnects with `session/resume`.
This differs from T3's in-memory host holding an idle connection across turns;
it preserves the same native conversation across separate runner/control-client
processes without introducing a second host RPC server. A process restart cannot
replay an ambiguous prompt automatically. Only a new signed user operation may
send another prompt.

## Layers and interfaces

| Unit | Responsibility | Excluded responsibility |
|---|---|---|
| `tools/providers/acp_transport.py` | Bounded JSON-RPC framing, request IDs, separate stderr drain, cancellation and EOF | HTTP, provider identity policy, browser DTOs |
| `tools/providers/agy_runtime.py` | Initialize/authenticate, exact native resume, configuration, native updates, permissions, background settlement | Hub credentials, shell interpretation, automatic prompt retry |
| `tools/providers/agy_worker.py` | Supervised executable entry point; typed capture rows and bounded private control mailbox | Arbitrary stdin access to adopted processes |
| `tools/providers/acp_mailbox.py` | Node-private pending native request/answer exchange between owned worker and signed control client | Browser-supplied paths, node authorization replacement |
| `tools/adapters/agy.py` | Resolve trusted node command and launch the Python ACP worker | Hub policy or native database discovery |
| `fleet_core/agent_evidence.py` | Pure public evidence projection, unknown/unsupported/observed distinctions | Granting controls or installation probing |
| Existing session application/HTTP services | Operator authorization, durable question/message idempotency, signed action dispatch | Vendor RPC IDs, native session IDs, CLI arguments |
| Continuation result collection | Command/turn-bound Git changes and test summary through existing bounded collectors | Replacing the original task result or guessing external workspace ownership |

Dependencies remain `hub -> fleet_core <- tools`. No SDK, Effect.js runtime,
broker, provider database writer or general RPC compatibility layer is added.

## Antigravity ACP lifecycle

1. Resolve the configured local command. The default is `agy acp`. Reject legacy
   print/session selectors and shell launchers; instructions are JSON text blocks,
   never part of the vendor command. Preserve allowed provider environment keys
   without forwarding runner, node or Hub credentials.
2. Spawn under the existing owned process tree. Bound one frame to 1 MiB, bound
   buffered frames and pending requests, and drain stderr independently. Invalid
   UTF-8, malformed frames, ambiguous response IDs, truncated frames and EOF have
   fixed error codes. Do not publish raw stderr/auth URLs as assistant messages.
3. Send `initialize` with protocol version 1, truthful Fleet client identity and
   `fs.readTextFile=false`, `fs.writeTextFile=false`, `terminal=false`. Agent-local
   tools remain Agent-owned; unadvertised client-side file/terminal requests are
   refused. Do not copy T3's older-client identity impersonation.
4. Authenticate with `oauth-personal` by default, following T3's eager
   authentication. Authentication failure returns a bounded failure; never launch
   an interactive browser or install/log in as part of task execution.
5. New execution calls `session/new` with the exact owned cwd and empty MCP server
   list. Continuation requires advertised `sessionCapabilities.resume`, calls
   `session/resume` with the retained ID/cwd and never falls back to `session/new`.
   Session IDs are bounded opaque private values, not assumed to be UUIDs.
   The private `fleet_acp_session` row includes a strict boolean
   `resume_supported`, derived from the negotiated resume capability object.
   Retaining an ID alone does not grant resume. Legacy print identities remain
   retained with their workspace but cannot acquire ACP continuation authority.
6. Apply only explicitly requested and advertised model/mode configuration.
   Prefer `session/set_config_option` for advertised options; use native mode/model
   methods only where negotiated. Do not equate mode names with Fleet authority.
7. Send one `session/prompt` per user operation. Text chunks append to a stable
   invocation/message identity. A second text item after tool activity gets a new
   message identity. Tool patches merge by native tool ID within this invocation.
   Public event identity remains the Hub cursor. Equal text is not deduplication.
8. Route `session/request_permission` through a separate signed native-answer
   action, not an ordinary appended user message. Only a current request and one
   offered option may be selected. A request can expire, be cancelled or become
   unavailable; no auto-allow is inferred from a CLI or UI name.
9. `session/prompt` result and process exit are different milestones. Pending
   permissions and known background tools must settle before success. Emit a
   terminal lifecycle state only after successful settlement; protocol/provider
   failures emit failed, and cancellation emits stopped. Cancellation sends
   `session/cancel`, then uses bounded owned process cleanup.
   Lost connection after sending a prompt is uncertain failure, never retried.
10. Preserve bounded source events and normalized visible text/tool/metadata via
    the existing encrypted capture channel. Close the runtime and verify its
    owned descendants before successful managed completion. Private identity is
    saved atomically by the existing continuation store.

Visible text chunks are split into at most 8192 characters without dropping the
remainder, including multibyte Unicode. Tool arguments/results use an 8192-character
preview. An oversized field has an explicit truncation marker and its complete
visible value is secret-redacted first, then retained in 4000-character
`source_record` chunks through the existing encrypted archive. This does not
archive the surrounding ACP frame, hidden metadata or reasoning. Normal chat
reads continue to exclude source records; deliberate source archive reads use
the existing separately bounded API. Capture/spool capacity and retention limits
still apply and a capture gap remains visible rather than implying completeness.

## Native permission HTTP and delivery contract

The existing `POST /api/sessions/<id>/messages` question shape keeps
`request_id`, `client_token`, `selected`/`text`. Existing non-ACP question behavior
remains unchanged. ACP requests carry an additive, validated native-delivery
marker. Their answers are routed to a fixed signed native-answer action with
request identity and selected public option IDs. Native RPC IDs and option values
are mapped only inside the worker mailbox.

The Hub reserves the question and deduplicates the command with the existing
interaction transaction/reconciliation behavior. The node rechecks session
authority and writes to the exact live worker's private mailbox. No arbitrary
path, command, process ID or raw JSON-RPC payload is accepted from HTTP. A closed
mailbox, wrong incarnation, expired request, unknown option, reused request or
failed native delivery cannot report success. Ordinary messages keep their own
client-token store. No answer is converted into a sibling CLI conversation.

## Observable capability evidence

Capability evidence describes what was observed, with provenance and protocol;
it never expands `allowed_actions`. Preserve distinctions among adapter support,
binary installation, negotiated protocol, current identity/authority and actual
capture quality. The public matrix covers Codex, Claude Code, Pi, ZCode CLI,
ZCode desktop, Hermes and Agy. Missing source/model/usage/approval information is
unknown or unsupported, not fabricated from the family label.

For Codex retain the working CLI/native-rollout path; an app-server migration
requires its own provider lifecycle spec and acceptance. For Claude Code retain
CLI stream/native JSONL; SDK adoption requires a demonstrated control benefit
and separately managed SDK dependency. Pi retains exact-session JSON/RPC evidence;
ZCode/Hermes retain verified private store/workspace and Runner-only guards.
The shared interfaces must allow later protocol improvements without moving
vendor branches into HTTP handlers or frontend control authorization.

The public evidence enum/provenance contract, durable bounded projection, v6
session migration and source-specific upgrade criteria are detailed in
[Agent observation contract](agent-observability.md). Both detail and directory
HTTP return the same evidence object; retained observations survive restart and
do not expand current actions.

## Per-continuation results

Each managed continuation result is bound to its signed command ID and exact
session/task/workspace. Snapshot Git baseline before dispatch and collect bounded
changes after terminal completion using existing worktree/output collectors.
Record execution and collection status independently, including partial failure,
no changes, truncation and unavailable test summary. Never overwrite the startup
task result or attribute cumulative previous changes to the new turn. Unmanaged,
unverified, non-Git or unavailable workspaces return explicit unavailability.

On Windows the collector uses the pinned NTFS source reader. It accepts a directory
owned by the current user, SYSTEM or Administrators and treats OWNER RIGHTS ACE as
applying to that verified owner only. Source directories writable by other principals
are ineligible for collection. The result spool creates Fleet-owned directories with
protected, inheritable ACLs under the configured cache and rejects existing spool
directories with unsafe permissions. The Hub signed-command SQLite store continues
to enforce its POSIX ownership/mode invariant on POSIX hosts.

Publication uses the existing authenticated node/runner delivery pattern,
durable idempotent persistence and operator-scoped read DTOs. Native paths,
credentials, command argv and database details stay private. New SQLite schema
is migrated atomically and tested on old, partially incompatible and damaged
database versions. A lost upload can recover the same completed result without
executing the Agent again.

## Verification and release gates

Independent strict stdio peers must validate literal wire methods and payloads;
they must not import product serialization code. Tests cover handshake/auth,
resume refusal without sibling creation, permissions, wrong-session updates,
tool patches, result-before-background completion, cancellation, EOF, stalled
stdin, frame/queue bounds, Unicode and stderr saturation. Run the actual worker
under the real Supervisor and capture events through real Hub SQLite/HTTP.

For every new signed action test authorization/revocation, current incarnation,
concurrent duplicate delivery, journal replay, response loss, expiry and bounded
errors. Test per-turn results with real temporary Git repositories and real
SQLite transactions, including dirty baseline, failed turns, repeated upload,
isolation and migration. Keep existing normal-message/question tests passing.

Run architecture, standalone node import, independent frontend packaging and
Windows/macOS CI gates. Actual Agy/Claude/etc. client and model acceptance is a
separate evidence row per platform/version, never inferred from fixture or CI
success. Missing binaries remain unavailable. Follow the established rebase,
full journey CI, signed client packaging and production deployment procedure;
do not weaken release gates or change unrelated deployment logic.


## Native transport and process completion reliability

ACP framing reads available bytes in chunks of at most 64 KiB, preserving split
UTF-8 until the complete newline-terminated frame. The 1 MiB frame limit includes
the newline; CRLF is accepted and partial EOF fails closed. Strict duplicate JSON
keys, nonfinite values, wrong identities and unknown response checks remain.
Consumers process records synchronously, preserving order and bounded backpressure.
The one-second outgoing write deadline includes lock, queue and flush waits;
pre-dispatch cancellation never queues a request. A first transport failure is
retained and wakes pending requests. Explicit close wakes them with `protocol_eof`
if no earlier failure exists; process teardown still owns blocked pipe cleanup.

Successful protocol completion alone does not prove process-tree cleanup. After
stdout EOF or the bounded tail drain, Supervisor requires confirmed reap and
absence before dropping ownership. Unconfirmed cleanup returns exit 125 /
`escape_unverified`, retains its handle/owner and nonterminal manifest, and blocks
native follow-ups across restart until an explicit stop confirms cleanup. Confirmed
normal completion retains the vendor exit code; a tail timeout with confirmed
cleanup remains exit 124 / `group_remaining`. Existing HTTP/SQLite DTOs and schema
are unchanged.

On Windows, confirmed empty owned-Job evidence remains attached to that private
process incarnation after the Job handle is released. A later global process
snapshot or reused PID cannot overturn that proof. Unreadable jobs and failed
cleanup never acquire it; recovered Supervisors still require fresh ownership
and liveness checks because this proof is not persisted as process authority.

## Finite session upload and cooperative shutdown

Each upload pass captures the encrypted spool's current `last_sequence` under
the capture lock and uploads only that bounded prefix. Events appended during
HTTP requests remain durable for the next pass, so a continuous producer cannot
keep the same upload call draining indefinitely. Existing event-count/byte caps,
contiguous ACK validation and source/Hub event identities remain unchanged.

The live upload worker and retained-history replay check their existing stop
signal between requests and again after reading the next batch. An already
in-flight HTTP response is applied before returning: its confirmed prefix is
checkpointed; response loss leaves that prefix unacknowledged locally and safely
replays the original event IDs after restart. Stop never marks unsent events as
accepted, removes their spool, or interrupts native Agent execution.

Stopping the background worker still waits for its current transport call and
checkpoint; this is cooperative cancellation, not a hard deadline for an arbitrary
injected transport. After background stop, normal `finish()` explicitly performs
the final upload, including close events. Existing transport timeouts, quota and
closed-history retention remain authoritative. No HTTP, database or spool-format
migration is required. See the [implementation plan](superpowers/plans/2026-11-27-session-upload-pass-recovery.md)
and [test entry](testing/README.md).

## Existing local Antigravity CLI history

The node archive worker now discovers only `*.db` directly inside
`~/.gemini/antigravity-cli/conversations` and `~/.gemini/antigravity/conversations`
(both observed on installed native CLIs). These are the native CLI's per-session
SQLite/protobuf stores, not JSONL. The exact `cascade_id` must match the UUID
filename. The sibling `conversation_summaries.db` supplies the native title,
with the native preview used only when that title is empty. Agy's caches,
credentials, nested repositories and provider-account profiles are not recursively
scanned. A nondefault history directory can use the existing sync CLI's explicit
`--source agy=DIRECTORY` option. No Agent process is started to import history.

`tools/session/agy_storage.py` owns the vendor-specific read-only projection;
`agy_archive.py` owns publication through the existing RunnerCapture, encrypted
spool and runner-authenticated ingest API. The existing public directory, detail,
event tail/before/after and search APIs consume the same DTOs without frontend
or HTTP changes. Existing externally created sessions are imported as unmanaged,
best effort, read only. Import never grants ACP continuation/control authority
or guesses a Google account binding; Fleet-managed ACP tasks keep their existing
separate ownership and account protocol.

The supported observed schema has indexed `steps.idx`, protobuf `step_payload`,
separate `metadata` and `step_format=0`. Field numbers were verified against the
locally installed CLI's `gemini_coder.Step`, `cortex_pb` and `codeium_common_pb`
descriptors; no descriptor dump, user record or vendor binary is distributed.
The allowlist retains user input, visible planner responses, explicitly displayed
errors, and visible generic/read/search/list/command/edit tool evidence. It never
recursively serializes protobuf objects or uploads thinking, raw thinking,
signatures, system/context records, generator/executor state, credentials or
arbitrary `Any` fields. Model names are included only when explicitly stored as
names in the step's `ModelInfo`; enum placeholders are left unknown. Native step
creation times and the first step's start time are retained. Historical token
chunks and a missing client version cannot be reconstructed from completed rows.

Each SQLite read is an indexed page of at most 32 steps and 4 MiB of selected
blob data, with a 2 MiB limit per blob, a bounded wire-field count, a query deadline
and a lock timeout. Rows are fetched incrementally rather than materializing
32 oversized blobs. SQLite uses `mode=ro`, query-only mode and a read transaction.
Malformed data, unsupported schema/format/type, unsafe paths and unreadable or
locked stores defer that file with bounded diagnostics; they do not advance its
cursor or block other files. Corrupt/future/oversized Fleet checkpoints fail
closed. No Hub, vendor database or encrypted spool format migration is required.
The new private `.agy-cursor` version 1 is created atomically on first import.

Native `idx` is a private source position. Public stable event identity remains
the Hub ingestion cursor. Hashed private source/step identities bind delivery
receipts, message snapshots and tool calls; text equality and timestamps never
remove distinct steps. A source step updated in place stays ahead of the durable
cursor until terminal, while changed visible snapshots are published with the
same logical message ID. An observation revision distinguishes A→B→A snapshots
and title changes. Complete rows past a nonterminal step wait for that step to
settle, preserving order. Terminal rows are treated as immutable; retroactive
edits to previously acknowledged terminal rows require separate source support.

The private source checkpoint records one pending append intention and a bounded
list of confirmed event receipts for the current step, without message bodies,
paths, credentials or native IDs. Under the spool writer lock, both the archive
worker and existing replay worker resolve an uncertain append against its exact
encrypted sequence before any upload can compact it. A quota gap is never a
message receipt. This prevents crash recovery from enqueueing the same event at
a new sequence. The source cursor advances only after every step event is durable;
lost HTTP responses replay the original encrypted events/sequence. A second
scanner reloads the source checkpoint after acquiring writer ownership. Disk or
quota failures preserve the retry position; shutdown checks stop between source
steps and requests. An in-flight configured HTTP request may still finish.

Visible messages are split into stable 4000-codepoint snapshot blocks, preserving
all bounded selected text. Tools use an 8192-character preview and an explicit
truncation marker; full selected visible tool fields are secret-redacted before
splitting into stable 4000-codepoint `source_record` chunks. Existing explicit
source reads retrieve that archive; ordinary chat reads exclude it. Spool and Hub
retention/quota limits continue to apply. Agy database/WAL and summary database/WAL
fingerprints invalidate completed scans, so new messages and renames are found
without rescanning acknowledged rows. The worker's existing finite traversal
prioritizes this newly supported source and preserves progress across slices.

Regression commands and the real-local read-only proof are linked from
[testing](testing/README.md) and the
[implementation plan](superpowers/plans/2026-11-28-agy-native-history.md).

## Native history discovery and Hermes

The independent archive worker uses the same explicitly configured data homes as
managed native continuation: `CODEX_HOME/sessions` and `archived_sessions`,
`CLAUDE_CONFIG_DIR/projects`, `PI_CODING_AGENT_DIR/sessions`,
`ZCODE_DATA_BASE_DIR/.zcode/cli/rollout`, `GEMINI_HOME/antigravity[-cli]/conversations`,
and `HERMES_HOME/state.db`. Missing variables select the existing user defaults;
an explicit variable replaces that family's default. These are local process
environment settings, not browser-provided paths. Only known transcript layouts
are enumerated. No account profile or credentials directory is traversed.

Windows Agy 1.5.18 was observed using `%USERPROFILE%/.gemini/antigravity/conversations`;
Mac's installed CLI uses `antigravity-cli/conversations`. Both use the verified
SQLite/protobuf projection above; OS/version alone never selects a schema. Actual
Windows native databases were read without Agent/model calls and retained their
hashes. Renames use the sibling summary database in that same namespace.

Codex moves dated rollout files from `sessions/YYYY/MM/DD` into
`archived_sessions`. The archive identity maps this exact native filename back to
its original active path, retaining the existing Fleet session ID, encrypted
sequence and byte checkpoint. There is no second import identity for that move.
Undated/custom filenames retain their original path-based identity.

Hermes catalog enumeration uses indexed `sessions.id` keyset pages of 32. A
private virtual source identity is the exact `state.db/session_id`; it is never
opened as a directory or returned in public DTOs. Each session reads at most 32
ordered `messages.id` rows, 4 MiB of selected data per page, with a 2 MiB bound
per selected field. Read-only SQLite connections have a 0.5 second lock timeout
and two-second query progress deadline. Concurrent catalog inserts below the
current key are discovered on the next traversal. Database and WAL fingerprints
invalidate completed sessions; metadata refresh does not rewind content.

Hermes projection preserves explicit title/model/start time, user and assistant
text, tool calls and tool results. Its NUL-prefixed JSON content is decoded only
for explicit text blocks; images, system/reasoning fields and private payloads
are excluded. `active=1 OR compacted=1` matches native display eligibility;
rewind-only, compressed-summary and special display-carrier rows are excluded.
Unknown display kinds are deferred from content projection rather than flattened.
Old schemas without optional active/compacted/display flags remain readable.

Native compaction can copy a logical message into later row IDs. A node-private
`*.hermes-identities.sqlite` records a SHA-256 of the exact native display-generation
key (role, content, native timestamp, tool identity/arguments) and its first
observed eligible row ID/publication flag. This follows the native client's
display-generation rule; an independent identical turn with a different native
timestamp is retained. Content, credentials and native session IDs are absent
from the ledger. Public event IDs derive from the first native row and event
position; Hub ingestion cursors remain the stable public event identity.

Ledger creation atomically migrates an empty v0 SQLite file to schema v1 under
`BEGIN IMMEDIATE`; an interrupted transaction can retry. Existing foreign v0,
corrupt and future versions are preserved and rejected. The ledger is capped at
32 MiB per native session. `.hermes-cursor` v1 uses atomic private JSON and stores
only source position, metadata hash/revision, bounded receipts and one pending
append intention. Both normal capture and replay resolve that append under the
existing spool writer lock before ACK/compaction. A durable ledger publication
flag precedes cursor advance, so a failed cursor write cannot duplicate events.
Lost HTTP replies resend original encrypted events. Stop checks preserve the
accepted prefix. Initial shared spool-key creation uses the existing cross-platform
owned lock, preventing two first scanners from racing on an incomplete key.

Imports remain unmanaged, best effort and read only. They do not grant control,
infer a Google account or start any CLI. Terminal database history cannot recover
unrecorded token timing. In-place edits/deletions of already imported Hermes rows,
post-import undo and custom display carriers are not a mirrored current-view
protocol; this is an append history of eligible observations. Missing indexes,
oversized fields, corrupt/unknown schemas or missing ledgers after progress fail
closed and retry without resetting the content cursor. See the
[review](reviews/2026-11-28-native-history-discovery.md) and
[tests](testing/README.md) for verified scope.

Fully acknowledged active spool tails are compacted after sealing at close while
writer ownership is held. A dirty/uncertain checkpoint prevents this compaction;
unacknowledged data is retained. This avoids many idle histories permanently
consuming the shared archive quota. Recovery keeps the original ACK and next
sequence even when no encrypted segments remain.

Duplicate native delivery recovery also covers retained frames from older clients
that retried a stable event ID under a new source sequence. The Hub atomically
adds an internal `delivery_receipts` row only after matching the stored redacted
event exactly, excluding source sequence and observation timestamp. Machine,
stream, session, kind, payload and authority must remain equal; changed data
returns bounded `event_id_conflict`, and an occupied sequence returns
`sequence_conflict`. No new public event/cursor or raw copy is allocated.
Contiguous ACK includes these durable receipts, allowing the old encrypted frame
to be acknowledged and compacted. A retry within another stream cannot use the
receipt. This is an ingestion delivery record, not a second browser API.

The transcripts SQLite store gains additive `meta.delivery_receipts_schema=1`
and a `delivery_receipts` table keyed by session/sequence/machine/stream, linked
to the original redacted event with cascading deletion. Empty old stores migrate
under one `BEGIN IMMEDIATE`; failures roll back table and marker together.
Unknown versions, foreign unversioned tables, a missing declared table and
lookalike schemas with wrong types, nullability or foreign-key cascade fail
closed. Existing event/raw/public cursor schema and operator authorization are
unchanged, and older application rollback ignores the additive table.

Native recovery indexes are not initialization receipts. After uploading retained
frames, `RunnerCapture.flush` publishes one versioned identity observation for
every native family, even for an empty/unnamed source. Only a durable append
allows the atomic `native_identity_version=1` marker; rejected writes remain
retryable. Its stable event ID recovers across a failed marker write through the
existing delivery receipt path. No lifecycle, prompt or control capability is
replayed. Flushing retained frames before repair lets ACKed data release quota.
