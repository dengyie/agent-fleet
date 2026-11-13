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
_MAX_JSON_DEPTH = 128


def _check_json_depth(value):
    if not isinstance(value, (dict, list)):
        return
    stack = [iter(value.values() if isinstance(value, dict) else value)]
    while stack:
        try:
            child = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        if isinstance(child, (dict, list)):
            if len(stack) >= _MAX_JSON_DEPTH:
                raise ApplicationError(*_INVALID_JSON)
            stack.append(iter(child.values() if isinstance(child, dict) else child))


def request_json(*, silent: bool = True, force: bool = False):
    """``request.get_json(...)``，深层嵌套 JSON 也归为 invalid_json。

    解码时的 ``RecursionError`` 转为 invalid_json；解码成功后也拒绝超过
    128 层的数组和对象，以保持不同 Python 版本的行为一致。深度检查在
    解码后执行，不限制解析阶段的资源消耗，也不掩盖业务代码中的递归错误。
    参数语义与 ``get_json`` 一致。
    """
    try:
        body = request.get_json(silent=silent, force=force)
    except RecursionError:
        raise ApplicationError(*_INVALID_JSON) from None
    _check_json_depth(body)
    return body


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
