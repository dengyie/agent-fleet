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
模块级做任何 credential / 路径查找。Operator 可读取完整 source_record；
事件仍经过共享 schema 校验，以有界分片和分页传输。
"""
from flask import Blueprint, current_app, g, jsonify, request

from hub.application.session_service import SessionService, SessionServiceError
from hub.application.session_search_service import SessionSearchServiceError
from hub.auth import require_ingest_token, require_operator, require_runner
from hub.http.errors import ApplicationError, error_response, get_request_id
from hub.http._common import request_json

bp = Blueprint("sessions", __name__)

_MAX_QUERY_LIMIT = 1000


@bp.route("/api/session-search")
@require_operator
def api_session_search():
    service = current_app.extensions.get("fleet", {}).get("services", {}).get("session_search")
    if service is None:
        return error_response(ApplicationError("search_disabled", "会话搜索尚未启用", 503))
    allowed = {"q", "machine", "project", "agent", "from", "to", "limit", "after"}
    if any(key not in allowed or len(request.args.getlist(key)) != 1 for key in request.args):
        return error_response(ApplicationError("invalid_filter", "搜索参数不合法", 400))
    filters = {key: value for key, value in request.args.items() if key != "q" and value != ""}
    try:
        return jsonify(service.search(request.args.get("q", ""), **filters))
    except SessionSearchServiceError as exc:
        return error_response(ApplicationError(exc.code, "会话搜索请求未完成", exc.status))


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


def _strict_cursor(name: str) -> int | None:
    values = request.args.getlist(name)
    if not values:
        return None
    value = values[0]
    if (len(values) != 1 or not isinstance(value, str)
            or not value.isascii() or not value.isdecimal()
            or (len(value) > 1 and value.startswith("0"))
            or len(value) > 19):
        raise SessionServiceError("invalid_cursor")
    parsed = int(value)
    if parsed > (1 << 63) - 1:
        raise SessionServiceError("invalid_cursor")
    return parsed


def _query_bool(name: str) -> bool:
    return str(request.args.get(name, "")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _ingest_machine_binding_error(events):
    """会话事件的机器绑定（与 /api/ingest 同一规则）。

    - 每机凭据（``g.ingest_machine``）：每个事件 ``machine_id`` 必须等于凭据机器；
    - 共享 token：不得为已由每机凭据绑定的机器写事件（``machine_bound``）。
    非 dict 事件交给服务层逐条 reject，这里不处理。
    """
    bound = getattr(g, "ingest_machine", None)
    machines = [e.get("machine_id") for e in events if isinstance(e, dict)]
    if bound is not None:
        if any(m != bound for m in machines):
            return error_response(ApplicationError(
                "machine_mismatch", "事件 machine_id 与凭据机器不一致", 403))
        return None
    observe = current_app.extensions.get("fleet", {}).get(
        "services", {}).get("observe")
    if observe is None:
        return None
    if any(observe.is_credential_bound(m) for m in set(
            m for m in machines if isinstance(m, str))):
        return error_response(ApplicationError(
            "machine_bound", "该机器已绑定每机凭据", 403))
    return None


@bp.route("/api/session-events", methods=["POST"])
@require_ingest_token
def api_session_events():
    """Observe 域：有界会话事件批量上报。

    请求体必须为事件列表（非对象）。整批超过 100 条或 256 KiB 上限时返回
    有界错误；逐事件校验失败的孤例以 ``rejected`` 数组返回，不丢合法事件。
    响应：``ok/status/stream_id/accepted_through/next_cursor/rejected``。
    """
    try:
        data = request_json(silent=False)
    except ApplicationError as exc:
        return error_response(exc)
    if not isinstance(data, list):
        return error_response(ApplicationError(
            "invalid_json", "请求体必须是事件列表", 400))
    denied = _ingest_machine_binding_error(data)
    if denied is not None:
        return denied
    try:
        result = _sessions().ingest_events(data)
    except SessionServiceError as exc:
        return _session_error(exc)
    result["request_id"] = get_request_id()
    return jsonify(result)


@bp.route("/api/runner-session-events", methods=["POST"])
@require_runner
def api_runner_session_events():
    # A runner can publish only its own machine-scoped stream. No extra
    # ingest credential is needed for the normal task execution path.
    import hashlib
    prefix = "runner_" + hashlib.sha256(g.runner_machine.encode()).hexdigest()[:16] + "_"
    try:
        data = request_json()
    except ApplicationError as exc:
        return error_response(exc)
    if not isinstance(data, list):
        return error_response(ApplicationError("invalid_json", "请求体必须是事件列表", 400))
    if any(not isinstance(e, dict) or e.get("machine_id") != g.runner_machine
           or not str(e.get("session_id", "")).startswith(prefix) for e in data):
        return error_response(ApplicationError("machine_mismatch", "会话不属于当前 runner", 403))
    try:
        return jsonify(_sessions().ingest_events(data))
    except SessionServiceError as exc:
        return _session_error(exc)


@bp.route("/api/sessions")
@require_operator
def api_sessions():
    try:
        cursor_values = request.args.getlist("cursor")
        if len(cursor_values) > 1:
            raise SessionServiceError("invalid_cursor")
        result = _sessions().list_session_page(
            machine=request.args.get("machine") or None,
            limit=_query_limit(), active=_query_bool("active"),
            cursor=cursor_values[0] if cursor_values else None)
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


@bp.route("/api/sessions/<session_id>/messages", methods=["POST"])
@require_operator
def api_session_message(session_id):
    from hub.application.supervisor_service import SupervisorServiceError
    try:
        data = request_json()
    except ApplicationError as exc:
        return error_response(exc)
    if not isinstance(data, dict):
        return error_response(ApplicationError("invalid_message", "请求必须为对象", 400))
    if "request_id" not in data and (
            set(data) - {"text", "client_token"}
            or not isinstance(data.get("text"), str)):
        return error_response(ApplicationError("invalid_message", "text 必须是字符串", 400))
    if "request_id" not in data and "client_token" in data and (
            type(data.get("client_token")) is not str
            or not 1 <= len(data["client_token"]) <= 128
            or data["client_token"].strip() != data["client_token"]
            or any(ord(char) < 0x20 or ord(char) == 0x7f
                   for char in data["client_token"])):
        return _session_error(SessionServiceError("invalid_client_token"))
    try:
        if "request_id" in data:
            return jsonify(_sessions().answer_interaction(session_id, data, actor=g.operator)), 202
        return jsonify(_sessions().send_message(
            session_id, data["text"], actor=g.operator,
            client_token=data.get("client_token"))), 202
    except SessionServiceError as exc:
        return _session_error(exc)
    except SupervisorServiceError as exc:
        return error_response(ApplicationError(exc.code, exc.detail, exc.status))


@bp.route("/api/sessions/<session_id>/events")
@require_operator
def api_session_events_query(session_id):
    try:
        after_cursor = _strict_cursor("after_cursor")
        before_cursor = _strict_cursor("before_cursor")
        at_sequence = _strict_cursor("at_sequence")
        tail_values = request.args.getlist("tail")
        if len(tail_values) > 1:
            raise SessionServiceError("invalid_cursor")
        tail = False
        if tail_values:
            if tail_values[0] in {"1", "true", "yes"}:
                tail = True
            elif tail_values[0] not in {"0", "false", "no"}:
                raise SessionServiceError("invalid_cursor")
        selected_modes = int(after_cursor is not None) + int(before_cursor is not None) + int(tail)
        if selected_modes > 1 or (tail and at_sequence is not None) or (
                before_cursor is not None and at_sequence is not None):
            raise SessionServiceError("invalid_cursor")
        if at_sequence is not None and after_cursor not in (None, 0):
            raise SessionServiceError("invalid_cursor")
        result = _sessions().session_events(
            session_id, limit=_query_limit(), after_cursor=after_cursor or 0,
            before_cursor=before_cursor, tail=tail, at_sequence=at_sequence)
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


def _handoffs():
    service = current_app.extensions.get("fleet", {}).get("services", {}).get("handoffs")
    if service is None:
        raise ApplicationError("handoff_unavailable", "接力服务不可用", 503)
    return service


@bp.route("/api/sessions/<session_id>/handoff", methods=["GET"])
@require_operator
def api_handoff_preview(session_id):
    try:
        return jsonify(_handoffs().preview(session_id))
    except ApplicationError as exc:
        return error_response(exc)


@bp.route("/api/sessions/<session_id>/handoff", methods=["POST"])
@require_operator
def api_handoff_create(session_id):
    try:
        return jsonify(_handoffs().create(session_id, request_json(), g.operator)), 202
    except ApplicationError as exc:
        return error_response(exc)
