"""Operator API for the execution-window control plane."""
from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, request

from hub.auth import require_operator
from hub.http.errors import ApplicationError, error_response

bp = Blueprint("execution_windows", __name__, url_prefix="/api/platform/v1/execution-windows")


def _service():
    service = current_app.extensions.get("fleet", {}).get("services", {}).get("execution_windows")
    if service is None:
        raise ApplicationError("not_found", "接口不存在", 404)
    return service


def _body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApplicationError("invalid_json", "请求体必须是 JSON 对象", 400)
    forbidden = {"command", "commands", "argv", "executable", "shell", "path", "pid", "pty", "browser", "process"}
    def contains_forbidden(value):
        if isinstance(value, dict):
            return any(
                (isinstance(key, str) and key.lower() in forbidden)
                or contains_forbidden(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(contains_forbidden(item) for item in value)
        return False
    if contains_forbidden(body):
        raise ApplicationError("unsupported_control", "执行窗口不接受宿主进程控制参数", 400)
    return body


def _call(fn):
    try:
        return fn()
    except ApplicationError as exc:
        return error_response(exc)


@bp.post("")
@require_operator
def create_window():
    def _create():
        body = _body()
        return jsonify(_service().create(g.operator, body.get("run_id"), ttl_s=body.get("ttl_s", 900.0), metadata=body.get("metadata")))
    return _call(_create)


@bp.get("")
@require_operator
def list_windows():
    def _list():
        return jsonify(_service().list(
            g.operator,
            run_id=request.args.get("run_id") or None,
            limit=request.args.get("limit", "20"),
        ))
    return _call(_list)


@bp.get("/<window_id>")
@require_operator
def get_window(window_id):
    return _call(lambda: jsonify(_service().get(g.operator, window_id)))


@bp.post("/<window_id>/attach")
@require_operator
def attach_window(window_id):
    def _attach():
        body = _body()
        return jsonify(_service().attach(g.operator, window_id, body.get("ticket")))
    return _call(_attach)


@bp.post("/<window_id>/reconnect")
@require_operator
def reconnect_window(window_id):
    def _reconnect():
        body = _body()
        return jsonify(_service().reconnect(g.operator, window_id, ttl_s=body.get("ttl_s", 300.0)))
    return _call(_reconnect)


@bp.post("/<window_id>/writer")
@require_operator
def acquire_writer(window_id):
    def _acquire():
        body = _body()
        return jsonify(_service().acquire_writer(g.operator, window_id, body.get("holder_id"), ttl_s=body.get("ttl_s", 60.0)))
    return _call(_acquire)


@bp.post("/<window_id>/writer/renew")
@require_operator
def renew_writer(window_id):
    def _renew():
        body = _body()
        return jsonify(_service().renew_writer(g.operator, window_id, body.get("holder_id"), body.get("lease_token"), ttl_s=body.get("ttl_s", 60.0)))
    return _call(_renew)


@bp.post("/<window_id>/writer/release")
@require_operator
def release_writer(window_id):
    def _release():
        body = _body()
        return jsonify(_service().release_writer(g.operator, window_id, body.get("holder_id"), body.get("lease_token")))
    return _call(_release)


@bp.post("/<window_id>/close")
@require_operator
def close_window(window_id):
    return _call(lambda: jsonify(_service().close(g.operator, window_id)))


@bp.post("/<window_id>/events")
@require_operator
def append_event(window_id):
    def _append():
        body = _body()
        return jsonify(_service().append_event(
            g.operator, window_id,
            body.get("holder_id"), body.get("lease_token"),
            body.get("client_event_id"), body.get("kind"),
            payload=body.get("payload", {}),
        ))
    return _call(_append)


@bp.get("/<window_id>/events")
@require_operator
def list_events(window_id):
    def _list():
        return jsonify(_service().list_events(
            g.operator, window_id,
            after=request.args.get("after", "0"),
            limit=request.args.get("limit", "100"),
        ))
    return _call(_list)


__all__ = ["bp"]
