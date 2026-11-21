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
PYTHONPATH=. python3 -m pytest tests/test_native_session_api.py \
  tests/test_agent_lifecycle.py \
  tests/test_session_repository.py tests/test_transcript_repository.py \
  tests/test_transcript_cursors.py tests/test_session_query_api.py \
  tests/test_session_interactions.py tests/test_adoption_control_http.py \
  tests/test_adoption_control_guard.py tests/test_supervisor_control.py -q
```

These tests exercise Hub authorization and local SQLite behavior. They do not
run Agent CLIs, contact external models, or prove a production deployment.
