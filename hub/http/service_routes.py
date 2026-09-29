"""Operator read-only service catalog and health evidence endpoints."""
from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, request

from hub.auth import require_operator
from hub.http.errors import ApplicationError, error_response
from hub.infrastructure.service_repository import ServiceRepositoryError
from hub.domain.service import ServiceValidationError

bp = Blueprint("platform_services", __name__, url_prefix="/api/platform/v1")


def _service():
    service = current_app.extensions.get("fleet", {}).get("services", {}).get("service_health")
    if service is None:
        raise ApplicationError("service_monitoring_unavailable", "服务监控不可用", 503)
    return service


def _body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApplicationError("invalid_json", "请求体必须是 JSON 对象", 400)
    return body


def _invoke(fn):
    try:
        return fn()
    except ApplicationError as exc:
        return error_response(exc)
    except (ServiceRepositoryError, ServiceValidationError) as exc:
        code = getattr(exc, "code", "service_store")
        status = {"service_not_found": 404, "reference_forbidden": 409, "evidence_conflict": 409}.get(code, 400)
        return error_response(ApplicationError(code, "服务请求不合法", status))


@bp.get("/services")
@require_operator
def list_services():
    return _invoke(lambda: jsonify(_service().list(g.operator)))


@bp.get("/services/<service_id>")
@require_operator
def get_service(service_id):
    return _invoke(lambda: jsonify(_service().get(g.operator, service_id)))


@bp.post("/services")
@require_operator
def register_service():
    return _invoke(lambda: jsonify(_service().register(g.operator, _body())))


__all__ = ["bp"]
