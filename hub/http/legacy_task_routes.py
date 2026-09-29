"""Bounded operator HTTP adapter for explicit legacy task links."""
from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, request

from hub.auth import require_operator
from hub.http.errors import ApplicationError, error_response

bp = Blueprint("platform_legacy_tasks", __name__, url_prefix="/api/platform/v1")


def _bridge():
    service = current_app.extensions.get("fleet", {}).get("services", {}).get("legacy_task_bridge")
    if service is None:
        raise ApplicationError("legacy_task_unavailable", "旧任务桥接不可用", 404)
    return service


def _body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApplicationError("invalid_json", "请求体必须是 JSON 对象", 400)
    task = body.get("task", body)
    if not isinstance(task, dict):
        raise ApplicationError("invalid_json", "task 必须是 JSON 对象", 400)
    return task


def _call(fn):
    try:
        return fn()
    except ApplicationError as exc:
        return error_response(exc)


@bp.post("/runs/<run_id>/legacy-task")
@require_operator
def enqueue_legacy_task(run_id):
    def _enqueue():
        bridge = _bridge()
        result = bridge.enqueue(g.operator, run_id, _body())
        # A request may complete the old task immediately; all later retries
        # remain owned by the explicit worker lifecycle.
        try:
            result = bridge.process_once(g.operator, run_id=run_id) or result
        except ApplicationError as exc:
            if exc.code not in {"tasks_unavailable", "platform_store"}:
                raise
        return jsonify(result), 202
    return _call(_enqueue)


@bp.get("/runs/<run_id>/legacy-task")
@require_operator
def get_legacy_task(run_id):
    return _call(lambda: jsonify(_bridge().get(g.operator, run_id)))


__all__ = ["bp"]
