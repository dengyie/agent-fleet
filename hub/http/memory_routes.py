"""Operator HTTP adapter for explicit platform MemoryItems."""
from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, request

from hub.auth import require_operator
from hub.http.errors import ApplicationError, error_response
from hub.infrastructure.platform_memory_repository import (
    PlatformMemoryRepositoryError,
)

bp = Blueprint("platform_memory", __name__, url_prefix="/api/platform/v1")


def _service():
    service = current_app.extensions.get("fleet", {}).get("services", {}).get(
        "platform_memory")
    if service is None:
        raise ApplicationError("memory_unavailable", "记忆服务不可用", 404)
    return service


def _body() -> dict:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApplicationError("invalid_json", "请求体必须是 JSON 对象", 400)
    return body


def _limit(default: int) -> int:
    raw = request.args.get("limit", str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ApplicationError("invalid_limit", "limit 不合法", 400) from exc
    if value < 1:
        raise ApplicationError("invalid_limit", "limit 不合法", 400)
    return min(value, 100)


def _expected_revision(body=None) -> int:
    raw = request.headers.get("If-Match")
    if raw is None and isinstance(body, dict):
        raw = body.get("revision")
    if isinstance(raw, str):
        raw = raw.strip().strip('"')
    elif raw is not None and type(raw) is not int:
        raise ApplicationError("invalid_revision", "revision 不合法", 400)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ApplicationError("precondition_required", "需要 If-Match revision", 428) from exc
    if value < 0:
        raise ApplicationError("invalid_revision", "revision 不合法", 400)
    return value


def _invoke(fn):
    try:
        return fn()
    except ApplicationError as exc:
        return error_response(exc)
    except PlatformMemoryRepositoryError as exc:
        status = {
            "memory_not_found": 404,
            "memory_conflict": 409,
            "revision_conflict": 409,
            "memory_search_unavailable": 503,
            "memory_store": 503,
            "memory_store_corrupt": 503,
        }.get(exc.code, 400)
        return error_response(ApplicationError(exc.code, "记忆操作失败", status))


@bp.get("/memory")
@require_operator
def list_memory():
    return _invoke(lambda: jsonify(_service().list(g.operator, limit=_limit(50))))


@bp.post("/memory")
@require_operator
def create_memory():
    def _create():
        body = _body()
        values = body.get("memory", body)
        if not isinstance(values, dict):
            raise ApplicationError("invalid_json", "memory 必须是 JSON 对象", 400)
        return jsonify(_service().create(g.operator, values)), 201
    return _invoke(_create)


@bp.get("/memory/search")
@require_operator
def search_memory():
    def _search():
        query = request.args.get("q")
        if not isinstance(query, str):
            raise ApplicationError("invalid_query", "q 不合法", 400)
        return jsonify(_service().search(
            g.operator, query, limit=_limit(20),
        ))
    return _invoke(_search)


@bp.get("/memory/<memory_id>")
@require_operator
def get_memory(memory_id):
    return _invoke(lambda: jsonify(_service().get(g.operator, memory_id)))


@bp.put("/memory/<memory_id>")
@require_operator
def update_memory(memory_id):
    def _update():
        body = _body()
        values = body.get("memory", body)
        if not isinstance(values, dict):
            raise ApplicationError("invalid_json", "memory 必须是 JSON 对象", 400)
        return jsonify(_service().update(
            g.operator, memory_id, values,
            expected_revision=_expected_revision(body),
        ))
    return _invoke(_update)


@bp.delete("/memory/<memory_id>")
@require_operator
def delete_memory(memory_id):
    def _delete():
        body = request.get_json(silent=True)
        if body is not None and not isinstance(body, dict):
            raise ApplicationError("invalid_json", "请求体必须是 JSON 对象", 400)
        return jsonify(_service().delete(
            g.operator, memory_id,
            expected_revision=_expected_revision(body or {}),
        ))
    return _invoke(_delete)
