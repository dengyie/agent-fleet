# Native Agent session HTTP contract

This document defines the operator-facing HTTP contract for native Agent
sessions. The browser uses the public Hub API only; it does not invoke a local
CLI, read node files, or call main-assistant management RPCs. Authentication
uses the existing operator or administrator account identity. Runner, machine,
and ingest credentials do not authorize these routes.

Session metadata and event fields remain the existing public DTOs. Responses do
not expose node paths, credentials, signed command payloads, or native database
details. Errors use the shared bounded shape with a stable `error` code and
request ID.

## Ordinary message delivery

`POST /api/sessions/<session_id>/messages` accepts an ordinary user turn:

```json
{
  "text": "Continue reviewing the failing tests.",
  "client_token": "browser-operation-7e83"
}
```

`client_token` is optional for compatibility with existing `{ "text" }`
callers. When supplied, it must be a non-empty string of at most 128
characters, with no surrounding whitespace or ASCII control characters. A
present `null`, empty, or invalid token returns 400 `invalid_client_token`.
Text must be 1–2000 characters after the existing control-payload validation;
invalid or oversized text returns 400 `invalid_message`.

The Hub binds the token to the authenticated operator, session, operation
(`append_user_turn`), and canonical validated payload. It stores a token digest
and content digest with the existing Supervisor command in the same SQLite
`BEGIN IMMEDIATE` transaction. The raw token and message body are not copied to
the idempotency record. An exact retry returns the original command ID and its
current status; it never enqueues a second command. This is also the recovery
path after a lost HTTP response, so no separate token-lookup endpoint is
needed. A client recovering after refresh must retain and replay the original
text and token.

Successful admission returns HTTP 202:

```json
{
  "session_id": "sess_abc123",
  "command_id": "cmd_0123456789abcdef0123456789abcdef",
  "status": "queued"
}
```

`status` reflects the current outbox or receipt state. A 202 confirms Hub
admission only; it does not confirm Agent application or completion. The
returned `command_id` can be used with the existing operator command-receipt
API, and later transcript events remain the evidence of Agent output.

The token is globally unique within this message-operation store. Reusing it
with a different operator, session, operation, or canonical text returns 409
`idempotency_conflict`. The response contains no original command ID, body,
token, or receipt. A failed, rejected, or expired command is still the original
operation: replay returns that same terminal status. A new intentional send
requires a new token.

First-time admission rechecks current session and adoption authorization.
Revocation prevents a new message from being queued and returns 409
`adoption_revoked`. An exact retry by the same operator may recover a command
accepted before revocation; that path reads its receipt and does not enqueue.
Unauthenticated callers receive the existing 401 response. Store failures
return a bounded 503; they never include SQLite paths or exception text.

Interactive question answers keep their existing, separate
`request_id`/`client_token` protocol with `selected` and/or `text`. They are not
stored as ordinary message operations and retain their current reservation and
reconciliation behavior.

## Session name, client metadata, and conversation content

`GET /api/sessions` and `GET /api/sessions/<session_id>` keep the existing
`public_session_dto` shape and add observed display metadata when the native
source provides it:

```json
{
  "session": {
    "session_id": "sess_abc123",
    "machine_id": "my-mac",
    "agent_family": "codex",
    "adapter": "native_transcript",
    "client_version": "0.1.2",
    "native_title": "Review the API boundary",
    "model": "codex-model-x",
    "provider": "openai",
    "native_started_at": "2026-11-22T10:00:00Z",
    "started_at": "2026-11-22T10:00:02Z",
    "updated_at": "2026-11-22T10:35:00Z",
    "finished_at": null
  }
}
```

The title is taken only from an explicit native title/session-name record.
When none exists, `title_preview` is the first observed, redacted user message;
it is a visibly different fallback, not a fabricated native name. The directory
and chat heading prefer `native_title`, then `title_preview`, then a neutral
Agent/session identifier. Provider, model, adapter, version, and timestamps are
included only when explicitly observed. Unsupported fields are omitted.

`native_started_at` is populated only from an explicit native session header.
If a client does not provide one, `started_at` is the Hub's first observation
time and the UI labels it accordingly. Duration runs from the native start when
present, otherwise from that first Hub observation, until `finished_at` or the
current time. It is a displayed duration, not an Agent-reported runtime.

