"""Bounded OpenAI-compatible Chat Completions provider.

This module owns the network boundary for the platform worker.  It has no
global client, no implicit endpoint, and no secret fallback: callers must
explicitly select the ``openai_compatible`` provider and supply an endpoint
and a ``SecretBroker`` reference.  Tests inject a transport, while production
uses the small stdlib urllib transport below.
"""
from __future__ import annotations

import json
import math
import os
import socket
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .base import ModelResponse
from .compatible import DeterministicProvider


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
})


class ProviderError(RuntimeError):
    """Stable, secret-free provider failure."""

    def __init__(self, code: str, *, retryable: bool = False, status: int | None = None):
        self.code = code
        self.retryable = bool(retryable)
        self.status = status
        self.upstream_code = None
        super().__init__(code)

    def diagnostic(self):
        result = {'provider_error': self.code if self.code in PROVIDER_ERROR_CODES else 'provider_error',
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


class UrllibTransport:
    """Minimal production transport with bounded non-streaming reads."""

    def request(self, *, url, headers, body, timeout_s, stream):
        request = Request(url, data=body, headers=dict(headers), method="POST")
        try:
            response = urlopen(request, timeout=timeout_s)
        except HTTPError as exc:
            # HTTPError is also a response.  Read only a bounded body; the
            # provider never exposes it in the resulting error.
            try:
                payload = exc.read(MAX_RESPONSE_BYTES + 1)
            except Exception:
                payload = b""
            return TransportResponse(int(exc.code), dict(exc.headers or {}), payload)
        except (socket.timeout, TimeoutError):
            raise ProviderError("timeout", retryable=True) from None
        except (URLError, OSError):
            raise ProviderError("network_error", retryable=True) from None

        headers_out = {str(k): str(v) for k, v in response.headers.items()}
        if not stream:
            try:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
            finally:
                response.close()
            return TransportResponse(int(response.status), headers_out, payload)

        def chunks():
            total = 0
            try:
                while True:
                    chunk = response.read(8192)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        raise ProviderError("response_too_large")
                    yield chunk
            except (socket.timeout, TimeoutError):
                raise ProviderError("timeout", retryable=True) from None
            except (URLError, OSError):
                raise ProviderError("network_error", retryable=True) from None
            finally:
                response.close()

        return TransportResponse(int(response.status), headers_out, chunks())


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
    result: dict[str, int] = {}
    for source, target in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"), ("total_tokens", "total_tokens")):
        value = raw.get(source)
        if isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= number <= 10_000_000:
            result[target] = number
    if "input_tokens" in result or "output_tokens" in result:
        result.setdefault("input_tokens", 0)
        result.setdefault("output_tokens", 0)
    return result or None


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if not isinstance(raw, str) or len(raw) > 128 * 1024:
        raise ProviderError("invalid_response")
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        raise ProviderError("invalid_response") from None
    if not isinstance(parsed, dict):
        raise ProviderError("invalid_response")
    return parsed


def _tool_response(message: Mapping[str, Any], usage=None) -> ModelResponse | None:
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
        tool=function["name"][:256],
        arguments=_arguments(function.get("arguments", "{}")),
        usage=usage,
        tool_call_id=str(first.get("id"))[:256] if first.get("id") else None,
    )


def _messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
                safe_calls.append({
                    "id": str(call.get("id") or "call")[:256],
                    "type": "function",
                    "function": {
                        "name": function["name"][:256],
                        "arguments": str(function.get("arguments") or "{}")[:128 * 1024],
                    },
                })
            if safe_calls:
                item["tool_calls"] = safe_calls
        result.append(item)
    return result


def _tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in tools:
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
            continue
        result.append({
            "type": "function",
            "function": {
                "name": item["name"][:256],
                "description": str(item.get("description") or "")[:1024],
                "parameters": item.get("parameters") if isinstance(item.get("parameters"), Mapping) else {"type": "object"},
            },
        })
    return result


