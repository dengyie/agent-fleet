"""Versioned, read-only HTTP health probe integration.

The probe is intentionally separate from the existing collector.  It owns a
small response contract that can be validated against a captured fixture, and
its network path is explicitly opt-in so importing or constructing it cannot
contact an endpoint.
"""
from __future__ import annotations

import hashlib
import json
import ipaddress
import math
import http.client
import errno
import ssl
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlparse

from platform_schema import validate_id


class HttpProbeError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True)
class HttpProbePolicy:
    """Operator-owned scheduling policy for one HTTP service endpoint."""

    base_url: str
    path: str
    interval_s: float = 60.0
    timeout_s: float = 10.0
    ttl_s: float = 180.0
    allow_loopback: bool = False
    probe_id: str = "health"
    enabled: bool = True


def _bounded_duration(value: Any, *, minimum: float, maximum: float, code: str) -> float:
    parsed = _finite_number(value, minimum=minimum, maximum=maximum)
    if parsed is None:
        raise HttpProbeError(code)
    return float(parsed)


def parse_http_probe_policy(value: Any) -> HttpProbePolicy:
    if not isinstance(value, Mapping):
        raise HttpProbeError("invalid_policy")
    base_url = value.get("base_url")
    path = value.get("path", value.get("probe_path"))
    try:
        parsed = urlparse(base_url or "")
    except Exception:
        raise HttpProbeError("invalid_endpoint") from None
    if (
        parsed.scheme not in {"http", "https"} or not parsed.hostname
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise HttpProbeError("invalid_endpoint")
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 256:
        raise HttpProbeError("invalid_endpoint")
    if (
        "?" in path or "#" in path or "%" in path or "//" in path or "\\" in path
        or any(part == ".." for part in path.split("/"))
        or any(ord(char) < 0x20 or ord(char) == 0x7f for char in path)
    ):
        raise HttpProbeError("invalid_endpoint")
    probe_id = value.get("probe_id", "health")
    if _text(probe_id, 64) is None:
        raise HttpProbeError("invalid_policy")
    enabled = value.get("enabled", True)
    allow_loopback = value.get("allow_loopback", False)
    if type(enabled) is not bool or type(allow_loopback) is not bool:
        raise HttpProbeError("invalid_policy")
    return HttpProbePolicy(
        base_url=canonical_probe_origin(base_url), path=path,
        interval_s=_bounded_duration(value.get("interval_s", 60), minimum=5, maximum=3600, code="invalid_interval"),
        timeout_s=_bounded_duration(value.get("timeout_s", 10), minimum=0.1, maximum=60, code="invalid_timeout"),
        ttl_s=_bounded_duration(value.get("ttl_s", 180), minimum=1, maximum=7 * 24 * 3600, code="invalid_ttl"),
        allow_loopback=allow_loopback,
        probe_id=str(probe_id).strip(),
        enabled=enabled,
    )


def canonical_probe_origin(value: Any) -> str:
    try:
        parsed = urlparse(value or "")
    except Exception:
        raise HttpProbeError("invalid_endpoint") from None
    if (
        parsed.scheme not in {"http", "https"} or not parsed.hostname
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise HttpProbeError("invalid_endpoint")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
    except (UnicodeError, ValueError):
        raise HttpProbeError("invalid_endpoint") from None
    if ":" in host and not host.startswith("["):
        host = "[" + host + "]"
    default = 443 if parsed.scheme == "https" else 80
    suffix = "" if port in (None, default) else f":{port}"
    return f"{parsed.scheme.lower()}://{host}{suffix}/"


def _resolve_probe_addresses(
    policy: HttpProbePolicy,
    *,
    allow_loopback_override: bool = False,
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> tuple[tuple[Any, ...], ...]:
    """Resolve and validate addresses once for one probe attempt."""
    parsed = urlparse(policy.base_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = resolver(
            parsed.hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except (OSError, socket.gaierror, TypeError):
        raise HttpProbeError("address_unavailable") from None
    if not addresses:
        raise HttpProbeError("address_unavailable")
    loopback_ok = bool(policy.allow_loopback and allow_loopback_override)
    validated: list[tuple[Any, ...]] = []
    for item in addresses:
        if not isinstance(item, (tuple, list)) or len(item) < 5:
            raise HttpProbeError("address_unavailable")
        family, socktype, proto, _canonname, sockaddr = item[:5]
        if family not in {socket.AF_INET, socket.AF_INET6}:
            raise HttpProbeError("address_unavailable")
        if socktype != socket.SOCK_STREAM:
            raise HttpProbeError("address_unavailable")
        host = sockaddr[0] if isinstance(sockaddr, tuple) and sockaddr else None
        if not isinstance(host, str):
            raise HttpProbeError("address_unavailable")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise HttpProbeError("address_unavailable") from None
        if address.is_loopback and loopback_ok:
            if family == socket.AF_INET6:
                validated.append((family, socktype, proto, "", (str(address), port, 0, 0)))
            else:
                validated.append((family, socktype, proto, "", (str(address), port)))
            continue
        if (
            address.is_loopback or address.is_private or address.is_link_local
            or address.is_multicast or address.is_unspecified or address.is_reserved
        ):
            raise HttpProbeError("unsafe_address")
        if family == socket.AF_INET6:
            validated.append((family, socktype, proto, "", (str(address), port, 0, 0)))
        else:
            validated.append((family, socktype, proto, "", (str(address), port)))
    return tuple(validated)


def validate_probe_endpoint(
    policy: HttpProbePolicy,
    *,
    allow_loopback_override: bool = False,
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> None:
    """Resolve and reject unsafe destinations before opening an HTTP socket."""
    _resolve_probe_addresses(
        policy,
        allow_loopback_override=allow_loopback_override,
        resolver=resolver,
    )



def _pinned_connect(connection: http.client.HTTPConnection) -> None:
    """Connect to validated socket addresses without hostname resolution."""
    last_error: OSError | None = None
    for family, socktype, proto, _canonname, sockaddr in connection._probe_addresses:
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(connection.timeout)
            sock.connect(sockaddr)
            connection.sock = sock
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError as exc:
                if getattr(exc, "errno", None) != errno.ENOPROTOOPT:
                    raise
            return
        except OSError as exc:
            last_error = exc
            sock.close()
    raise last_error or OSError("connection failed")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port, addresses, *, timeout):
        super().__init__(host, port, timeout=timeout)
        self._probe_addresses = addresses

    def connect(self):
        _pinned_connect(self)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, addresses, *, timeout):
        super().__init__(
            host,
            port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._probe_addresses = addresses

    def connect(self):
        _pinned_connect(self)
        self.sock = self._context.wrap_socket(
            self.sock,
            server_hostname=self.host,
        )


def _text(value: Any, limit: int = 128) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return None
    value = value.strip()
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return None
    return value


def _finite_number(value: Any, *, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < minimum or number > maximum:
        return None
    return number


@dataclass(frozen=True)
class HttpProbeSnapshot:
    probe_id: str
    state: str
    status_code: int | None
    latency_ms: float | None
    observed_at: float

    def __post_init__(self):
        if _text(self.probe_id) != self.probe_id:
            raise ValueError("invalid snapshot")
        if self.state not in {"healthy", "degraded", "unhealthy", "unknown"}:
            raise ValueError("invalid snapshot")
        if self.status_code is not None and (
            type(self.status_code) is not int or not 100 <= self.status_code <= 599
        ):
            raise ValueError("invalid snapshot")
        if self.latency_ms is not None and _finite_number(
            self.latency_ms, minimum=0, maximum=300_000
        ) is None:
            raise ValueError("invalid snapshot")
        if _finite_number(self.observed_at, minimum=0, maximum=4_102_444_800) is None:
            raise ValueError("invalid snapshot")

    def as_dict(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id, "state": self.state,
            "status_code": self.status_code, "latency_ms": self.latency_ms,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class HttpProbePayloadContract:
    schema_version: int = 1

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ValueError("invalid schema version")

    def parse(self, payload: Any, *, observed_at: float | None = None) -> HttpProbeSnapshot:
        if not isinstance(payload, Mapping):
            raise HttpProbeError("invalid_response")
        version = payload.get("schema_version")
        if type(version) is not int or version != self.schema_version:
            raise HttpProbeError("unsupported_schema")
        probe = payload.get("probe")
        if not isinstance(probe, Mapping):
            raise HttpProbeError("invalid_response")
        probe_id = _text(probe.get("id"))
        state = _text(probe.get("state"), 32)
        if not probe_id or state not in {"healthy", "degraded", "unhealthy", "unknown"}:
            raise HttpProbeError("invalid_response")
        status_raw = probe.get("status_code")
        status = None
        if status_raw is not None:
            if type(status_raw) is not int or not 100 <= status_raw <= 599:
                raise HttpProbeError("invalid_response")
            status = status_raw
        latency = None
        if probe.get("latency_ms") is not None:
            latency = _finite_number(probe.get("latency_ms"), minimum=0, maximum=300_000)
            if latency is None:
                raise HttpProbeError("invalid_response")
        observed_value = probe.get("observed_at", observed_at)
        if observed_value is None:
            observed_value = time.time()
        observed = _finite_number(observed_value, minimum=0, maximum=4_102_444_800)
        if observed is None:
            raise HttpProbeError("invalid_response")
        return HttpProbeSnapshot(probe_id, state, status, latency, observed)


def probe_evidence(
    snapshot: HttpProbeSnapshot,
    service_id: str,
    *,
    observed_at: float | None = None,
    ttl_s: float = 180.0,
    service_version: int | None = None,
) -> dict[str, Any]:
    if not isinstance(snapshot, HttpProbeSnapshot):
        raise HttpProbeError("invalid_evidence")
    try:
        service_id = validate_id(service_id, "service_id")
    except (TypeError, ValueError):
        raise HttpProbeError("invalid_evidence") from None
    timestamp = snapshot.observed_at if observed_at is None else _finite_number(observed_at, minimum=0, maximum=4_102_444_800)
    if timestamp is None:
        raise HttpProbeError("invalid_evidence")
    ttl = _finite_number(ttl_s, minimum=1, maximum=7 * 24 * 3600)
    if ttl is None:
        raise HttpProbeError("invalid_evidence")
    if service_version is not None and (
        type(service_version) is not int or not 1 <= service_version <= 1_000_000
    ):
        raise HttpProbeError("invalid_evidence")
    seed_values = [service_id, snapshot.probe_id, snapshot.observed_at, timestamp, snapshot.state]
    if service_version is not None:
        seed_values.append(service_version)
    seed = json.dumps(
        seed_values,
        separators=(",", ":"), ensure_ascii=True,
    )
    detail: dict[str, Any] = {"probe_id": snapshot.probe_id}
    if snapshot.status_code is not None:
        detail["status_code"] = snapshot.status_code
    if snapshot.latency_ms is not None:
        detail["latency_ms"] = snapshot.latency_ms
    if service_version is not None:
        detail["service_version"] = service_version
    return {
        "service_id": service_id, "dimension": "application_health",
        "source": "http_probe_v1", "state": snapshot.state,
        "observed_at": float(timestamp), "ttl_s": float(ttl),
        "evidence_id": "http_probe_" + hashlib.sha256(seed.encode()).hexdigest()[:40],
        "detail": detail,
    }


class HttpProbeClient:
    MAX_BODY_BYTES = 64 * 1024

    def __init__(
        self,
        base_url: str,
        probe_path: str,
        *,
        requester: Callable[..., Any] | None = None,
        timeout_s: float = 10.0,
        allow_network: bool = False,
        allow_loopback: bool = False,
        validate_addresses: bool = True,
        resolver: Callable[..., Any] = socket.getaddrinfo,
    ):
        parsed = urlparse(base_url or "")
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise HttpProbeError("invalid_endpoint")
        if (
            not isinstance(probe_path, str) or not probe_path.startswith("/")
            or len(probe_path) > 256 or "?" in probe_path or "#" in probe_path
            or "%" in probe_path
            or "//" in probe_path or any(part == ".." for part in probe_path.split("/"))
            or "\\" in probe_path
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in probe_path)
        ):
            raise HttpProbeError("invalid_endpoint")
        try:
            timeout = float(timeout_s)
        except (TypeError, ValueError):
            raise HttpProbeError("invalid_timeout") from None
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 60:
            raise HttpProbeError("invalid_timeout")
        self.base_url = canonical_probe_origin(base_url)
        self.probe_path = probe_path
        self.requester = requester
        self.timeout_s = timeout
        self.allow_network = bool(allow_network)
        self.allow_loopback = bool(allow_loopback)
        # Retain the constructor argument for compatibility; all real network
        # requests validate and pin addresses to preserve the SSRF invariant.
        self.validate_addresses = bool(validate_addresses)
        self.resolver = resolver

    def _urllib_request(
        self,
        *,
        url: str,
        timeout: float,
        addresses: tuple[tuple[Any, ...], ...],
    ) -> bytes:
        parsed = urlparse(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        connection_type = (
            _PinnedHTTPSConnection
            if parsed.scheme == "https"
            else _PinnedHTTPConnection
        )
        connection = connection_type(
            parsed.hostname,
            port,
            addresses,
            timeout=timeout,
        )
        try:
            connection.request(
                "GET",
                parsed.path or "/",
                headers={"Accept": "application/json"},
            )
            response = connection.getresponse()
            code = int(response.status)
            if code in {401, 403}:
                raise HttpProbeError("unauthorized")
            if code == 429:
                raise HttpProbeError("rate_limited")
            if 300 <= code <= 399:
                raise HttpProbeError("redirect_denied")
            if 500 <= code <= 599:
                raise HttpProbeError("upstream_error")
            if code < 200 or code >= 400:
                raise HttpProbeError("http_error")
            body = response.read(self.MAX_BODY_BYTES + 1)
        except HttpProbeError:
            raise
        except (socket.timeout, TimeoutError, OSError, http.client.HTTPException):
            raise HttpProbeError("source_unavailable") from None
        finally:
            connection.close()
        if len(body) > self.MAX_BODY_BYTES:
            raise HttpProbeError("response_too_large")
        return body

    def fetch(self) -> HttpProbeSnapshot:
        if self.requester is None and not self.allow_network:
            raise HttpProbeError("network_disabled")
        addresses = None
        if self.requester is None:
            policy = HttpProbePolicy(
                base_url=self.base_url, path=self.probe_path,
                timeout_s=self.timeout_s, allow_loopback=self.allow_loopback,
            )
            addresses = _resolve_probe_addresses(
                policy, allow_loopback_override=self.allow_loopback,
                resolver=self.resolver,
            )
        url = urljoin(self.base_url, self.probe_path.lstrip("/"))
        try:
            if self.requester is not None:
                response = self.requester(url=url, timeout=self.timeout_s)
            else:
                response = self._urllib_request(
                    url=url,
                    timeout=self.timeout_s,
                    addresses=addresses or (),
                )
        except HttpProbeError:
            raise
        except (socket.timeout, TimeoutError, OSError, http.client.HTTPException):
            raise HttpProbeError("source_unavailable") from None
        if isinstance(response, (bytes, bytearray)):
            if len(response) > self.MAX_BODY_BYTES:
                raise HttpProbeError("response_too_large")
            try:
                payload = json.loads(bytes(response).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise HttpProbeError("invalid_response") from None
        elif isinstance(response, str):
            if len(response.encode("utf-8")) > self.MAX_BODY_BYTES:
                raise HttpProbeError("response_too_large")
            try:
                payload = json.loads(response)
            except ValueError:
                raise HttpProbeError("invalid_response") from None
        elif isinstance(response, Mapping):
            payload = response
        else:
            raise HttpProbeError("invalid_response")
        return HttpProbePayloadContract().parse(payload)


__all__ = [
    "HttpProbeClient", "HttpProbeError", "HttpProbePayloadContract",
    "HttpProbePolicy", "HttpProbeSnapshot", "parse_http_probe_policy",
    "probe_evidence", "validate_probe_endpoint", "canonical_probe_origin",
]
