"""hub/http/observe_routes.py — 观测链路薄 HTTP 适配器（ingest token 认证域）

路由（URL、状态码、脱敏与旧 ``hub/routes_observe`` 完全一致）：
  POST /api/ingest        机器自报告（token 认证）
  POST /api/scan          触发 stale reconciliation（token 认证，后台线程）
  GET  /api/status        全部机器摘要（公共，已白名单脱敏）
  GET  /api/machines/<n>  单机详情 + 历史时间线（公共，脱敏）
  GET  /api/events        最近事件摘要（公共，不含快照体）
  GET  /api/stream        SSE 实时事件流（脱敏 + headers 保留）

只做 transport：解析请求 → 认证 → 调 ``ObserveService`` → 序列化。
业务逻辑全部在 ``hub.application.observe_service``；本模块通过
``current_app.extensions["fleet"]["services"]`` 取得服务，不 import 任何
模块级路径存储、hosts 解析器或 SSE 全局门面。
"""
import json
import queue
import threading

from flask import Blueprint, current_app, jsonify, request, Response, stream_with_context
from werkzeug.exceptions import BadRequest

from hub.application.observe_service import ObserveError, ObserveService
from hub.auth import extract_operator_identity, require_ingest_token
from hub.http.errors import ApplicationError, error_response
from report_schema import sanitize_agents, sanitize_system

bp = Blueprint("observe", __name__)

#: The ONLY fields an anonymous caller sees in each ``instances[]`` row of the
#: machine-detail endpoint.  Raw ``pid``/``pgid``/``started_at``/
#: ``exe_path``/``cmdline``/``native_file_path`` are NEVER HTTP-visible without
#: an operator identity (plan constraint #3) — the anonymous view is
#: deliberately metadata-only.  Operator-authenticated callers keep the full
#: sanitized rows because adopting a candidate needs ``pid`` + ``started_at``.
_ANONYMOUS_INSTANCE_FIELDS = ("agent_family", "attachable")


def _identity_gated_machine_detail(detail, identity):
    """Reduce ``instances[]`` rows unless an operator identity is present.

    Keeps every OTHER detail field (``agents``/``system``/``history``/
    ``current``) unchanged; anonymous callers only lose the per-process raw
    identity from each instance row.
    """
    if identity:
        return detail
    projected = dict(detail)
    current = projected.get("current")
    if isinstance(current, dict):
        rows = current.get("instances")
        if isinstance(rows, list):
            reduced = []
            for row in rows:
                if isinstance(row, dict):
                    reduced.append({key: row.get(key)
                                    for key in _ANONYMOUS_INSTANCE_FIELDS
                                    if key in row})
                else:
                    reduced.append(row)
            current = dict(current)
            current["instances"] = reduced
            projected["current"] = current
    return projected


def _fleet_extensions():
    fleet = current_app.extensions.get("fleet", {})
    if not fleet:
        raise RuntimeError("fleet extension not wired")
    return fleet


def _observe_service() -> ObserveService:
    service = _fleet_extensions().get("services", {}).get("observe")
    if service is None:
        raise RuntimeError("observe service not wired")
    return service


def _publisher():
    publisher = _fleet_extensions().get("publisher")
    if publisher is None:
        raise RuntimeError("event publisher not wired")
    return publisher


def _observe_error(exc: ObserveError):
    """ObserveError → 统一错误外型；保留 bounded detail 与 status。

    ``ObserveError`` 已经在应用层截断了 detail（≤200）并计算好 status
    （``not_found`` → 404），这里只做外型转换，不重新丢弃元数据；原始异常
    仅通过其有界的 ``code``/``detail``/``status`` 暴露。
    """
    return error_response(ApplicationError(
        code=exc.code, detail=exc.detail, status=exc.status))


def _observe_service_error(code, detail="", status=400):
    return ApplicationError(code=code, detail=detail, status=status)


def build_summary():
    """机器摘要列表（fleet 页面 SSR 用）。委托给注入的 ObserveService。"""
    return _observe_service().status()["machines"]