The event DTO remains the source for each user-visible message, tool call/result,
and lifecycle transition. Message payloads may include `message_id` and
`content_mode` (`delta` or `snapshot`): deltas append to the same native message,
while snapshots replace that message's current text. These are real source
updates persisted as separate Hub events and merged using Hub ingestion cursors;
vendor sequence numbers and equal text are never used for identity. Codex
rollouts provide snapshots and, where available, text deltas; Claude stream
records provide visible text deltas; Pi, ZCode model I/O, and ZCode desktop
message pages generally provide completed snapshots. ZCode desktop metadata
uses only explicit title/model/provider fields from its native session summary.
A source that has no streaming surface stays completed-message-only. User-visible
assistant text, tool evidence, interaction questions/answers, state changes,
and capture gaps are retained. Internal `thinking`/`reasoning`, system prompts,
and unrecognized source fields are not conversation content.

The event endpoint accepts `include_source=0` (or `false`) to exclude separate
`source_record` archive rows. Native chat always uses this option so raw source
records cannot leak into the browser's conversation response. Omitting the
parameter preserves the existing operator diagnostic behavior; use
`include_source=1` only when deliberately reading the separate source archive.
Duplicate or unrecognized values return 400 `invalid_include_source`. This does
not change the event cursor contract: hidden source-record rows may leave gaps
between returned Hub cursors, and clients continue from the last returned
cursor.

## Event history

`GET /api/sessions/<session_id>/events` supports three mutually exclusive
positions. All event cursors are decimal Hub ingestion cursors, returned as
strings. They are independent of vendor sequence, event text, emitted time,
and source stream identity. Each query reads at most `limit + 1` rows; `limit`
defaults to 100 and is clamped to 1–1000.

The existing forward read remains available and returns the first page after
the supplied cursor in ascending ingestion order:

```http
GET /api/sessions/sess_abc123/events?after_cursor=42&limit=100
```

Use the last event's `cursor` (also returned as `next_cursor`) for another
forward page. An empty page preserves the requested cursor.

For first load, request the latest page:

```http
GET /api/sessions/sess_abc123/events?tail=1&limit=100
```

Tail results are still returned oldest-to-newest. `has_more` means older
records exist, `before_cursor` is the first/oldest returned event, and
`next_cursor` is the last/newest returned event. Save `next_cursor` for live
forward updates. If the history is empty, `next_cursor` is `"0"`; an event
ingested immediately after the empty read is therefore still visible to the
next `after_cursor=0` request.

Load an older page with the first event's cursor:

```http
GET /api/sessions/sess_abc123/events?before_cursor=40&limit=100
```

`before_cursor` is exclusive. Results are in ascending ingestion order,
`has_more` indicates that still older records exist, and `before_cursor` in the
response is the oldest event in that page. Repeating with this response value
continues without overlap. `next_cursor` is the newest event in the returned
page; use the saved tail checkpoint for live updates rather than replacing it
with an older-page cursor.

`tail`, `before_cursor`, and `after_cursor` cannot be combined. The existing
`at_sequence` deep link resolves to a Hub cursor before reading; for compatibility
with clients that always send an after cursor, it may be paired with an omitted
`after_cursor` or `after_cursor=0`. A nonzero after cursor cannot be combined
with `at_sequence`. Invalid syntax, duplicate cursor parameters, values
above SQLite's signed 64-bit cursor range, or a cursor beyond the repository's
high-water mark return 400 `invalid_cursor`.

Event cursors do not expire. A cursor for a clipped or deleted event remains a
valid exclusive boundary, and an unused numeric position at or below the
high-water mark is also treated as a boundary. Reads return whatever retained
events are still available around it; clipping cannot restore deleted history.
The Hub ingestion cursor is the only event identity used for this pagination.

Example response:

```json
{
  "events": [
    {
      "event_id": "evt_abc",
      "session_id": "sess_abc123",
      "sequence": 8,
      "kind": "assistant_message",
      "emitted_at": "2026-11-22T10:30:00Z",
      "payload": {"text": "The tests pass.", "is_complete": true},
      "cursor": "43"
    }
  ],
  "after_cursor": "42",
  "before_cursor": "43",
  "next_cursor": "43",
  "has_more": false
}
```

