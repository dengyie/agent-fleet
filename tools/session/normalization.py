"""Pure native CLI row -> bounded normalized events.

Vendor JSON shapes are isolated from capture source selection, redaction,
encrypted spooling, upload and user-echo lifecycle (owned by SessionBridge).
No system or reasoning records are projected as conversation content.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

def _native_message_text(content: Any) -> str:
    """Flatten one native transcript ``message.content`` to bounded text.

    String content passes through bounded; a block list keeps only the
    ``text`` blocks (thinking blocks and tool payloads are separate concerns
    mapped below, never flattened into a message body).
    """
    if isinstance(content, str):
        return content[:_MAX_TEXT]
    if isinstance(content, list):
        parts = []
        for block in content:
            if (isinstance(block, Mapping) and block.get("type") == "text"
                    and isinstance(block.get("text"), str) and block["text"]):
                parts.append(block["text"])
        return "\n".join(parts)[:_MAX_TEXT]
    return ""


_MAX_TEXT = 262144
_MAX_TOOL_FIELD = 262144


def normalize_native_row(raw: Mapping) -> list[tuple[str, Mapping]]:
    """Map a native transcript JSONL row to a list of ``(kind, payload_source)``.

    claude_code transcripts carry lowercase ``user``/``assistant`` rows whose
    ``message.content`` is a string (user prompt) or a block list
    (``text`` / ``thinking`` / ``tool_use`` / ``tool_result``).  ``thinking``
    blocks are dropped (internal reasoning, not an observable surface); the
    message payload keeps only text blocks.  Tool blocks map onto the bounded
    ``tool_call``/``tool_result`` payload allowlist — the nested JSON of a
    tool's input/result IS preserved (bounded) so real tool activity is
    captured, matching the hook adapter's behavior.  Unknown row types
    (attachment, file-history-snapshot, queue-operation, ...) return ``[]``.

    A row with several mapped blocks yields one event per block in order
    (a text-only claude row stays a single message event; claude rarely
    interleaves text and tool_use in one row, but when it does every block
    is now captured rather than only the first tool block).
    """
    rtype = str(raw.get("type") or "")
    metadata = _native_metadata(raw)
    if rtype == 'model_io':
        return _prepend_metadata(metadata, _shape_zcode_row(raw))
    if rtype in ("item.started", "item.updated", "item.completed"):
        item = raw.get("item")
        if not isinstance(item, Mapping):
            return []
        itype = item.get("type")
        completed = rtype == "item.completed"
        if itype == "agent_message":
            text = item.get("text", "")
            shaped = [("assistant_message", {
                "text": text, "is_complete": completed,
                "message_id": item.get("id"), "content_mode": "snapshot",
            })] if isinstance(text, str) and (text or completed) else []
            return _prepend_metadata(metadata, shaped)
        if itype in ("command_execution", "mcp_tool_call", "file_change", "web_search"):
            shaped = [("tool_result" if completed else "tool_call", {
                "tool_name": str(item.get("tool") or itype),
                "call_id": str(item.get("id") or ""),
                "arguments": _bounded_json(item.get("command", item.get("arguments", item.get("changes", item.get("query"))))),
                "result": _bounded_json(item.get("aggregated_output", item.get("result", item.get("error")))),
                "status": str(item.get("status") or ("completed" if completed else "running")),
            })]
            return _prepend_metadata(metadata, shaped)
        return _prepend_metadata(metadata, [])
    if rtype == "stream_event":
        return _prepend_metadata(metadata, _shape_claude_stream(raw))
    if rtype in ("event_msg", "response_item"):
        # codex rollout rows (type/payload wrapper) — see _shape_codex_row.
        return _prepend_metadata(metadata, _shape_codex_row(raw))
    if rtype in ("message", "message_end"):
        # pi transcript rows (role/message envelope) — see _shape_pi_row.
        return _prepend_metadata(metadata, _shape_pi_row(raw))
    if rtype in ("session_meta", "session", "model_change", "custom-title", "summary", "system"):
        return _prepend_metadata(metadata, [])
    # Pi also emits tool_execution_end before message_end(toolResult).
    # Project only the complete transcript message, once.
    if rtype not in ("user", "assistant"):
        return []
    message = raw.get("message")
    if not isinstance(message, Mapping):
        return []
    content = message.get("content")
    kind = "user_message" if rtype == "user" else "assistant_message"
    shaped: list[tuple[str, Mapping]] = []
    # tool_use / tool_result blocks live in the content block list; a plain
    # string or text-only list is a message body.
    if isinstance(content, list):
        pending_text: list[str] = []
        for block in content:
            if not isinstance(block, Mapping):
                continue
            if block.get("type") == "tool_use":
                if pending_text:
                    shaped.append((kind, {"text": "\n".join(pending_text)
                                          [:_MAX_TEXT], "is_complete": True,
                                          "message_id": _message_id(raw, message),
                                          "content_mode": "snapshot"}))
                    pending_text = []
                shaped.append(("tool_call", {
                    "tool_name": str(block.get("name") or "")[:128],
                    "call_id": str(block.get("id") or "")[:128],
                    "arguments": _bounded_json(block.get("input")),
                    "result": "",
                    "digest": "",
                }))
            elif block.get("type") == "tool_result":
                if pending_text:
                    shaped.append((kind, {"text": "\n".join(pending_text)
                                          [:_MAX_TEXT], "is_complete": True,
                                          "message_id": _message_id(raw, message),
                                          "content_mode": "snapshot"}))
                    pending_text = []
                shaped.append(("tool_result", {
                    "tool_name": "",
                    "call_id": str(block.get("tool_use_id") or "")[:128],
                    "arguments": "",
                    # Keep bounded TEXT only: a non-text block list (e.g.
                    # image payloads) would otherwise dump base64 blobs into
                    # the durable record — map it to "" instead (same
                    # discipline as the pi toolResult mapper).
                    "result": _native_message_text(block.get("content")),
                    "digest": "",
                }))
            elif (block.get("type") == "text"
                    and isinstance(block.get("text"), str) and block["text"]):
                pending_text.append(block["text"])
        if pending_text:
            shaped.append((kind, {"text": "\n".join(pending_text)[:_MAX_TEXT],
                                  "is_complete": True,
                                  "message_id": _message_id(raw, message),
                                  "content_mode": "snapshot"}))
        return _prepend_metadata(metadata, shaped)
    text = _native_message_text(content)
    if not text:
        return _prepend_metadata(metadata, [])
    return _prepend_metadata(metadata, [(kind, {
        "text": text, "is_complete": True,
        "message_id": _message_id(raw, message), "content_mode": "snapshot",
    })])


def _prepend_metadata(metadata, events):
    return ([('session_metadata', metadata)] if metadata else []) + events


def _native_metadata(raw: Mapping) -> dict[str, str]:
    """Extract only display-safe native title and model facts."""
    rtype = str(raw.get('type') or '')
    payload = raw.get('payload') if isinstance(raw.get('payload'), Mapping) else {}
    message = raw.get('message') if isinstance(raw.get('message'), Mapping) else {}
    request = raw.get('request') if isinstance(raw.get('request'), Mapping) else {}
    response = raw.get('response') if isinstance(raw.get('response'), Mapping) else {}
    stream = raw.get('event') if isinstance(raw.get('event'), Mapping) else {}
    stream_message = stream.get('message') if isinstance(stream.get('message'), Mapping) else {}
    result = {}
    title = None
    if rtype == 'custom-title':
        title = raw.get('customTitle')
    elif rtype in ('session_meta', 'session'):
        title = (raw.get('title') or payload.get('title')
                 or raw.get('name') or payload.get('name'))
    if isinstance(title, str) and title.strip():
        result['title'] = title.strip()[:160]

    model = (message.get('model') or stream_message.get('model')
             or payload.get('model') or response.get('model')
             or request.get('model') or raw.get('model') or raw.get('modelId'))
    provider = (message.get('provider') or stream_message.get('provider')
                or payload.get('model_provider') or response.get('provider')
                or request.get('provider') or payload.get('provider')
                or raw.get('provider'))
    if rtype == 'model_change':
        model = raw.get('modelId') or model
    if isinstance(model, str) and model.strip():
        result['model'] = model.strip()[:160]
    if isinstance(provider, str) and provider.strip():
        result['provider'] = provider.strip()[:96]

    version = payload.get('cli_version') or raw.get('cli_version')
    if rtype == 'session':
        version = raw.get('version') or version
    if isinstance(version, str) and version.strip():
        result['client_version'] = version.strip()[:96]

    if rtype in ('session_meta', 'session'):
        timestamp = (payload.get('timestamp') or raw.get('timestamp')
                     or raw.get('created_at'))
        if isinstance(timestamp, str) and timestamp.strip():
            result['native_started_at'] = timestamp.strip()[:40]
    return result


def _message_id(raw: Mapping, message: Mapping | None = None):
    value = raw.get('uuid') or raw.get('id')
    if message is not None:
        value = message.get('id') or value
    return value if isinstance(value, str) and value else None


def _shape_claude_stream(raw: Mapping):
    event = raw.get('event')
    if not isinstance(event, Mapping):
        return []
    etype = event.get('type')
    if etype == 'content_block_delta':
        delta = event.get('delta')
        if (isinstance(delta, Mapping) and delta.get('type') == 'text_delta'
                and isinstance(delta.get('text'), str) and delta['text']):
            return [('assistant_message', {
                'text': delta['text'], 'is_complete': False,
                'content_mode': 'delta',
            })]
    return []


def _shape_zcode_row(raw: Mapping) -> list[tuple[str, Mapping]]:
    """Project one native ZCode request/response without replaying old context.

    The complete, redacted source remains available as source_record. Headers,
    system prompts and provider metadata are not conversation preview events.
    """
    request = raw.get('request')
    response = raw.get('response')
    messages = request.get('messages', []) if isinstance(request, Mapping) else []
    messages = messages if isinstance(messages, list) else []
    # ZCode records full/tail/delta context. Only inputs after the last prior
    # assistant message belong to this invocation; its response is below.
    start = next((i + 1 for i in range(len(messages) - 1, -1, -1)
                  if isinstance(messages[i], Mapping) and messages[i].get('role') == 'assistant'), 0)
    shaped = []
    for message in messages[start:]:
        if not isinstance(message, Mapping):
            continue
        text = _native_message_text(message.get('content'))
        if message.get('role') == 'user' and text:
            shaped.append(('user_message', {'text': text, 'is_complete': True}))
        elif message.get('role') == 'tool':
            shaped.append(('tool_result', {
                'call_id': str(message.get('toolCallId') or ''),
                'tool_name': str(message.get('toolName') or ''),
                'result': text, 'status': 'error' if message.get('isError') else 'ok',
            }))
    if isinstance(response, Mapping):
        text = _native_message_text(response.get('text'))
        if text:
            shaped.append(('assistant_message', {'text': text, 'is_complete': True,
                'message_id': response.get('id'), 'content_mode': 'snapshot'}))
        calls = response.get('toolCalls')
        for call in calls if isinstance(calls, list) else []:
            if isinstance(call, Mapping):
                shaped.append(('tool_call', {'call_id': str(call.get('id') or ''),
                    'tool_name': str(call.get('name') or ''),
                    'arguments': _bounded_json(call.get('input'))}))
    return shaped


def _shape_pi_row(raw: Mapping) -> list[tuple[str, Mapping]]:
    """Map a pi transcript JSONL row to a list of ``(kind, payload_source)``.

    Pi wraps each turn as ``{"type": "message", "message": {...}}`` where the
    inner role is ``user`` / ``assistant`` / ``toolResult`` — tool results are
    their own role (with top-level ``toolName``/``toolCallId``/``isError``)
    rather than content blocks, and a call is a ``toolCall`` block whose
    ``arguments`` is already a dict.  Non-``message`` envelopes (``session``,
    ``model_change``, ``compaction``, ...) return ``[]``; ``thinking`` blocks
    are internal surfaces and never reach a message body.

    One row maps onto MULTIPLE events when the real transcript interleaves
    them inside a single message: real pi sessions frequently emit
    ``text`` + ``toolCall`` in one assistant row and several ``toolCall``
    blocks per row, so every toolCall block maps to its own ``tool_call``
    and any surrounding text maps to its own message event (order
    preserved; text is emitted before the first toolCall that follows it).
    An assistant row with no text and no toolCall yields ``[]`` (observed
    for tool-only turns).
    """
    message = raw.get("message")
    if not isinstance(message, Mapping):
        return []
    role = str(message.get("role") or "")
    content = message.get("content")
    if role == "toolResult":
        text = _native_message_text(content)
        body = {
            "tool_name": str(message.get("toolName") or "")[:128],
            "call_id": str(message.get("toolCallId") or "")[:128],
            "status": "error" if message.get("isError") else "ok",
            "arguments": "",
            # Keep bounded TEXT for the common case; a non-text block list
            # (e.g. image payloads) would otherwise dump base64 blobs into
            # the durable record — map it to "" instead.
            "result": text if text else "",
            "digest": "",
        }
        return [("tool_result", body)]
    if role not in ("user", "assistant"):
        return []
    shaped: list[tuple[str, Mapping]] = []
    message_kind = "user_message" if role == "user" else "assistant_message"
    if isinstance(content, list):
        pending_text: list[str] = []
        for block in content:
            if not (isinstance(block, Mapping)):
                continue
            if (block.get("type") == "text" and isinstance(block.get("text"), str)
                    and block["text"]):
                pending_text.append(block["text"])
                continue
            if block.get("type") == "toolCall":
                if pending_text:
                    text = "\n".join(pending_text)[:_MAX_TEXT]
                    shaped.append((message_kind,
                                   {"text": text, "is_complete": True,
                                    "message_id": message.get("id") or raw.get("uuid"),
                                    "content_mode": "snapshot"}))
                    pending_text = []
                shaped.append(("tool_call", {
                    "tool_name": str(block.get("name") or "")[:128],
                    "call_id": str(block.get("id") or "")[:128],
                    "arguments": _bounded_json(block.get("arguments")),
                    "result": "",
                    "digest": "",
                }))
        if pending_text:
            shaped.append((message_kind,
                           {"text": "\n".join(pending_text)[:_MAX_TEXT],
                            "is_complete": True,
                            "message_id": message.get("id") or raw.get("uuid"),
                            "content_mode": "snapshot"}))
    else:
        text = _native_message_text(content)
        if text:
            shaped.append((message_kind, {"text": text, "is_complete": True,
                "message_id": message.get("id") or raw.get("uuid"),
                "content_mode": "snapshot"}))
    return shaped


def _bounded_json(value: Any) -> str:
    """Bounded JSON serialization for nested tool input/result values."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:_MAX_TOOL_FIELD]
    try:
        return json.dumps(value, ensure_ascii=False)[:_MAX_TOOL_FIELD]
    except (TypeError, ValueError):
        return str(value)[:_MAX_TOOL_FIELD]


