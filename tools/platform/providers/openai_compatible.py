"""Bounded OpenAI-compatible Chat Completions provider.

This module owns the network boundary for the platform worker.  It has no
global client, no implicit endpoint, and no secret fallback: callers must
explicitly select the ``openai_compatible`` provider and supply an endpoint
and a ``SecretBroker`` reference.  Tests inject a transport, while production
uses the small stdlib urllib transport below.
"""
from __future__ import annotations

import codecs
import json
from http.client import HTTPException
import math
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Iterator, Mapping, Protocol
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request

from tools.platform.http_transport import DeadlineResponse, open_http

from .base import ModelResponse
from .compatible import DeterministicProvider
from .observation import RequestObservation
from tools.platform.request_metadata import (
    RequestMetadata, RequestObserver, normalized_tokens, request_cost, token_count,
)


MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_TEXT_CHARS = 128 * 1024
MAX_SSE_EVENTS = 4096
TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
SAFE_HEADER_NAMES = frozenset({"user-agent", "accept-language", "x-client-name", "openai-organization"})


UPSTREAM_ERROR_CODES = frozenset({'system_cpu_overloaded', 'system_memory_overloaded', 'do_request_failed'})

PROVIDER_ERROR_CODES = frozenset({
    'auth_error', 'rate_limit', 'transient_http', 'request_rejected',
    'provider_http_error', 'timeout', 'network_error', 'invalid_response',
    'response_too_large', 'provider_unavailable', 'provider_network_disabled',
    'provider_secret_missing', 'provider_secret_unavailable',
    'provider_tools_invalid', 'redirect_rejected',
})


class ProviderError(RuntimeError):
    """Stable, secret-free provider failure."""

    def __init__(self, code: str, *, retryable: bool = False, status: int | None = None):
        self.code = code
        self.retryable = bool(retryable)
        self.status = status
        self.upstream_code: str | None = None
        super().__init__(code)

    def diagnostic(self) -> RequestMetadata:
        result: RequestMetadata = {'provider_error': self.code if self.code in PROVIDER_ERROR_CODES else 'provider_error',
                  'retryable': self.retryable}
        if type(self.status) is int and 100 <= self.status <= 599:
            result['provider_status'] = self.status
        if self.upstream_code in UPSTREAM_ERROR_CODES:
            result['upstream_code'] = self.upstream_code
        return result

    def __str__(self) -> str:
        return self.code


class ProviderUnavailable(ProviderError):
    def __init__(self, code: str = "provider_unavailable"):
        super().__init__(code)


class SecretBroker(Protocol):
    def resolve(self, secret_ref: str) -> str | None: ...


class EnvironmentSecretBroker:
    """Resolve only explicit ``env://NAME`` references.

    The raw value is returned to the adapter and is never included in a
    profile snapshot, exception, event, or log.  Other reference schemes fail
    closed until a deployment supplies a dedicated broker.
    """

    def resolve(self, secret_ref: str) -> str | None:
        if not isinstance(secret_ref, str) or not secret_ref.startswith("env://"):
            return None
        name = secret_ref[6:]
        if not name or len(name) > 128 or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for ch in name):
            return None
        value = os.environ.get(name)
        return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class TransportResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes | Iterable[bytes]


class ProviderTransport(Protocol):
    def request(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_s: float,
        stream: bool,
    ) -> TransportResponse: ...


class _ResponseBody:
    def __init__(self, response: DeadlineResponse) -> None:
        self.response = response
        self.total = 0

    def __iter__(self) -> Iterator[bytes]:
        return self

    def __next__(self) -> bytes:
        try:
            chunk = self.response.read1(8192)
        except (TimeoutError, OSError, URLError, HTTPException) as exc:
            raise _transport_error(exc) from exc
        if not chunk:
            raise StopIteration
        self.total += len(chunk)
        if self.total > MAX_RESPONSE_BYTES:
            raise ProviderError('response_too_large')
        return chunk

    def close(self) -> None:
        self.response.close()


def _transport_error(exc: Exception, *, status: int | None = None) -> ProviderError:
    reason = exc.reason if isinstance(exc, URLError) else exc
    # A POST may already have been accepted, even without response headers.
    return ProviderError('timeout' if isinstance(reason, TimeoutError) else 'network_error', status=status)


