"""Operator HTTP adapter for the additive platform defaults catalog."""
from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, request

from hub.auth import require_operator
from hub.http.errors import ApplicationError, error_response, get_request_id
from hub.infrastructure.platform_db import PlatformRepositoryError
from platform_schema import PlatformValidationError

bp = Blueprint("platform", __name__, url_prefix="/api/platform/v1")


def _service():
    service = current_app.extensions.get("fleet", {}).get("services", {}).get("platform_defaults")
    if service is None:
        raise RuntimeError("platform service not wired")
    return service


def _inspection():
    service = current_app.extensions.get("fleet", {}).get("services", {}).get("command_inspection")
    if service is None:
        raise ApplicationError("platform_store", "命令检查不可用", 503)
    return service


def _postcheck():
    service = current_app.extensions.get("fleet", {}).get("services", {}).get("command_postcheck")
    if service is None:
        raise ApplicationError("platform_store", "命令 post-check 不可用", 503)
    return service


def _json_object():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApplicationError("invalid_json", "请求体必须是 JSON 对象", 400)
    return body


def _expected_revision(body=None) -> int:
    raw = request.headers.get("If-Match")
    if raw is None and isinstance(body, dict):
        raw = body.get("revision")
    if isinstance(raw, str):
        raw = raw.strip().strip('"')
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ApplicationError("precondition_required", "需要 If-Match revision", 428) from None
    if value < 0:
        raise ApplicationError("invalid_revision", "revision 不合法", 400)
    return value


def _invoke(fn):
    try:
        return fn()
    except ApplicationError as exc:
        return error_response(exc)
    except PlatformRepositoryError as exc:
        status = {"reference_forbidden": 409, "invalid_node_credential": 400}.get(exc.code, 503)
        detail = {
            "reference_forbidden": "节点不存在或已禁用",
            "invalid_node_credential": "节点凭据不合法",
        }.get(exc.code, "平台存储不可用")
        return error_response(ApplicationError(exc.code, detail, status))
    except PlatformValidationError as exc:
        return error_response(ApplicationError(exc.code, exc.detail, 400))


@bp.get('/readiness')
@require_operator
def get_readiness():
    def check():
        from hub.application.platform_readiness_service import check_platform_readiness
        result = check_platform_readiness(current_app.extensions['fleet'], g.operator)
        if not result['configuration_ready']:
            result.update(error='platform_not_ready', detail='平台配置不完整', request_id=get_request_id())
        return jsonify(result), 200 if result['configuration_ready'] else 503
    return _invoke(check)


@bp.get("/defaults")
@require_operator
def get_defaults():
    return _invoke(lambda: jsonify(_service().get_defaults(g.operator)))


@bp.put("/defaults")
@require_operator
def put_defaults():
    def _put():
        body = _json_object()
        values = body.get("defaults", body)
        if not isinstance(values, dict):
            raise ApplicationError("invalid_json", "defaults 必须是 JSON 对象", 400)
        result = _service().update_defaults(g.operator, values, _expected_revision(body))
        return jsonify(result)
    return _invoke(_put)


@bp.get("/models")
@require_operator
def get_models():
    return _invoke(lambda: jsonify(_service().list_models(g.operator)))


@bp.get("/workspaces")
@require_operator
def get_workspaces():
    return _invoke(lambda: jsonify({"ok": True, "workspaces": _service().get_defaults(g.operator)["workspaces"]}))


@bp.get("/nodes")
@require_operator
def get_nodes():
    return _invoke(lambda: jsonify({"ok": True, "nodes": _service().get_defaults(g.operator)["nodes"]}))


@bp.get("/commands/unknown")
@require_operator
def list_unknown_commands():
    return _invoke(lambda: jsonify(_inspection().list_unknown(
        g.operator, limit=request.args.get("limit", 50))))


@bp.get("/commands/<command_id>")
@require_operator
def get_command(command_id):
    return _invoke(lambda: jsonify(_inspection().get(g.operator, command_id)))


@bp.post("/commands/<command_id>/reconcile")
@require_operator
def reconcile_command(command_id):
    def _reconcile():
        body = _json_object()
        return jsonify(_inspection().reconcile(
            g.operator, command_id,
            actor=g.operator,
            outcome=body.get("outcome"),
            evidence_source=(body.get("evidence") or {}).get("source")
            if isinstance(body.get("evidence"), dict) else None,
            evidence_reference=(body.get("evidence") or {}).get("reference")
            if isinstance(body.get("evidence"), dict) else None,
        )), 202
    return _invoke(_reconcile)


@bp.post("/commands/<command_id>/postcheck")
@require_operator
def request_command_postcheck(command_id):
    def _request():
        return jsonify(_postcheck().request_any(g.operator, command_id)), 202
    return _invoke(_request)


@bp.get("/commands/<command_id>/postcheck")
@require_operator
def get_command_postcheck(command_id):
    return _invoke(lambda: jsonify(_postcheck().get_any(g.operator, command_id)))


@bp.post("/nodes/<node_id>/credential")
@require_operator
def provision_node_credential(node_id):
    """Issue or rotate a node credential; plaintext is returned once."""
    def _provision():
        body = request.get_json(silent=True)
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise ApplicationError("invalid_json", "请求体必须是 JSON 对象", 400)
        secret = body.get("secret")
        if secret is not None and not isinstance(secret, str):
            raise ApplicationError("invalid_node_credential", "节点凭据不合法", 400)
        repository = current_app.extensions["fleet"]["platform_repository"]
        result = repository.provision_node_credential(g.operator, node_id, secret=secret)
        return jsonify({"ok": True, "node_id": result["node_id"], "credential": result["credential"], "issued_at": result["issued_at"]})
    return _invoke(_provision)


__all__ = ["bp", "get_defaults", "get_models", "get_nodes", "get_workspaces", "put_defaults",
           "list_unknown_commands", "get_command", "reconcile_command",
           "request_command_postcheck", "get_command_postcheck"]
