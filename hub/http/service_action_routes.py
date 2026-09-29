"""Operator routes for gated service actions and approval grants."""
from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, request

from hub.auth import require_operator
from hub.application.task_service import ApplicationError
from hub.http.errors import error_response

bp = Blueprint("platform_service_actions", __name__, url_prefix="/api/platform/v1")


def _service():
    if not current_app.config.get("SERVICE_ACTIONS_ENABLED"):
        raise ApplicationError("not_found", "接口不存在", 404)
    service = current_app.extensions.get("fleet", {}).get("services", {}).get("service_actions")
    if service is None:
        raise ApplicationError("service_actions_unavailable", "服务动作不可用", 503)
    return service


def _body():
    body = request.get_json(silent=True)
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise ApplicationError("invalid_json", "请求体必须是 JSON 对象", 400)
    return body


def _invoke(fn):
    try:
        return fn()
    except ApplicationError as exc:
        return error_response(exc)


@bp.post("/services/<service_id>/actions")
@require_operator
def request_action(service_id):
    def _request():
        body = _body()
        return jsonify(_service().request(
            g.operator, service_id, body.get("action"),
            arguments=body.get("arguments"),
            idempotency_key=body.get("idempotency_key") or request.headers.get("Idempotency-Key"),
        ))
    return _invoke(_request)


@bp.get("/approvals/<grant_id>")
@require_operator
def get_approval(grant_id):
    return _invoke(lambda: jsonify(_service().get_grant(g.operator, grant_id)))


@bp.post("/approvals/<grant_id>/decisions")
@require_operator
def decide_approval(grant_id):
    def _decide():
        body = _body()
        approve = body.get("decision") == "approve" or body.get("approved") is True
        if body.get("decision") not in (None, "approve", "reject") and body.get("approved") is not True:
            raise ApplicationError("invalid_decision", "审批决定不合法", 400)
        return jsonify(_service().decide(
            g.operator, grant_id, approve=approve))
    return _invoke(_decide)


__all__ = ["bp"]