class UrllibTransport:
    """No redirect/replay; one deadline covers dispatch through response close."""

    def request(self, *, url: str, headers: Mapping[str, str], body: bytes,
                timeout_s: float, stream: bool) -> TransportResponse:
        request = Request(url, data=body, headers=dict(headers), method="POST")
        try:
            response = open_http(request, timeout_s=timeout_s)
        except (TimeoutError, OSError, URLError, HTTPException) as exc:
            raise _transport_error(exc) from exc
        headers_out = {str(k): str(v) for k, v in response.headers.items()}
        if 300 <= response.status < 400:
            response.close()
            return TransportResponse(response.status, headers_out, b'')
        if response.status >= 400:
            try:
                # Only allowlisted error codes are consumed, never raw messages.
                payload = response.read(4097)
                return TransportResponse(response.status, headers_out, payload)
            except (TimeoutError, OSError, URLError, HTTPException) as exc:
                raise _transport_error(exc, status=response.status) from exc
            finally:
                response.close()
        return TransportResponse(response.status, headers_out, _ResponseBody(response))


def _bounded_timeout(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 60.0
    if not math.isfinite(number):
        return 60.0
    return max(1.0, min(600.0, number))


def _bounded_retries(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(5, number))


def _endpoint(settings: Mapping[str, Any]) -> str:
    raw = settings.get("endpoint") or settings.get("base_url")
    if not isinstance(raw, str) or not raw or len(raw) > 2048:
        raise ProviderUnavailable("provider_endpoint_missing")
    parsed = urlsplit(raw)
    if (parsed.scheme not in ("http", "https") or not parsed.netloc
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ProviderUnavailable("provider_endpoint_invalid")
    endpoint = raw.rstrip("/")
    path = parsed.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        endpoint += "/chat/completions" if path.endswith("/v1") else "/v1/chat/completions"
    return endpoint


def _safe_headers(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProviderUnavailable("provider_headers_invalid")
    result: dict[str, str] = {}
    forbidden = ("authorization", "api-key", "token", "secret", "password", "credential")
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str) or len(key) > 80 or len(item) > 512:
            raise ProviderUnavailable("provider_headers_invalid")
        lowered = key.lower()
        if any(marker in lowered for marker in forbidden) or lowered not in SAFE_HEADER_NAMES:
            raise ProviderUnavailable("provider_headers_invalid")
        result[key] = item
    if len(result) > 32:
        raise ProviderUnavailable("provider_headers_invalid")
    return result


def _usage(payload: Mapping[str, Any]) -> dict[str, int] | None:
    raw = payload.get("usage")
    if not isinstance(raw, Mapping):
        return None
    result = {}
    for source, target in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"), ("total_tokens", "total_tokens")):
        number = token_count(raw.get(source))
        if number is not None:
            result[target] = number
    return result or None


def _metadata(payload: Mapping[str, Any], pricing: object = None) -> RequestMetadata:
    raw = payload.get('usage')
    raw = raw if isinstance(raw, Mapping) else {}
    prompt = raw.get('prompt_tokens_details')
    output = raw.get('completion_tokens_details')
    prompt = prompt if isinstance(prompt, Mapping) else {}
    output = output if isinstance(output, Mapping) else {}
    cache_write = prompt.get('cache_write_tokens', prompt.get('cached_creation_tokens', raw.get('cache_creation_input_tokens')))
    if cache_write is None:
        five = token_count(raw.get('claude_cache_creation_5_m_tokens'))
        hour = token_count(raw.get('claude_cache_creation_1_h_tokens'))
        if five is not None and hour is not None:
            cache_write = five + hour
    usage = normalized_tokens({**(_usage(payload) or {}),
        'cache_read_tokens': prompt.get('cached_tokens', raw.get('prompt_cache_hit_tokens')),
        'cache_write_tokens': cache_write,
        'reasoning_tokens': output.get('reasoning_tokens')})
    data: RequestMetadata = {'usage': usage, 'cost': request_cost(usage, reported=raw.get('cost'), pricing=pricing)}
    reason = None
    if (token_count(raw.get('claude_cache_creation_1_h_tokens')) or 0) > 0:
        reason = 'unsupported_cache_tier'
    if any((token_count(details.get(key)) or 0) > 0 for details in (prompt, output) for key in ('audio_tokens', 'image_tokens')):
        reason = 'unsupported_modality'
    if reason and (data['cost'] is None or data['cost']['source'] != 'provider'):
        data['cost'] = None
        data['cost_unavailable_reason'] = reason
    for key, maximum in (('model', 160), ('id', 256)):
        value = payload.get(key)
        if isinstance(value, str) and 0 < len(value) <= maximum and not any(ord(c) < 32 for c in value):
            data['response_id' if key == 'id' else key] = value
            if key == 'model':
                data['response_model'] = value
    return data


def _argument_value(value: Any) -> Any:
    # Decimal decoding protects monetary metadata. The existing structured
    # tool-argument variant still supplies ordinary JSON numbers to tools.
    if isinstance(value, Decimal):
        number = float(value)
        if not math.isfinite(number):
            raise ProviderError('invalid_response')
        return number
    if isinstance(value, Mapping):
        return {key: _argument_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_argument_value(item) for item in value]
    return value


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return {key: _argument_value(value) for key, value in raw.items()}
    if not isinstance(raw, str) or len(raw) > 128 * 1024:
        raise ProviderError("invalid_response")
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError) as exc:
        raise ProviderError("invalid_response") from exc
    if not isinstance(parsed, dict):
        raise ProviderError("invalid_response")
    return parsed


def _tool_response(message: Mapping[str, Any], usage=None, *,
                   tool_names: Mapping[str, str]) -> ModelResponse | None:
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        return None
    first = calls[0]
    if not isinstance(first, Mapping):
        raise ProviderError("invalid_response")
    function = first.get("function")
    if not isinstance(function, Mapping) or not isinstance(function.get("name"), str) or not function["name"]:
        raise ProviderError("invalid_response")
    return ModelResponse(
        kind="tool_call",
        text=str(message.get("content") or "")[:MAX_TEXT_CHARS],
        tool=_internal_tool_name(function["name"], tool_names),
        arguments=_arguments(function.get("arguments", "{}")),
        usage=usage,
        tool_call_id=str(first.get("id"))[:256] if first.get("id") else None,
    )


def _tool_name_map(tools: list[dict[str, Any]]) -> dict[str, str]:
    """Keep broker names internal; the wire protocol forbids their dots.

    Reject ambiguous aliases instead of dispatching a model call to the wrong
    tool. The map belongs to this request, so provider reuse shares no state.
    """
    names = {}
    used = set()
    for tool in tools:
        if not isinstance(tool, Mapping) or not isinstance(tool.get("name"), str):
            raise ProviderUnavailable("provider_tools_invalid")
        name = tool["name"]
        wire_name = name.replace(".", "_")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", wire_name) or wire_name in used:
            raise ProviderUnavailable("provider_tools_invalid")
        names[name] = wire_name
        used.add(wire_name)
    return names


def _internal_tool_name(wire_name: str, tool_names: Mapping[str, str]) -> str:
    for name, alias in tool_names.items():
        if alias == wire_name:
            return name
    raise ProviderError("invalid_response")


def _messages(messages: list[dict[str, Any]], tool_names: Mapping[str, str]) -> list[dict[str, Any]]:
    result = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role") or "user")[:32]
        content = message.get("content", "")
        if role == "tool":
            value = message.get("result", content)
            try:
                content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            except (TypeError, ValueError):
                content = str(value)[:MAX_TEXT_CHARS]
        if not isinstance(content, str):
            content = str(content)
        item = {"role": role, "content": content[:MAX_TEXT_CHARS]}
        if role == "tool" and isinstance(message.get("tool_call_id"), str):
            item["tool_call_id"] = message["tool_call_id"][:256]
        calls = message.get("tool_calls")
        if role == "assistant" and isinstance(calls, list):
            safe_calls = []
            for call in calls[:16]:
                if not isinstance(call, Mapping) or not isinstance(call.get("function"), Mapping):
                    continue
                function = call["function"]
                if not isinstance(function.get("name"), str):
                    continue
                if function["name"] not in tool_names:
                    raise ProviderUnavailable("provider_tools_invalid")
                safe_calls.append({
                    "id": str(call.get("id") or "call")[:256],
                    "type": "function",
                    "function": {
                        "name": tool_names[function["name"]],
                        "arguments": str(function.get("arguments") or "{}")[:128 * 1024],
                    },
                })
            if safe_calls:
                item["tool_calls"] = safe_calls
        result.append(item)
    return result


def _tools(tools: list[dict[str, Any]], tool_names: Mapping[str, str]) -> list[dict[str, Any]]:
    result = []
    for item in tools:
        result.append({
            "type": "function",
            "function": {
                "name": tool_names[item["name"]],
                "description": str(item.get("description") or "")[:1024],
                "parameters": item.get("parameters") if isinstance(item.get("parameters"), Mapping) else {"type": "object"},
            },
        })
    return result


def _json_payload(body: bytes) -> dict[str, Any]:
    if len(body) > MAX_RESPONSE_BYTES:
        raise ProviderError("response_too_large")
    try:
        payload = json.loads(body.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, TypeError, ValueError, InvalidOperation) as exc:
        raise ProviderError("invalid_response") from exc
    if not isinstance(payload, dict):
        raise ProviderError("invalid_response")
    return payload


def _sse_payloads(body: bytes | Iterable[bytes]) -> Iterator[dict[str, Any] | None]:
    """Decode bounded SSE frames, including arbitrary UTF-8/TCP boundaries."""
    chunks = [bytes(body)] if isinstance(body, (bytes, bytearray)) else body
    decoder = codecs.getincrementaldecoder('utf-8')()
    buffer = ''
    total_bytes = events = 0

    def decode_event(event: str) -> dict[str, Any] | None:
        data = '\n'.join(line[5:].lstrip() for line in event.splitlines() if line.startswith('data:'))
        if data == '[DONE]':
            return None
        return _json_payload(data.encode('utf-8')) if data else {}

    try:
        for chunk in chunks:
            if not isinstance(chunk, (bytes, bytearray)):
                raise ProviderError('invalid_response')
            total_bytes += len(chunk)
            if total_bytes > MAX_RESPONSE_BYTES:
                raise ProviderError('response_too_large')
            buffer += decoder.decode(bytes(chunk))
            while True:
                separator = re.search(r'\r?\n\r?\n', buffer)
                if separator is None:
                    break
                event, buffer = buffer[:separator.start()], buffer[separator.end():]
                events += 1
                if events > MAX_SSE_EVENTS:
                    raise ProviderError('response_too_large')
                payload = decode_event(event)
                yield payload
                if payload is None:
                    return
        buffer += decoder.decode(b'', final=True)
        if buffer.strip():
            if events >= MAX_SSE_EVENTS:
                raise ProviderError('response_too_large')
            yield decode_event(buffer)
    except UnicodeDecodeError as exc:
        raise ProviderError('invalid_response') from exc


def _close_body(body):
    close = getattr(body, 'close', None)
    if callable(close):
        close()


def _finish_reason(value):
    return value if value in ('stop', 'length', 'tool_calls', 'content_filter', 'function_call') else None


class OpenAICompatibleProvider:
    def __init__(self, *, model: str, endpoint: str, api_key: str, transport: ProviderTransport | None = None,
                 timeout_s: float = 60.0, max_retries: int = 0, headers: Mapping[str, str] | None = None,
                 stream: bool = False, sleeper=time.sleep, allow_network: bool = False, pricing=None):
        if not isinstance(model, str) or not model or len(model) > 160:
            raise ProviderUnavailable("provider_model_missing")
        if not isinstance(api_key, str) or not api_key:
            raise ProviderUnavailable("provider_secret_missing")
        self.pricing = pricing
        self.model = model
        self.endpoint = endpoint
        self.api_key = api_key
        self.transport = transport or UrllibTransport()
        self.timeout_s = _bounded_timeout(timeout_s)
        self.max_retries = _bounded_retries(max_retries)
        self.headers = dict(headers or {})
        self.stream = bool(stream)
        self.sleeper = sleeper
        # Injected transports are test/local boundaries and are always allowed;
        # the real urllib transport requires an explicit rollout gate.
        self.allow_network = bool(allow_network or transport is not None)

    @classmethod
    def from_profile(cls, profile: Mapping[str, Any], *, secret_broker: SecretBroker,
                     transport: ProviderTransport | None = None, sleeper=time.sleep,
                     allow_network: bool = False):
        settings = profile.get("provider_config") or profile.get("settings") or {}
        if not isinstance(settings, Mapping):
            raise ProviderUnavailable("provider_config_invalid")
        secret_ref = profile.get("secret_ref")
        if not isinstance(secret_ref, str) or not secret_ref:
            raise ProviderUnavailable("provider_secret_missing")
        try:
            secret = secret_broker.resolve(secret_ref)
        except Exception as exc:
            raise ProviderUnavailable("provider_secret_unavailable") from exc
        if not isinstance(secret, str) or not secret:
            raise ProviderUnavailable("provider_secret_unavailable")
        endpoint = _endpoint(settings)
        return cls(
            model=str(profile.get("model") or ""), endpoint=endpoint, api_key=secret,
            transport=transport, timeout_s=settings.get("timeout_s", 60.0),
            max_retries=settings.get("max_retries", 0), headers=_safe_headers(settings.get("headers")),
            stream=bool(settings.get("stream", False)), sleeper=sleeper,
            allow_network=allow_network, pricing=settings.get("pricing"),
        )

    def _request(self, body: bytes) -> TransportResponse:
        headers = {
            "Accept": "text/event-stream" if self.stream else "application/json",
            "Content-Type": "application/json", "User-Agent": "agent-fleet/1.0",
            "Authorization": f"Bearer {self.api_key}", **self.headers,
        }
        try:
            response = self.transport.request(url=self.endpoint, headers=headers, body=body,
                timeout_s=self.timeout_s, stream=self.stream)
        except ProviderError:
            raise
        except TimeoutError as exc:
            raise ProviderError('timeout', retryable=True) from exc
        except Exception as exc:
            raise ProviderError('network_error', retryable=True) from exc
        status = int(getattr(response, 'status', 0) or 0)
        if 200 <= status < 300:
            return response
        if 300 <= status < 400:
            error = ProviderError('redirect_rejected', status=status)
        elif status in (401, 403):
            error = ProviderError('auth_error', status=status)
        elif status == 429:
            error = ProviderError('rate_limit', retryable=True, status=status)
        elif status in TRANSIENT_STATUS:
            error = ProviderError('transient_http', retryable=True, status=status)
        elif 400 <= status < 500:
            error = ProviderError('request_rejected', status=status)
        else:
            error = ProviderError('provider_http_error', retryable=True, status=status)
        if isinstance(response.body, bytes) and len(response.body) <= 4096:
            try:
                body_error = json.loads(response.body).get('error', {})
                upstream = body_error.get('code') if isinstance(body_error, dict) else None
                if upstream in UPSTREAM_ERROR_CODES:
                    error.upstream_code = upstream
            except (ValueError, AttributeError, TypeError):
                pass
        _close_body(response.body)
        raise error

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *,
                 request_observer: RequestObserver | None = None) -> ModelResponse:
        tool_names = _tool_name_map(tools)
        if not self.allow_network and self.transport.__class__ is UrllibTransport:
            raise ProviderUnavailable('provider_network_disabled')
        payload = {"model": self.model, "messages": _messages(messages, tool_names),
                   "tools": _tools(tools, tool_names), "stream": self.stream}
        if self.stream:
            payload['stream_options'] = {'include_usage': True}
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        for attempt in range(self.max_retries + 1):
            observation = RequestObservation(request_observer, model=self.model,
                provider='openai_compatible', index=attempt + 1,
                parameters={'stream': self.stream, 'timeout_s': self.timeout_s})
            try:
                response = self._request(body)
            except ProviderError as exc:
                observation.finish(error=exc, http_status=exc.status)
                if exc.retryable and attempt < self.max_retries:
                    self.sleeper(min(2.0, 0.1 * (2 ** attempt)))
                    continue
                raise
            # Once a response body is being consumed, failure is ambiguous and
            # must never automatically replay an already accepted request.
            try:
                result = self._parse_response(response, tool_names)
            except ProviderError as exc:
                observation.finish(error=exc, http_status=response.status)
                raise
            except (TimeoutError, OSError) as exc:
                error = ProviderError('timeout' if isinstance(exc, TimeoutError) else 'network_error')
                observation.finish(error=error, http_status=response.status)
                raise error from exc
            finally:
                _close_body(response.body)
            observation.finish(metadata={**result.metadata, 'finish_reason': result.finish_reason},
                               http_status=response.status)
            return result
        raise ProviderError('provider_unavailable')

    def _parse_response(self, response, tool_names):
        if self.stream:
            return self._parse_sse(response.body, tool_names)
        raw = response.body
        if not isinstance(raw, (bytes, bytearray)):
            pieces, total = [], 0
            for chunk in raw:
                if not isinstance(chunk, (bytes, bytearray)):
                    raise ProviderError('invalid_response')
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise ProviderError('response_too_large')
                pieces.append(chunk)
            raw = b''.join(pieces)
        payload = _json_payload(bytes(raw))
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise ProviderError("invalid_response")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, Mapping):
            raise ProviderError("invalid_response")
        usage = _usage(payload)
        tool = _tool_response(message, usage, tool_names=tool_names)
        if tool is not None:
            return ModelResponse(**{**tool.__dict__, "finish_reason": _finish_reason(choice.get("finish_reason")), "metadata": _metadata(payload, self.pricing)})
        text = message.get("content")
        if not isinstance(text, str):
            raise ProviderError("invalid_response")
        return ModelResponse(kind="final", text=text[:MAX_TEXT_CHARS], usage=usage,
                             finish_reason=_finish_reason(choice.get("finish_reason")), metadata=_metadata(payload, self.pricing))

    def _parse_sse(self, body: bytes | Iterable[bytes], tool_names: Mapping[str, str]) -> ModelResponse:
        metadata_payload = {}
        text_parts = []
        tool_name, arguments = '', ''
        tool_id = finish_reason = None
        completed = False
        usage = {}
        for payload in _sse_payloads(body):
            if payload is None:
                completed = True
                break
            for key in ('model', 'id'):
                if payload.get(key) is not None:
                    metadata_payload[key] = payload[key]
            if isinstance(payload.get('usage'), Mapping):
                metadata_payload.setdefault('usage', {}).update(payload['usage'])
                usage.update(_usage(payload) or {})
            choices = payload.get('choices')
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0]
            if not isinstance(choice, Mapping):
                raise ProviderError('invalid_response')
            finish_reason = _finish_reason(choice.get('finish_reason')) or finish_reason
            delta = choice.get('delta')
            if not isinstance(delta, Mapping):
                continue
            if isinstance(delta.get('content'), str):
                text_parts.append(delta['content'])
            calls = delta.get('tool_calls')
            if isinstance(calls, list) and calls:
                call = calls[0]
                if not isinstance(call, Mapping):
                    raise ProviderError('invalid_response')
                tool_id = tool_id or call.get('id')
                function = call.get('function')
                if isinstance(function, Mapping):
                    if isinstance(function.get('name'), str):
                        tool_name += function['name']
                    if isinstance(function.get('arguments'), str):
                        arguments += function['arguments']
        if not completed and not finish_reason:
            raise ProviderError('invalid_response')
        metadata = _metadata(metadata_payload, self.pricing)
        if tool_name:
            return ModelResponse(kind='tool_call', text=''.join(text_parts)[:MAX_TEXT_CHARS],
                tool=_internal_tool_name(tool_name, tool_names), arguments=_arguments(arguments or '{}'),
                usage=usage or None, tool_call_id=str(tool_id)[:256] if tool_id else None,
                finish_reason=finish_reason, metadata=metadata)
        if not text_parts:
            raise ProviderError('invalid_response')
        return ModelResponse(kind='final', text=''.join(text_parts)[:MAX_TEXT_CHARS],
            usage=usage or None, finish_reason=finish_reason, metadata=metadata)


class ProviderFactory:
    """Create only explicitly supported providers."""

    def __init__(self, *, secret_broker: SecretBroker | None = None,
                 transport: ProviderTransport | None = None, sleeper=time.sleep,
                 allow_network: bool = False):
        self.secret_broker = secret_broker or EnvironmentSecretBroker()
        self.transport = transport
        self.sleeper = sleeper
        self.allow_network = bool(allow_network)

    def __call__(self, profile: Mapping[str, Any] | None):
        if not profile:
            raise ProviderUnavailable()
        provider = str(profile.get("provider") or "").lower()
        if provider == "deterministic":
            return DeterministicProvider()
        if provider in {"openai_compatible", "openai-compatible"}:
            return OpenAICompatibleProvider.from_profile(
                profile, secret_broker=self.secret_broker,
                transport=self.transport, sleeper=self.sleeper,
                allow_network=self.allow_network)
        raise ProviderUnavailable()


__all__ = [
    "EnvironmentSecretBroker", "OpenAICompatibleProvider", "ProviderError",
    "ProviderFactory", "ProviderTransport", "ProviderUnavailable",
    "SecretBroker", "TransportResponse", "UrllibTransport",
]