@bp.route("/api/ingest", methods=["POST"])
@require_ingest_token
def api_ingest():
    try:
        data = request.get_json(force=True)
    except (ValueError, TypeError, UnicodeDecodeError, BadRequest):
        return error_response(_observe_service_error("invalid_json"))
    if not isinstance(data, dict):
        return error_response(_observe_service_error("invalid_json"))
    try:
        result = _observe_service().ingest(data)
    except ObserveError as exc:
        return _observe_error(exc)
    return jsonify(result)


@bp.route("/api/scan", methods=["POST"])
@require_ingest_token
def api_scan():
    thread = threading.Thread(target=_observe_service().reconcile, daemon=True)
    thread.start()
    return jsonify({"ok": True, "message": "scan triggered (async)"})


@bp.route("/api/status")
def api_status():
    payload = dict(_observe_service().status())
    payload["features"] = {
        "append_user_turn": bool(
            current_app.config.get("APPEND_USER_TURN_ENABLED")),
        "apply_local_profile": bool(
            current_app.config.get("APPLY_LOCAL_PROFILE_ENABLED")),
    }
    return jsonify(payload)


@bp.route("/api/machines/<name>")
def api_machine(name):
    try:
        detail = _observe_service().machine_detail(name)
    except ObserveError as exc:
        return _observe_error(exc)
    # Identity-gated instance disclosure: CF-Access email (or DEV_OPERATOR with
    # no foreign-domain header) sees the full sanitized rows an operator needs
    # to adopt; anonymous callers see the bounded metadata-only projection.
    identity = extract_operator_identity()
    return jsonify(_identity_gated_machine_detail(detail, identity))


@bp.route("/api/events")
def api_events():
    try:
        limit = min(200, max(1, int(request.args.get("limit", 50))))
    except (TypeError, ValueError):
        limit = 50
    return jsonify({"events": _observe_service().events(limit)})


# ---------------------------------------------------------------------------
# SSE 事件名 / payload 映射（对外保留函数名，兼容旧模块测试引用）
# ---------------------------------------------------------------------------

def _sse_event_name(e):
    t = e.get("event", "")
    if t == "state_changed":
        return "machine_update"
    if t == "task_log":
        return "task_log"
    if t.startswith("task_"):
        return "task_update"
    return "fleet_event"


def _sse_payload(e):
    name = _sse_event_name(e)
    extra = e.get("extra", {}) if isinstance(e.get("extra"), dict) else {}
    if name == "machine_update" and isinstance(extra.get("snapshot"), dict):
        snap = extra["snapshot"]
        return {
            "machine": e.get("machine"),
            "changes": e.get("changes", []),
            "online": bool(snap.get("reachable", True)),
            "ts": e.get("ts"),
            "agents": sanitize_agents(snap.get("agents", {})),
            "system": sanitize_system(snap.get("system", {})),
        }
    if name == "task_log":
        return {"task_id": extra.get("task_id"), "line": extra.get("line"),
                "ts": e.get("ts")}
    if name == "task_update":
        return {"task_id": extra.get("task_id"), "machine": e.get("machine"),
                "state": extra.get("state"), "event": e.get("event"), "ts": e.get("ts")}
    return {
        "event": e.get("event"),
        "machine": e.get("machine"),
        "changes": e.get("changes", []),
        "ts": e.get("ts"),
    }


@bp.route("/api/stream")
def api_stream():
    try:
        since = float(request.args.get("since", 0) or 0)
    except (TypeError, ValueError):
        since = 0.0
    publisher = _publisher()
    q, unsubscribe = publisher.subscribe_sse(queue_size=200)

    def gen():
        try:
            yield ": connected\n\n"
            if since:  # 断线重连补发
                for e in publisher.read_since(since, 200):
                    name = _sse_event_name(e)
                    yield f"event: {name}\ndata: {json.dumps(_sse_payload(e), ensure_ascii=False)}\n\n"
            while True:
                try:
                    e = q.get(timeout=25)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                name = _sse_event_name(e)
                yield f"event: {name}\ndata: {json.dumps(_sse_payload(e), ensure_ascii=False)}\n\n"
        finally:
            unsubscribe()

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})