def _json_payload(body: bytes) -> dict[str, Any]:
    if len(body) > MAX_RESPONSE_BYTES:
        raise ProviderError("response_too_large")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, TypeError, ValueError):
        raise ProviderError("invalid_response") from None
    if not isinstance(payload, dict):
        raise ProviderError("invalid_response")
    return payload


class OpenAICompatibleProvider:
    def __init__(self, *, model: str, endpoint: str, api_key: str, transport: ProviderTransport | None = None,
                 timeout_s: float = 60.0, max_retries: int = 0, headers: Mapping[str, str] | None = None,
                 stream: bool = False, sleeper=time.sleep, allow_network: bool = False):
        if not isinstance(model, str) or not model or len(model) > 160:
            raise ProviderUnavailable("provider_model_missing")
        if not isinstance(api_key, str) or not api_key:
            raise ProviderUnavailable("provider_secret_missing")
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
        except Exception:
            raise ProviderUnavailable("provider_secret_unavailable") from None
        if not isinstance(secret, str) or not secret:
            raise ProviderUnavailable("provider_secret_unavailable")
        endpoint = _endpoint(settings)
        return cls(
            model=str(profile.get("model") or ""), endpoint=endpoint, api_key=secret,
            transport=transport, timeout_s=settings.get("timeout_s", 60.0),
            max_retries=settings.get("max_retries", 0), headers=_safe_headers(settings.get("headers")),
            stream=bool(settings.get("stream", False)), sleeper=sleeper,
            allow_network=allow_network,
        )

    def _request(self, body: bytes):
        if not self.allow_network and self.transport.__class__ is UrllibTransport:
            raise ProviderUnavailable("provider_network_disabled")
        headers = {
            "Accept": "text/event-stream" if self.stream else "application/json",
            "Content-Type": "application/json",
            "User-Agent": "agent-fleet/1.0",
            "Authorization": f"Bearer {self.api_key}",
            **self.headers,
        }
        for attempt in range(self.max_retries + 1):
            try:
                response = self.transport.request(
                    url=self.endpoint, headers=headers, body=body,
                    timeout_s=self.timeout_s, stream=self.stream,
                )
            except ProviderError as exc:
                if exc.retryable and attempt < self.max_retries:
                    self.sleeper(min(2.0, 0.1 * (2 ** attempt)))
                    continue
                raise
            except (TimeoutError,):
                error = ProviderError("timeout", retryable=True)
                if attempt < self.max_retries:
                    self.sleeper(min(2.0, 0.1 * (2 ** attempt)))
                    continue
                raise error from None
            except Exception:
                error = ProviderError("network_error", retryable=True)
                if attempt < self.max_retries:
                    self.sleeper(min(2.0, 0.1 * (2 ** attempt)))
                    continue
                raise error from None
            status = int(getattr(response, "status", 0) or 0)
            if 200 <= status < 300:
                return response
            if status in (401, 403):
                error = ProviderError("auth_error", status=status)
            elif status == 429:
                error = ProviderError("rate_limit", retryable=True, status=status)
            elif status in TRANSIENT_STATUS:
                error = ProviderError("transient_http", retryable=True, status=status)
            elif 400 <= status < 500:
                error = ProviderError("request_rejected", status=status)
            else:
                error = ProviderError("provider_http_error", retryable=True, status=status)
            if isinstance(response.body, bytes) and len(response.body) <= 4096:
                try:
                    body_error = json.loads(response.body).get('error', {})
                    upstream = body_error.get('code') if isinstance(body_error, dict) else None
                    if upstream in UPSTREAM_ERROR_CODES:
                        error.upstream_code = upstream
                except (ValueError, AttributeError, TypeError):
                    pass
            if error.retryable and attempt < self.max_retries:
                self.sleeper(min(2.0, 0.1 * (2 ** attempt)))
                continue
            raise error
        raise ProviderError("provider_unavailable")

    def complete(self, messages, tools) -> ModelResponse:
        payload = {
            "model": self.model,
            "messages": _messages(messages),
            "tools": _tools(tools),
            "stream": self.stream,
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response = self._request(body)
        if self.stream:
            return self._parse_sse(response.body)
        raw = response.body
        if not isinstance(raw, (bytes, bytearray)):
            try:
                raw = b"".join(raw)
            except Exception:
                raise ProviderError("invalid_response") from None
        payload = _json_payload(bytes(raw))
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise ProviderError("invalid_response")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, Mapping):
            raise ProviderError("invalid_response")
        usage = _usage(payload)
        tool = _tool_response(message, usage)
        if tool is not None:
            return ModelResponse(**{**tool.__dict__, "finish_reason": choice.get("finish_reason")})
        text = message.get("content")
        if not isinstance(text, str):
            raise ProviderError("invalid_response")
        return ModelResponse(kind="final", text=text[:MAX_TEXT_CHARS], usage=usage,
                             finish_reason=str(choice.get("finish_reason")) if choice.get("finish_reason") else None)

    def _parse_sse(self, body: bytes | Iterable[bytes]) -> ModelResponse:
        if isinstance(body, (bytes, bytearray)):
            chunks = [bytes(body)]
        else:
            chunks = body
        buffer = ""
        text_parts: list[str] = []
        tool_name = ""
        tool_id = None
        arguments = ""
        usage = None
        finish_reason = None
        events = 0
        try:
            for chunk in chunks:
                if not isinstance(chunk, (bytes, bytearray)):
                    raise ProviderError("invalid_response")
                buffer += bytes(chunk).decode("utf-8")
                if len(buffer.encode("utf-8")) > MAX_RESPONSE_BYTES:
                    raise ProviderError("response_too_large")
                while "\n\n" in buffer or "\r\n\r\n" in buffer:
                    separator = "\r\n\r\n" if "\r\n\r\n" in buffer and ("\n\n" not in buffer or buffer.index("\r\n\r\n") < buffer.index("\n\n")) else "\n\n"
                    event, buffer = buffer.split(separator, 1)
                    events += 1
                    if events > MAX_SSE_EVENTS:
                        raise ProviderError("response_too_large")
                    data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
                    if not data or data == "[DONE]":
                        continue
                    payload = _json_payload(data.encode("utf-8"))
                    usage = _usage(payload) or usage
                    choices = payload.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    choice = choices[0]
                    if not isinstance(choice, Mapping):
                        raise ProviderError("invalid_response")
                    finish_reason = choice.get("finish_reason") or finish_reason
                    delta = choice.get("delta")
                    if not isinstance(delta, Mapping):
                        continue
                    content = delta.get("content")
                    if isinstance(content, str):
                        text_parts.append(content)
                    calls = delta.get("tool_calls")
                    if isinstance(calls, list) and calls:
                        call = calls[0]
                        if not isinstance(call, Mapping):
                            raise ProviderError("invalid_response")
                        tool_id = tool_id or call.get("id")
                        function = call.get("function")
                        if isinstance(function, Mapping):
                            if isinstance(function.get("name"), str):
                                tool_name += function["name"]
                            if isinstance(function.get("arguments"), str):
                                arguments += function["arguments"]
            if buffer.strip():
                # A final event without a blank line is common when a proxy
                # closes immediately after ``[DONE]``; parse it the same way.
                data = "\n".join(line[5:].lstrip() for line in buffer.splitlines() if line.startswith("data:"))
                if data and data != "[DONE]":
                    payload = _json_payload(data.encode("utf-8"))
                    usage = _usage(payload) or usage
        except UnicodeDecodeError:
            raise ProviderError("invalid_response") from None
        if tool_name:
            return ModelResponse(kind="tool_call", text="".join(text_parts)[:MAX_TEXT_CHARS], tool=tool_name[:256],
                                 arguments=_arguments(arguments or "{}"), usage=usage,
                                 tool_call_id=str(tool_id)[:256] if tool_id else None,
                                 finish_reason=str(finish_reason) if finish_reason else None)
        if not text_parts:
            raise ProviderError("invalid_response")
        return ModelResponse(kind="final", text="".join(text_parts)[:MAX_TEXT_CHARS], usage=usage,
                             finish_reason=str(finish_reason) if finish_reason else None)


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