def _shape_codex_row(raw: Mapping) -> list[tuple[str, Mapping]]:
    """Map a codex rollout JSONL row to a list of ``(kind, payload_source)``.

    Codex wraps everything as ``{"type": "event_msg"|"response_item", ...,
    "payload": {...}}``.  Only observable surfaces map: user/assistant
    messages, tool calls/outputs.  ``reasoning`` and ``token_count`` are
    internal surfaces (never emitted); ``response_item/message`` is dropped —
    the parallel ``event_msg`` stream already carries the same content and
    emitting both would double-count every message.  Each codex payload maps
    to at most one event, returned as a single-element list.
    """
    rtype = str(raw.get("type") or "")
    payload = raw.get("payload")
    if not isinstance(payload, Mapping):
        return []
    ptype = str(payload.get("type") or "")
    if rtype == "event_msg":
        if ptype in ("item_started", "item_updated", "item_completed"):
            item = payload.get("item")
            complete = ptype == "item_completed"
            if isinstance(item, Mapping) and item.get("type") in ("UserMessage", "AgentMessage"):
                blocks = item.get("content") or []
                text = "\n".join(block["text"] for block in blocks
                                 if isinstance(block, Mapping)
                                 and block.get("type") in ("text", "Text")
                                 and isinstance(block.get("text"), str))
                if text or complete:
                    return [("user_message" if item["type"] == "UserMessage" else "assistant_message",
                        {"text": text, "is_complete": complete,
                         "message_id": item.get("id"), "content_mode": "snapshot"})]
            return []
        if ptype == "user_message":
            text = payload.get("message")
            if isinstance(text, str) and text:
                return [("user_message", {"text": text[:_MAX_TEXT],
                    "is_complete": True, "message_id": payload.get("item_id") or payload.get("message_id"),
                    "content_mode": "snapshot"})]
            return []
        if ptype == "agent_message":
            text = payload.get("message")
            if isinstance(text, str) and text:
                return [("assistant_message", {"text": text[:_MAX_TEXT],
                    "is_complete": True, "message_id": payload.get("item_id") or payload.get("message_id"),
                    "content_mode": "snapshot"})]
            return []
        if ptype == "agent_message_delta":
            text = payload.get("delta") or payload.get("message")
            if isinstance(text, str) and text:
                return [("assistant_message", {"text": text[:_MAX_TEXT],
                    "is_complete": False,
                    "message_id": payload.get("item_id") or payload.get("message_id"),
                    "content_mode": "delta"})]
            return []
        if ptype == "mcp_tool_call_end":
            invocation = payload.get("invocation")
            result = payload.get("result")
            ok = isinstance(result, Mapping) and "Ok" in result
            return [("tool_result", {
                "tool_name": str((invocation or {}).get("tool")
                                 if isinstance(invocation, Mapping)
                                 else "")[:128],
                "call_id": str(payload.get("call_id") or "")[:128],
                "status": "ok" if ok else "error",
                "arguments": _bounded_json(
                    (invocation or {}).get("arguments")
                    if isinstance(invocation, Mapping) else None),
                "result": _bounded_json(
                    result.get("Ok") if isinstance(result, Mapping) else None),
                "digest": "",
            })]
        return []
    if rtype == "response_item":
        if ptype == "function_call":
            return [("tool_call", {
                "tool_name": str(payload.get("name") or "")[:128],
                "call_id": str(payload.get("call_id") or "")[:128],
                "arguments": _bounded_json(payload.get("arguments")),
                "result": "",
                "digest": "",
            })]
        if ptype == "custom_tool_call":
            return [("tool_call", {
                "tool_name": str(payload.get("name") or "")[:128],
                "call_id": str(payload.get("call_id") or "")[:128],
                "arguments": _bounded_json(payload.get("input")),
                "result": "",
                "digest": "",
            })]
        if ptype == "function_call_output":
            return [("tool_result", {
                "tool_name": "",
                "call_id": str(payload.get("call_id") or "")[:128],
                "arguments": "",
                "result": _bounded_json(payload.get("output")),
                "digest": "",
            })]
        if ptype == "custom_tool_call_output":
            return [("tool_result", {
                "tool_name": "",
                "call_id": str(payload.get("call_id") or "")[:128],
                "arguments": "",
                "result": _bounded_json(payload.get("output")),
                "digest": "",
            })]
        # message rows are duplicated by the event_msg stream; reasoning is
        # internal — both dropped here.
        return []
    return []
