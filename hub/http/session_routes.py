"""hub/http/session_routes.py — session 收口与查询薄 HTTP 适配器（Task 6）

路由：
  POST /api/session-events             会话事件批次入口（Observe ingest 域）
  GET  /api/sessions                  会话元数据列表（Operator 域）
  GET  /api/sessions/<session_id>      单会话元数据（Operator 域）
  GET  /api/sessions/<session_id>/events           有界 redacted 事件流（Operator）
  GET  /api/sessions/<session_id>/policy-signals   有界策略信号索引（Operator）

只做 transport：解析请求 → 认证域装饰器 → 调 ``SessionService`` →
统一错误序列化（含不透明 request_id）。业务逻辑全部在
``hub.application.session_service``；本模块经由
``current_app.extensions["fleet"]["services"]["sessions"]`` 取得服务，不在
模块级做任何 credential / 路径查找。原始 transcript 永不进入任何响应体：
仅返回经过共享 schema 白名单的 bounded DTO。
"""
from flask import Blueprint, current_app, jsonify, request

from hub.application.session_service import SessionService, SessionServiceError
from hub.auth import require_ingest_token, require_operator
from hub.http.errors import ApplicationError, error_response, get_request_id

bp = Blueprint("sessions", __name__)

_MAX_QUERY_LIMIT = 1000


def _sessions() -> SessionService:
    fleet = current_app.extensions.get("fleet", {})
    services = fleet.get("services", {})
    service = services.get("sessions")
    if service is None:
        raise RuntimeError("session service not wired")
    return service


def _session_error(exc: SessionServiceError):
    """SessionServiceError → 统一错误外型（含 request_id，bounded code/detail）。"""
    return error_response(ApplicationError(
        code=exc.code, detail=exc.detail, status=exc.status))


def _query_limit(default: int = 100) -> int:
    try:
        limit = int(request.args.get("limit", default))
    except (TypeError, ValueError):
        return default
    return max(1, min(limit, _MAX_QUERY_LIMIT))


@bp.route("/api/session-events", methods=["POST"])
@require_ingest_token
def api_session_events():
    """Observe 域：有界会话事件批量上报。

    请求体必须为事件列表（非对象）。整批超过 100 条或 256 KiB 上限时返回
    有界错误；逐事件校验失败的孤例以 ``rejected`` 数组返回，不丢合法事件。
    响应：``ok/status/stream_id/accepted_through/next_cursor/rejected``。
    """
    data = request.get_json(force=False)
    if not isinstance(data, list):
        return error_response(ApplicationError(
            "invalid_json", "请求体必须是事件列表", 400))
    try:
        result = _sessions().ingest_events(data)
    except SessionServiceError as exc:
        return _session_error(exc)
    result["request_id"] = get_request_id()
    return jsonify(result)


@bp.route("/api/sessions")
@require_operator
def api_sessions():
    try:
        result = _sessions().list_sessions(
            machine=request.args.get("machine") or None,
            limit=_query_limit())
    except SessionServiceError as exc:
        return _session_error(exc)
    return jsonify(result)


@bp.route("/api/sessions/<session_id>")
@require_operator
def api_session_detail(session_id):
    try:
        result = _sessions().get_session(session_id)
    except SessionServiceError as exc:
        return _session_error(exc)
    return jsonify(result)


@bp.route("/api/sessions/<session_id>/events")
@require_operator
def api_session_events_query(session_id):
    try:
        result = _sessions().session_events(
            session_id, limit=_query_limit())
    except SessionServiceError as exc:
        return _session_error(exc)
    return jsonify(result)


@bp.route("/api/sessions/<session_id>/policy-signals")
@require_operator
def api_session_policy_signals(session_id):
    try:
        result = _sessions().policy_signals(session_id)
    except SessionServiceError as exc:
        return _session_error(exc)
    return jsonify(result)


__all__ = [
    "api_session_events",
    "api_session_events_query",
    "api_session_detail",
    "api_session_policy_signals",
    "api_sessions",
    "bp",
]