The native chat client requests the same endpoint with `include_source=0`; its
message timeline still includes every retained, allowlisted user-visible text,
tool, and lifecycle event, without fetching the separate source archive.

## Session directory

`GET /api/sessions` returns the existing `public_session_dto` rows plus
`has_more` and `next_cursor`. The default page size is 100 and the public
maximum is 1000. Follow the cursor to read the remainder of the same snapshot:

```http
GET /api/sessions?machine=my-mac&limit=50
GET /api/sessions?machine=my-mac&limit=50&cursor=<next_cursor>
```

The first request freezes the maximum session-directory position. The opaque
cursor carries that snapshot and the exclusive keyset position and is bound to
the `machine` filter. A malformed cursor, a cursor from another machine
filter, or a position beyond the current directory high-water mark returns 400
`invalid_cursor`.

Directory order is assigned once per session. Existing rows receive a
deterministic migration order using their stored update/start time and
`session_id` as a tie-breaker; newly seen sessions receive a later position.
Session identity binds its machine, so updates to state or timestamps do not
reorder a traversal. Concurrent inserts after the snapshot appear when the
client restarts from the first page. Updates to listed rows use their current
public DTO values. The cursor has no time-based expiry. A cursor beyond the
current directory high-water mark is rejected; after replacing the database,
clients should start a new traversal because old numeric positions may
coincidentally be valid.

The legacy `active=1` view remains a live slice and does not accept a directory
cursor. Session pages do not change event pagination or expose repository
positions as DTO fields.

## Compatibility and verification

The existing PR #109 call `{ "text": "..." }`, its interaction-answer request,
and forward event polling continue to work. The client must send and retain a
normal-message `client_token` to use response-loss recovery; it must request
`tail`/`before_cursor` to use reverse event paging, and consume `next_cursor` to
traverse the directory. PR #110's task target and task creation endpoints are
separate and already available on `main`.

The HTTP and SQLite regression suite is:

```sh
PYTHONPATH=. python3 -m pytest tests/test_session_schema.py \
  tests/test_session_bridge.py tests/test_native_session_api.py \
  tests/test_session_query_api.py \
  tests/test_agent_lifecycle.py \
  tests/test_session_repository.py tests/test_transcript_repository.py \
  tests/test_transcript_cursors.py \
  tests/test_session_interactions.py tests/test_adoption_control_http.py \
  tests/test_adoption_control_guard.py tests/test_supervisor_control.py \
  tests/test_zcode_session_binding.py tests/test_native_continuation.py \
  tests/test_review_node_sync.py -q
```

The native display regressions cover explicit title/model/client extraction,
snapshot and delta assembly, hidden-reasoning exclusion, metadata projection,
v4-to-v5 migration rollback, and the browser's source-record exclusion. Real
Chrome coverage for the new metadata and source boundary is:

```sh
FLEET_PLAYWRIGHT_MODULE=/path/to/playwright \
FLEET_BROWSER_CHANNEL=chrome PYTHONPATH=. \
python3 -m pytest tests/test_native_chat_frontend.py \
  -k 'source-record or native-metadata or directory-metadata' -q
```

These tests exercise Hub authorization and local SQLite behavior. They do not
run Agent CLIs, contact external models, or prove a production deployment.

### 内容块与补充元数据

Codex `turn_context` 的实际 model/provider 与 Pi `session_info.name` 投影到既有
session_metadata；未知值保持缺失。Claude 每个原生文本 content block 和 Pi
每个文本块拥有独立 message_id，避免工具前后的文本被 snapshot 覆盖。Claude
stream delta、block stop 和最终完整快照使用同一块身份；Hub cursor 继续是事件
唯一身份，message_id 只负责消息展示更新。ZCode 桌面分页推进保留元数据 checkpoint，
相同标题/模型不会在每次扫描重复上报。

agy Runner 流使用既有 assistant_message/tool_call/tool_result DTO。每次原生
调用有独立的消息命名空间，同一次调用按 step index 合并增量。重复 init 会开启新
调用命名空间；不以文本或 vendor step index 替代 Hub ingestion cursor。源端未
提供的标题、版本、模型或历史不做推断。内部 reasoning/thinking 不作为聊天内容。
