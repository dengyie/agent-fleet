"""HTTP 适配层共享的请求体解析助手。

各路由模块曾各自复制 ``_body`` / ``_json_object``；语义完全一致的部分收敛到
这里，带额外校验（如 legacy task 包装、执行窗口禁用字段）的模块保留本地实现。
"""
from __future__ import annotations

from flask import request

from hub.http.errors import ApplicationError, error_response

__all__ = [
    "json_object",
    "json_object_or_empty",
    "json_object_or_error",
    "request_json",
]

_INVALID_JSON = ("invalid_json", "请求体必须是 JSON 对象", 400)


def request_json(*, silent: bool = True, force: bool = False):
    """``request.get_json(...)``，深层嵌套 JSON 也归为 invalid_json。

    ``silent=True`` 只吞 ``ValueError``；超深嵌套的数组/对象在解码时抛
    ``RecursionError``，否则会变成 500。这里只在 JSON 解码这一步转换，
    不会掩盖业务代码里的真实递归错误。参数语义与 ``get_json`` 一致。
    """
    try:
        return request.get_json(silent=silent, force=force)
    except RecursionError:
        raise ApplicationError(*_INVALID_JSON) from None


_request_json = request_json


def json_object() -> dict:
    """请求体必须是 JSON 对象；否则抛 ``ApplicationError(invalid_json)``。"""
    body = _request_json()
    if not isinstance(body, dict):
        raise ApplicationError(*_INVALID_JSON)
    return body


def json_object_or_empty() -> dict:
    """空请求体视为 ``{}``；非对象 JSON 抛 ``ApplicationError(invalid_json)``。"""
    body = _request_json()
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise ApplicationError(*_INVALID_JSON)
    return body


def json_object_or_error() -> tuple[dict | None, object | None]:
    """与 ``json_object_or_empty`` 相同，但以 ``(body, error_response)`` 返回错误。"""
    try:
        return json_object_or_empty(), None
    except ApplicationError as exc:
        return None, error_response(exc)
