"""hub/http/command_routes.py — runner API 薄 HTTP 适配器（runner credential 认证域）

runner 通过 HTTPS 主动 poll；hub 不反向连接机器。
lease TTL 300s，heartbeat 续期；结果按 attempt_id 幂等。
路由与旧 ``hub/routes_commands`` 一致：
  POST /api/commands/poll            领取下一个任务
  POST /api/commands/<id>/heartbeat  续租 + 转发有界日志行
  POST /api/commands/<id>/result     回传结果（可选有界 files 快照）

只做 transport：认证（runner 域）→ JSON 请求体解析 → 调 ``RunnerService`` →
统一错误序列化。机器来自认证后的 ``g.runner_machine``，绝不采信 runner JSON
中的 machine 字段。业务逻辑在 ``hub.application.runner_service``。
"""
from flask import Blueprint, current_app, g, jsonify, request

from hub.application.runner_service import ApplicationError, RunnerService
from hub.auth import require_runner, require_task_store
from hub.http.errors import ApplicationError as HttpApplicationError
from hub.http.errors import error_response

bp = Blueprint("commands", __name__)


def _commands() -> RunnerService:
    fleet = current_app.extensions.get("fleet", {})
    services = fleet.get("services", {})
    service = services.get("runner")
    if service is None:
        raise RuntimeError("runner service not wired")
    return service


def _error(exc):
    return error_response(exc)


def _json_body():
    """解析 JSON 请求体；缺失/null → {}；非对象 JSON → 400 invalid_json（绝不 500）。"""
    data = request.get_json(silent=True)
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        err = HttpApplicationError("invalid_json", "请求体必须是 JSON 对象", 400)
        return None, error_response(err)
    return data, None


@bp.route("/api/commands/poll", methods=["POST"])
@require_runner
@require_task_store
def poll():
    data, err = _json_body()
    if err:
        return err
    try:
        result = _commands().poll(g.runner_machine, data.get("runner_id"))
    except ApplicationError as exc:
        return _error(exc)
    return jsonify(result)


@bp.route("/api/commands/<attempt_id>/heartbeat", methods=["POST"])
@require_runner
@require_task_store
def heartbeat(attempt_id):
    data, err = _json_body()
    if err:
        return err
    try:
        result = _commands().heartbeat(
            g.runner_machine, attempt_id, data.get("nonce"), data.get("log_lines"))
    except ApplicationError as exc:
        return _error(exc)
    return jsonify(result)


@bp.route("/api/commands/<attempt_id>/result", methods=["POST"])
@require_runner
@require_task_store
def result(attempt_id):
    data, err = _json_body()
    if err:
        return err
    try:
        result = _commands().result(
            g.runner_machine, attempt_id, data.get("nonce"),
            data.get("exit_code"), data.get("log_summary", ""),
            data.get("diff_stat", ""), data.get("duration_s"),
            data.get("files"),
            data.get("diff_patch"), data.get("test_summary"))
    except ApplicationError as exc:
        return _error(exc)
    return jsonify(result)