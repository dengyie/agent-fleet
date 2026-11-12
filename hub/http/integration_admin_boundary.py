"""Shared cookie-admin proof and bounded offline integration management input."""
from __future__ import annotations

import json
from typing import Any

from flask import current_app, g, request

from hub.http.errors import ApplicationError


def require_account_admin() -> None:
    user = g.get("account")
    if not current_app.config.get("ACCOUNTS_ENABLED") or not user or user.get("role") != "admin":
        raise ApplicationError("forbidden", "集成管理需要账户管理员权限", 403)


def strict_admin_body() -> object:
    if request.headers.get("Origin") != current_app.config["ACCOUNT_ORIGIN"]:
        raise ApplicationError("invalid_origin", "请求来源无效", 403)
    if not request.is_json:
        raise ApplicationError("invalid_json", "请求体必须是 JSON", 415)
    if request.content_length is not None and request.content_length > 2048:
        raise ApplicationError("request_too_large", "集成管理请求超过限制", 413)
    raw = request.stream.read(2049)
    if len(raw) > 2048:
        raise ApplicationError("request_too_large", "集成管理请求超过限制", 413)

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def finite(value: str) -> None:
        raise ValueError("invalid JSON constant")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=finite)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ApplicationError("invalid_json", "请求体必须是合法 JSON 对象", 400) from exc
