"""Small, read-only Komari snapshot client.

The deployed Komari fork is not treated as a stable public API.  Callers must
provide the exact endpoint path and may inject a requester in tests or in a
deployment adapter.  The client accepts only bounded, allowlisted fields and
never returns the token or raw response body.
"""
from __future__ import annotations

import json
import math
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


class KomariError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class KomariNodeSnapshot:
    external_id: str
    name: str | None
    online: bool | None
    observed_at: float
    detail: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "external_id": self.external_id, "name": self.name,
            "online": self.online, "observed_at": self.observed_at,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class KomariPayloadContract:
    """Bounded structural contract for sanitized Komari node payloads."""

    schema_version: int = 1
    container_keys: tuple[str, ...] = (
        "nodes", "clients", "data", "items", "results",
    )

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ValueError("invalid schema version")
        if not self.container_keys or any(
            not isinstance(key, str) or not key for key in self.container_keys
        ):
            raise ValueError("invalid container keys")

    @staticmethod
    def _is_node_mapping(payload: Mapping[str, Any]) -> bool:
        return any(
            key in payload for key in ("id", "uuid", "client_id", "node_id")
        )

    def _rows_from_mapping(self, payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        if "schema_version" in payload:
            version = payload.get("schema_version")
            if type(version) is not int or version != self.schema_version:
                raise KomariError("unsupported_schema")
        for key in self.container_keys:
            if key not in payload:
                continue
            value = payload.get(key)
            if isinstance(value, list):
                if any(not isinstance(row, Mapping) for row in value):
                    raise KomariError("invalid_response")
                return list(value[:1000])
            if isinstance(value, Mapping):
                return self.rows(value)
            raise KomariError("invalid_response")
        if self._is_node_mapping(payload):
            return [payload]
        raise KomariError("invalid_response")

    def rows(self, payload: Any) -> list[Mapping[str, Any]]:
        if isinstance(payload, list):
            if any(not isinstance(row, Mapping) for row in payload):
                raise KomariError("invalid_response")
            return list(payload[:1000])
        if not isinstance(payload, Mapping):
            raise KomariError("invalid_response")
        return self._rows_from_mapping(payload)


def _bounded_text(value: Any, limit: int = 256) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return None
    value = value.strip()
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in value):
        return None
    return value


def _bounded_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"online", "up", "healthy", "true", "1"}:
            return True
        if normalized in {"offline", "down", "unhealthy", "false", "0"}:
            return False
    return None


def normalize_nodes(payload: Any, *, observed_at: float | None = None) -> list[KomariNodeSnapshot]:
    try:
        timestamp = float(time.time() if observed_at is None else observed_at)
    except (TypeError, ValueError):
        raise KomariError("invalid_observed_at") from None
    if not math.isfinite(timestamp) or timestamp < 0:
        raise KomariError("invalid_observed_at")
    raw_rows = KomariPayloadContract().rows(payload)
    rows: list[KomariNodeSnapshot] = []
    for row in raw_rows:
        external_id = next((_bounded_text(row.get(key), 256) for key in ("uuid", "id", "client_id", "node_id") if _bounded_text(row.get(key), 256)), None)
        if not external_id:
            continue
        name = next((_bounded_text(row.get(key), 256) for key in ("name", "hostname", "label") if _bounded_text(row.get(key), 256)), None)
        online = next((_bounded_bool(row.get(key)) for key in ("online", "alive", "connected", "status", "state") if _bounded_bool(row.get(key)) is not None), None)
        detail = {}
        for key in ("status", "state", "last_seen", "version", "platform"):
            value = row.get(key)
            if isinstance(value, (str, int, float, bool)) and not isinstance(value, (bytes, bytearray)):
                detail[key] = str(value)[:256] if isinstance(value, str) else value
        rows.append(KomariNodeSnapshot(external_id, name, online, timestamp, detail))
    if raw_rows and not rows:
        raise KomariError("invalid_response")
    return rows


class KomariClient:
    """Read-only client with an injected requester and bounded response."""

    MAX_BODY_BYTES = 512 * 1024

    def __init__(
        self, base_url: str, token: str, *, nodes_path: str,
        requester: Callable[..., Any] | None = None, timeout_s: float = 10.0,
        allow_network: bool = False,
    ):
        parsed = urlparse(base_url or "")
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise KomariError("invalid_endpoint")
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise KomariError("invalid_credential")
        if not isinstance(nodes_path, str) or not nodes_path.startswith("/") or len(nodes_path) > 256 or "?" in nodes_path:
            raise KomariError("invalid_endpoint")
        try:
            timeout = float(timeout_s)
        except (TypeError, ValueError):
            raise KomariError("invalid_timeout") from None
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 60:
            raise KomariError("invalid_timeout")
        # Treat the configured endpoint as an origin.  The read-only resource
        # path is the only path component owned by this client.
        self.base_url = f"{parsed.scheme}://{parsed.netloc}/"
        self.token = token
        self.nodes_path = nodes_path
        self.requester = requester
        self.timeout_s = timeout
        self.allow_network = bool(allow_network)

    def _urllib_request(self, *, url: str, token: str, timeout: float) -> bytes:
        request = Request(
            url, headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
            method="GET",
        )
        try:
            response = urlopen(request, timeout=timeout)
        except HTTPError as exc:
            try:
                code = int(exc.code)
                if code in {401, 403}:
                    raise KomariError("unauthorized") from None
                if code == 429:
                    raise KomariError("rate_limited") from None
                if 500 <= code <= 599:
                    raise KomariError("upstream_error") from None
                raise KomariError("http_error") from None
            finally:
                exc.close()
        except (socket.timeout, TimeoutError, URLError, OSError):
            raise KomariError("source_unavailable") from None
        try:
            body = response.read(self.MAX_BODY_BYTES + 1)
        except (socket.timeout, TimeoutError, URLError, OSError):
            raise KomariError("source_unavailable") from None
        finally:
            response.close()
        if len(body) > self.MAX_BODY_BYTES:
            raise KomariError("response_too_large")
        return body

    def _request(self) -> Any:
        url = urljoin(self.base_url, self.nodes_path.lstrip("/"))
        requester = self.requester
        if requester is None:
            if not self.allow_network:
                raise KomariError("network_disabled")
            requester = self._urllib_request
        try:
            response = requester(url=url, token=self.token, timeout=self.timeout_s)
        except KomariError:
            raise
        except Exception:
            raise KomariError("source_unavailable") from None
        if isinstance(response, (bytes, bytearray)):
            if len(response) > self.MAX_BODY_BYTES:
                raise KomariError("response_too_large")
            try:
                return json.loads(bytes(response).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise KomariError("invalid_response") from None
        if isinstance(response, str):
            if len(response.encode("utf-8")) > self.MAX_BODY_BYTES:
                raise KomariError("response_too_large")
            try:
                return json.loads(response)
            except ValueError:
                raise KomariError("invalid_response") from None
        if isinstance(response, Mapping) or isinstance(response, list):
            return response
        raise KomariError("invalid_response")

    def fetch_nodes(self, *, observed_at: float | None = None) -> list[KomariNodeSnapshot]:
        return normalize_nodes(self._request(), observed_at=observed_at)


__all__ = [
    "KomariClient", "KomariError", "KomariNodeSnapshot",
    "KomariPayloadContract", "normalize_nodes",
]
