"""hub/http/supervisor_routes.py — supervisor poll/receipt thin HTTP adapter.

Routes (spec §13):
  POST /api/supervisor/poll       Agent 主动拉取本机签名控制命令
  POST /api/supervisor/receipts   Agent 回传控制执行 receipts

Transport only: supervisor credential auth (machine-bound) → JSON parse →
``SupervisorService`` → unified bounded error shape.  The machine is the
authenticated ``g.supervisor_machine``, never a field from the Agent payload.
No raw exception/path/token ever leaves this module.
"""
from flask import Blueprint, current_app, g, jsonify, request

from hub.application.supervisor_service import (
    SupervisorService,
    SupervisorServiceError,
)
from hub.auth import require_supervisor
from hub.http.errors import ApplicationError, error_response, get_request_id

bp = Blueprint("supervisor", __name__)


def _supervisor() -> SupervisorService:
    fleet = current_app.extensions.get("fleet", {})
    services = fleet.get("services", {})
    service = services.get("supervisor")
    if service is None:
        raise RuntimeError("supervisor service not wired")
    return service


def _json_object() -> tuple[dict | None, object | None]:
    data = request.get_json(silent=True)
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return None, error_response(ApplicationError(
            "invalid_json", "请求体必须是 JSON 对象", 400))
    return data, None


@bp.route("/api/supervisor/poll", methods=["POST"])
@require_supervisor
def poll():
    data, err = _json_object()
    if err:
        return err
    machine = g.get("supervisor_machine", "")
    try:
        commands = _supervisor().poll(machine)
    except SupervisorServiceError as exc:
        return error_response(ApplicationError(
            code=exc.code, detail=exc.detail, status=exc.status))
    return jsonify({
        "ok": True,
        "commands": commands,
        "machine": machine,
        "request_id": get_request_id(),
    })


@bp.route("/api/supervisor/receipts", methods=["POST"])
@require_supervisor
def supervisor_receipts():
    data, err = _json_object()
    if err:
        return err
    machine = g.get("supervisor_machine", "")
    command_id = data.get("command_id") or ""
    status = data.get("status") or ""
    reason = str(data.get("reason") or "")[:120]
    try:
        receipt = _supervisor().receipt(
            command_id, status, reason, machine=machine)
    except SupervisorServiceError as exc:
        return error_response(ApplicationError(
            code=exc.code, detail=exc.detail, status=exc.status))
    return jsonify({
        "ok": True,
        "receipt": receipt,
        "request_id": get_request_id(),
    })


__all__ = ["bp"]