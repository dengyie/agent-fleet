"""Operator API for the default-off durable read-only schedule control plane."""
from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, request

from hub.application.platform_schedule_service import PlatformScheduleError
from hub.auth import require_operator
from hub.http.errors import ApplicationError, error_response

bp = Blueprint("platform_schedules", __name__, url_prefix="/api/platform/v1/schedules")


def _service():
    service = current_app.extensions.get("fleet", {}).get("services", {}).get(
        "platform_schedules"
    )
    if service is None:
        raise ApplicationError("not_found", "接口不存在", 404)
    return service


def _body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApplicationError("invalid_json", "请求体必须是 JSON 对象", 400)
    return body


def _error(exc: PlatformScheduleError) -> ApplicationError:
    code = getattr(exc, "code", "schedule_store")
    status = {
        "schedule_not_found": 404,
        "schedule_conflict": 409,
        "revision_conflict": 409,
        "invalid_revision": 400,
        "invalid_action": 400,
        "invalid_target": 400,
        "invalid_id": 400,
        "invalid_interval": 400,
        "invalid_timezone": 400,
        "invalid_missed_policy": 400,
        "invalid_overlap_policy": 400,
        "invalid_schedule": 400,
        "invalid_name": 400,
        "invalid_next_run": 400,
        "invalid_enabled": 400,
        "value_too_large": 413,
    }.get(code, 503)
    detail = "调度请求不合法" if status < 500 else "调度存储不可用"
    return ApplicationError(code, detail, status)


def _call(fn):
    try:
        return fn()
    except ApplicationError as exc:
        return error_response(exc)
    except PlatformScheduleError as exc:
        return error_response(_error(exc))


@bp.get("")
@require_operator
def list_schedules():
    def _list():
        try:
            limit = int(request.args.get("limit", 100))
        except (TypeError, ValueError):
            raise ApplicationError("invalid_limit", "limit 不合法", 400) from None
        if limit < 1 or limit > 100:
            raise ApplicationError("invalid_limit", "limit 不合法", 400)
        return jsonify({"ok": True, "schedules": _service().list(g.operator, limit=limit)})
    return _call(_list)


@bp.post("")
@require_operator
def create_schedule():
    def _create():
        return jsonify({"ok": True, "schedule": _service().create(g.operator, _body())}), 201
    return _call(_create)


@bp.get("/<schedule_id>")
@require_operator
def get_schedule(schedule_id):
    return _call(lambda: jsonify({"ok": True, "schedule": _service().get(g.operator, schedule_id)}))


@bp.put("/<schedule_id>")
@require_operator
def update_schedule(schedule_id):
    def _update():
        body = _body()
        expected = request.headers.get("If-Match", body.get("revision"))
        if isinstance(expected, str):
            expected = expected.strip().strip('"')
        elif expected is not None and type(expected) is not int:
            raise ApplicationError("invalid_revision", "revision 不合法", 400)
        try:
            expected = int(expected) if expected is not None else None
        except (TypeError, ValueError):
            raise ApplicationError("invalid_revision", "revision 不合法", 400) from None
        return jsonify({"ok": True, "schedule": _service().update(
            g.operator, schedule_id, body, expected_revision=expected,
        )})
    return _call(_update)


@bp.delete("/<schedule_id>")
@require_operator
def delete_schedule(schedule_id):
    return _call(lambda: jsonify(_service().delete(g.operator, schedule_id)))


@bp.post("/<schedule_id>/run")
@require_operator
def run_schedule(schedule_id):
    def _run():
        return jsonify(_service().run_once(g.operator, schedule_id)), 202
    return _call(_run)


__all__ = ["bp"]
