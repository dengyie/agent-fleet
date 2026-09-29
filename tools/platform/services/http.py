"""Allowlisted HTTP application checker."""
from __future__ import annotations

import ipaddress
import math
import socket
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlparse
from urllib.request import Request
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, build_opener

from .base import CollectorError, HealthEvidence, _detail


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, new):
        raise HTTPError(req.full_url, code, "redirect denied", headers, fp)


_DEFAULT_OPENER = build_opener(_NoRedirectHandler())


class HttpCollector:
    source = "http"
    MAX_BODY_BYTES = 16 * 1024

    def __init__(self, endpoints: Mapping[str, str], *, opener: Callable[..., Any] | None = None, resolve: Callable[..., Any] | None = None):
        self.endpoints = dict(endpoints or {})
        # The default opener never follows a response Location header. A
        # redirect target would otherwise bypass the DNS/IP checks performed
        # on the registered URL. Tests and callers may inject an opener, but
        # production construction always uses this bounded transport.
        self.opener = opener or _DEFAULT_OPENER.open
        self.resolve = resolve or socket.getaddrinfo

    def _url(self, alias: str) -> str:
        url = self.endpoints.get(alias)
        if not isinstance(url, str) or not url:
            raise CollectorError("endpoint_unregistered")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.fragment or not parsed.hostname:
            raise CollectorError("invalid_endpoint")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise CollectorError("invalid_endpoint")
        host = parsed.hostname
        try:
            addresses = self.resolve(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except (OSError, TypeError):
            raise CollectorError("dns_failed") from None
        for item in addresses:
            sockaddr = item[4] if len(item) > 4 else item
            ip_text = sockaddr[0] if isinstance(sockaddr, tuple) else sockaddr
            try:
                address = ipaddress.ip_address(ip_text)
            except ValueError:
                continue
            mapped = getattr(address, "ipv4_mapped", None)
            if mapped is not None:
                address = mapped
            if (address.is_loopback or address.is_private or address.is_link_local
                    or address.is_multicast or address.is_unspecified or address.is_reserved):
                raise CollectorError("endpoint_address_denied")
        return url

    def collect(self, service_id: str, alias: str, *, timeout_s: float = 5.0, ttl_s: float = 180.0, now: Callable[[], float] = time.time) -> dict[str, Any]:
        try:
            timeout = float(timeout_s)
        except (TypeError, ValueError):
            raise CollectorError("invalid_timeout") from None
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 300:
            raise CollectorError("invalid_timeout")
        observed = float(now())
        try:
            url = self._url(alias)
            request = Request(url, method="GET", headers={"User-Agent": "agent-fleet-health/1"})
            with self.opener(request, timeout=timeout) as response:
                status_value = getattr(response, "status", None)
                status = int(status_value if status_value is not None else response.getcode())
                if 300 <= status < 400:
                    raise CollectorError("redirect_denied")
                body = response.read(self.MAX_BODY_BYTES + 1)
            truncated = len(body) > self.MAX_BODY_BYTES
            body = body[:self.MAX_BODY_BYTES]
            state = "healthy" if 200 <= status < 400 else "unhealthy"
            detail = _detail(alias=alias, status_code=status, body_bytes=len(body), truncated=truncated)
        except CollectorError:
            raise
        except HTTPError as exc:
            status_code = int(getattr(exc, "code", 0) or 0)
            if 300 <= status_code < 400:
                raise CollectorError("redirect_denied") from None
            if status_code >= 400:
                state, detail = "unhealthy", _detail(
                    alias=alias, status_code=status_code, body_bytes=0, truncated=False,
                )
            else:
                state, detail = "unknown", _detail(
                    alias=alias, collector_status="request_failed", error="HTTPError"
                )
        except TimeoutError:
            state, detail = "unknown", _detail(alias=alias, collector_status="timeout")
        except OSError as exc:
            state, detail = "unknown", _detail(alias=alias, collector_status="request_failed", error=type(exc).__name__)
        return HealthEvidence("application_health", self.source, state, observed, float(ttl_s), detail).as_dict(service_id=service_id)


__all__ = ["HttpCollector"]
