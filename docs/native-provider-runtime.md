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
