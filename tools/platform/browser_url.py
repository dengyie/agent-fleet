"""Shared fail-closed URL/origin normalization for browser policy and transport."""
from __future__ import annotations

import ipaddress
from urllib.parse import SplitResult, urlsplit, urlunsplit


_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _validate_text(value: object) -> str:
    if not isinstance(value, str) or any(
        ord(char) <= 0x20 or ord(char) == 0x7F for char in value
    ):
        raise ValueError
    return value


def _authority_has_empty_port(netloc: str) -> bool:
    authority = netloc.rsplit("@", 1)[-1]
    if authority.endswith("]"):
        return False
    return authority.endswith(":")


def _normalize_parsed(parsed: SplitResult, *, require_root: bool = False) -> tuple[str, str, int]:
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError
    if require_root and parsed.path not in {"", "/"}:
        raise ValueError
    if require_root and (parsed.query or parsed.fragment):
        raise ValueError
    if parsed.username is not None or parsed.password is not None:
        raise ValueError
    if not parsed.netloc or _authority_has_empty_port(parsed.netloc):
        raise ValueError

    host = parsed.hostname
    if not host or "%" in host or host.endswith(".."):
        raise ValueError
    if host.endswith("."):
        host = host[:-1]
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise ValueError from None
    else:
        host = str(literal)

    try:
        parsed_port = parsed.port
    except ValueError:
        raise ValueError from None
    port = (443 if scheme == "https" else 80) if parsed_port is None else parsed_port
    if not 1 <= port <= 65535:
        raise ValueError

    if ":" not in host:
        labels = host.split(".")
        if len(host) > 253 or any(
            not label or len(label) > 63 or label[0] == "-" or label[-1] == "-"
            or any(not (char.isalnum() or char == "-") for char in label)
            for label in labels
        ):
            raise ValueError
    return scheme, host, port


def normalize_origin(value: object, *, require_root: bool = False) -> tuple[str, str, int]:
    parsed = urlsplit(_validate_text(value))
    return _normalize_parsed(parsed, require_root=require_root)


def canonical_url(value: object) -> str:
    text = _validate_text(value)
    parsed = urlsplit(text)
    scheme, host, port = _normalize_parsed(parsed)
    authority_host = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    authority = authority_host if port == default_port else f"{authority_host}:{port}"
    return urlunsplit((scheme, authority, parsed.path or "/", parsed.query, ""))


__all__ = ["canonical_url", "normalize_origin"]
