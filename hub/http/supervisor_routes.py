"""hub/http/supervisor_routes.py — supervisor poll/receipt thin HTTP adapter.

Routes (spec §13):
  POST /api/supervisor/poll       Agent 主动拉取本机签名控制命令
  POST /api/supervisor/receipts   Agent 回传控制执行 receipts

Transport only: supervisor credential auth (machine-bound) → JSON parse →
``SupervisorService`` → unified bounded error shape.  The machine is the
authenticated ``g.supervisor_machine``, never a field from the Agent payload.
No raw exception/path/token ever leaves this module.
"""
from flask import Blueprint, current_app, g, jsonify

from hub.application.supervisor_service import (
    SupervisorService,
    SupervisorServiceError,
)
from hub.infrastructure.adoption_repository import AdoptionRepositoryError
from hub.auth import require_operator, require_supervisor
from hub.http.errors import ApplicationError, error_response, get_request_id
from hub.http._common import json_object_or_error as _json_object

bp = Blueprint("supervisor", __name__)


def _supervisor() -> SupervisorService:
    fleet = current_app.extensions.get("fleet", {})
    services = fleet.get("services", {})
    service = services.get("supervisor")
    if service is None:
        raise RuntimeError("supervisor service not wired")
    return service


@bp.route("/api/supervisor/poll", methods=["POST"])
@require_supervisor
def poll():
    data, err = _json_object()
    if err:
        return err
    machine = g.get("supervisor_machine", "")
    recovery = None
    try:
        if "adoption_recovery_after" in data:
            after = data["adoption_recovery_after"]
            if (not isinstance(after, str) or len(after) > 160
                    or any(not (c.isascii() and (c.isalnum() or c in "_-")) for c in after)):
                return error_response(ApplicationError("invalid_cursor", "", 400))
            adoptions = current_app.extensions["fleet"]["services"].get("adoptions")
            recovery = (adoptions.recover_for_machine(machine, after) if adoptions
                        else {"commands": [], "next_cursor": None})
        # Wait for the client to acknowledge all bindings before leasing
        # controls. A bridge failure must not consume a queued pause/stop.
        commands = [] if recovery and recovery["commands"] else _supervisor().poll(machine)
    except AdoptionRepositoryError:
        return error_response(ApplicationError("adoption_store", "", 503))
    except SupervisorServiceError as exc:
        return error_response(ApplicationError(
            code=exc.code, detail=exc.detail, status=exc.status))
    return jsonify({
        "ok": True,
        "commands": commands,
        **({"adoption_recovery": recovery} if recovery is not None else {}),
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


@bp.route("/api/supervisor/receipts/<command_id>", methods=["GET"])
@require_operator
def supervisor_receipt_status(command_id):
    """Operator read path for the receipt of a queued control command."""
    try:
        receipt = _supervisor().receipt_status(command_id)
    except SupervisorServiceError as exc:
        return error_response(ApplicationError(
            code=exc.code, detail=exc.detail, status=exc.status))
    return jsonify({
        "ok": True,
        "receipt": receipt,
        "request_id": get_request_id(),
    })


__all__ = ["bp"]
