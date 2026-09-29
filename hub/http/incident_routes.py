"""Operator read-only Incident views for the service monitoring domain."""
from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, request

from hub.auth import require_operator
from hub.http.errors import ApplicationError, error_response
from hub.infrastructure.incident_repository import IncidentRepositoryError
from hub.infrastructure.platform_scheduler_repository import (
    PlatformSchedulerRepositoryError,
)

bp = Blueprint("platform_incidents", __name__, url_prefix="/api/platform/v1")


def _repository():
    repository = current_app.extensions.get("fleet", {}).get("repositories", {}).get("platform_incidents")
    if repository is None:
        raise ApplicationError("incident_monitoring_unavailable", "事件监控不可用", 503)
    return repository


def _monitoring_service():
    service = current_app.extensions.get("fleet", {}).get("services", {}).get("service_health")
    if service is None:
        raise ApplicationError("service_monitoring_unavailable", "服务监控不可用", 503)
    return service


def _invoke(fn):
    try:
        return fn()
    except ApplicationError as exc:
        return error_response(exc)
    except IncidentRepositoryError as exc:
        status = {"invalid_state": 400, "invalid_limit": 400}.get(exc.code, 503)
        detail = "事件请求不合法" if status == 400 else "事件存储不可用"
        return error_response(ApplicationError(exc.code, detail, status))
    except PlatformSchedulerRepositoryError as exc:
        status = 400 if exc.code.startswith("invalid_") else 503
        detail = "调度请求不合法" if status == 400 else "调度存储不可用"
        return error_response(ApplicationError(exc.code, detail, status))


@bp.get("/incidents")
@require_operator
def list_incidents():
    def _list():
        state = request.args.get("state") or None
        raw_limit = request.args.get("limit", "100")
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            raise ApplicationError("invalid_limit", "limit 不合法", 400) from None
        if limit < 1 or limit > 500:
            raise ApplicationError("invalid_limit", "limit 不合法", 400)
        rows = _repository().list(g.operator, state=state, limit=limit)
        return jsonify({"ok": True, "incidents": rows})
    return _invoke(_list)


@bp.get("/incidents/<incident_id>")
@require_operator
def get_incident(incident_id):
    def _get():
        row = _repository().get(g.operator, incident_id)
        if row is None:
            raise ApplicationError("not_found", "事件不存在", 404)
        return jsonify({"ok": True, "incident": row})
    return _invoke(_get)


@bp.post("/komari/sync")
@require_operator
def sync_komari():
    def _sync():
        if not current_app.config.get("KOMARI_ENABLED", False):
            raise ApplicationError("not_found", "接口不存在", 404)
        integration = current_app.extensions.get("fleet", {}).get("integrations", {}).get("komari")
        service = current_app.extensions.get("fleet", {}).get("services", {}).get("incidents")
        if integration is None or service is None:
            raise ApplicationError("komari_unavailable", "Komari 监控不可用", 503)
        services = _monitoring_service().repository.list_services(g.operator)
        return jsonify(service.sync_from_client(g.operator, services, integration))
    return _invoke(_sync)


@bp.get("/komari/status")
@require_operator
def komari_status():
    def _status():
        if not current_app.config.get("KOMARI_SYNC_ENABLED", False):
            raise ApplicationError("not_found", "接口不存在", 404)
        service = current_app.extensions.get("fleet", {}).get("services", {}).get(
            "platform_monitoring")
        if service is None:
            raise ApplicationError("komari_scheduler_unavailable", "Komari 调度不可用", 503)
        return jsonify({"ok": True, "status": service.status()})
    return _invoke(_status)


@bp.get("/http-probe/status")
@require_operator
def http_probe_status():
    def _status():
        if not current_app.config.get("HTTP_PROBE_SYNC_ENABLED", False):
            raise ApplicationError("not_found", "接口不存在", 404)
        service = current_app.extensions.get("fleet", {}).get("services", {}).get(
            "http_probe_monitoring")
        if service is None:
            raise ApplicationError("http_probe_scheduler_unavailable", "HTTP 探针调度不可用", 503)
        return jsonify({"ok": True, "status": service.status()})
    return _invoke(_status)


__all__ = ["bp"]
