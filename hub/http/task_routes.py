"""hub/http/task_routes.py — task API 薄 HTTP 适配器（CF Access operator 认证域）

路由与旧 ``hub/routes_tasks`` 一致：
  POST /api/tasks                      创建（201 新建 / 200 幂等命中）
  GET  /api/tasks                      列表（可选 machine/state/limit 过滤）
  GET  /api/tasks/<task_id>            详情
  GET  /api/tasks/<task_id>/files      Hub-local 快照列表（无正文）
  GET  /api/tasks/<task_id>/files/<p>  Hub-local 快照正文（有界脱敏）
  POST /api/tasks/<task_id>/cancel     取消
  POST /api/tasks/<task_id>/retry      重试
  POST /api/tasks/<task_id>/pause      暂停
  POST /api/tasks/<task_id>/continue   从暂停继续（新 attempt）
  POST /api/tasks/<task_id>/confirm    人工确认闸放行
  POST /api/tasks/<task_id>/reject     闸拒绝并取消
  GET  /api/tasks/<task_id>/diff       脱敏 patch 正文
  GET  /api/agent-types                可下发 agent 类型（注册表派生）

只做 transport：认证（operator 域，``@require_operator`` 内部用
``extract_operator_identity``）→ 调 ``TaskService`` → 统一错误序列化。
业务逻辑全部在 ``hub.application.task_service``；本模块从
``current_app.extensions["fleet"]["services"]["tasks"]`` 取得服务。
"""
from flask import Blueprint, current_app, g, jsonify, request

from agent_profiles import EXECUTABLE_AGENT_TYPES
from hub.application.task_service import ApplicationError, TaskService
from hub.auth import require_operator, require_task_store
from hub.domain.task import DomainError
from hub.http.errors import error_response

bp = Blueprint("tasks", __name__)


def _tasks() -> TaskService:
    fleet = current_app.extensions.get("fleet", {})
    services = fleet.get("services", {})
    service = services.get("tasks")
    if service is None:
        raise RuntimeError("task service not wired")
    return service


def _error(exc):
    """DomainError / ApplicationError → 统一错误外型（含 request_id）。"""
    return error_response(exc)


@bp.route("/api/tasks", methods=["POST"])
@require_operator
@require_task_store
def create_task():
    try:
        result = _tasks().create(request.get_json(silent=True), g.operator)
    except (DomainError, ApplicationError) as exc:
        return _error(exc)
    return jsonify(result), 201 if result["created"] else 200


@bp.route("/api/tasks")
@require_operator
@require_task_store
def list_tasks():
    state = request.args.get("state") or None
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    try:
        result = _tasks().list(
            machine=request.args.get("machine") or None,
            state=state,
            limit=limit,
        )
    except ApplicationError as exc:
        return _error(exc)
    return jsonify(result)


@bp.route("/api/agent-types")
@require_operator
def list_agent_types():
    """可下发 agent 类型（从 agent_profiles 注册表派生，非硬编码清单）。"""
    return jsonify({"agent_types": list(EXECUTABLE_AGENT_TYPES)})

@bp.route("/api/tasks/<task_id>")
@require_operator
@require_task_store
def get_task(task_id):
    try:
        return jsonify(_tasks().get(task_id))
    except ApplicationError as exc:
        return _error(exc)


@bp.route("/api/tasks/<task_id>/cancel", methods=["POST"])
@require_operator
@require_task_store
def cancel_task(task_id):
    try:
        return jsonify(_tasks().cancel(task_id, g.operator))
    except ApplicationError as exc:
        return _error(exc)


@bp.route("/api/tasks/<task_id>/retry", methods=["POST"])
@require_operator
@require_task_store
def retry_task(task_id):
    try:
        return jsonify(_tasks().retry(task_id, g.operator))
    except ApplicationError as exc:
        return _error(exc)


@bp.route("/api/tasks/<task_id>/files")
@require_operator
@require_task_store
def list_task_files(task_id):
    try:
        return jsonify(_tasks().list_files(task_id))
    except ApplicationError as exc:
        return _error(exc)


@bp.route("/api/tasks/<task_id>/files/<path:relpath>")
@require_operator
@require_task_store
def get_task_file(task_id, relpath):
    try:
        return jsonify(_tasks().get_file(task_id, relpath, g.operator))
    except ApplicationError as exc:
        return _error(exc)


@bp.route("/api/tasks/<task_id>/pause", methods=["POST"])
@require_operator
@require_task_store
def pause_task(task_id):
    try:
        return jsonify(_tasks().pause(task_id, g.operator))
    except ApplicationError as exc:
        return _error(exc)


@bp.route("/api/tasks/<task_id>/continue", methods=["POST"])
@require_operator
@require_task_store
def continue_task(task_id):
    try:
        return jsonify(_tasks().continue_task(task_id, g.operator))
    except ApplicationError as exc:
        return _error(exc)


@bp.route("/api/tasks/<task_id>/confirm", methods=["POST"])
@require_operator
@require_task_store
def confirm_task(task_id):
    try:
        return jsonify(_tasks().confirm(task_id, g.operator))
    except ApplicationError as exc:
        return _error(exc)


@bp.route("/api/tasks/<task_id>/reject", methods=["POST"])
@require_operator
@require_task_store
def reject_task(task_id):
    try:
        return jsonify(_tasks().reject(task_id, g.operator))
    except ApplicationError as exc:
        return _error(exc)


@bp.route("/api/tasks/<task_id>/diff")
@require_operator
@require_task_store
def get_task_diff(task_id):
    try:
        return jsonify(_tasks().get_diff(task_id, g.operator))
    except ApplicationError as exc:
        return _error(exc)