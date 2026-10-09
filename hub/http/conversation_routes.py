"""Operator conversation/Run HTTP adapter."""
from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, request

from hub.auth import require_operator
from hub.http.errors import ApplicationError, error_response

bp = Blueprint("platform_conversations", __name__, url_prefix="/api/platform/v1")


def _services():
    services = current_app.extensions.get("fleet", {}).get("services", {})
    return services["conversations"], services["runs"], services["run_events"]


def _approvals():
    services = current_app.extensions.get("fleet", {}).get("services", {})
    service = services.get("submit_approvals")
    if service is None:
        raise ApplicationError("submit_disabled", "审批提交能力未启用", 404)
    return service


def _body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ApplicationError("invalid_json", "请求体必须是 JSON 对象", 400)
    return body


def _call(fn):
    try:
        return fn()
    except ApplicationError as exc:
        return error_response(exc)


@bp.post("/conversations")
@require_operator
def create_conversation():
    def _create():
        body = _body()
        overrides = body.get("overrides", {})
        if "overrides" in body and not isinstance(overrides, dict):
            raise ApplicationError("invalid_value", "请求数据不合法", 400)
        return jsonify(_services()[0].create(
            g.operator, title=body.get("title", ""),
            workspace_id=body.get("workspace_id"),
            overrides=overrides,
        ))
    return _call(_create)


@bp.get("/conversations")
@require_operator
def list_conversations():
    def _list():
        try:
            limit = int(request.args.get("limit", "50"))
        except (TypeError, ValueError):
            raise ApplicationError("invalid_limit", "limit 不合法", 400) from None
        if limit < 1:
            raise ApplicationError("invalid_limit", "limit 不合法", 400)
        archived = request.args.get("archived", "false")
        if archived not in ("true", "false"):
            raise ApplicationError("invalid_value", "archived 必须为 true 或 false", 400)
        return jsonify(_services()[0].list(
            g.operator, limit=limit, archived=archived == "true"))
    return _call(_list)


@bp.get("/conversations/<conversation_id>")
@require_operator
def get_conversation(conversation_id):
    return _call(lambda: jsonify(_services()[0].get(g.operator, conversation_id)))

@bp.patch("/conversations/<conversation_id>")
@require_operator
def rename_conversation(conversation_id):
    def _rename():
        body = _body()
        if set(body) != {"title"}:
            raise ApplicationError("invalid_value", "请求数据不合法", 400)
        return jsonify(_services()[0].rename(
            g.operator, conversation_id, body["title"]))
    return _call(_rename)

@bp.post("/conversations/<conversation_id>/archive")
@require_operator
def archive_conversation(conversation_id):
    return _call(lambda: jsonify(_services()[0].set_archived(
        g.operator, conversation_id, archived=True)))

@bp.post("/conversations/<conversation_id>/restore")
@require_operator
def restore_conversation(conversation_id):
    return _call(lambda: jsonify(_services()[0].set_archived(
        g.operator, conversation_id, archived=False)))

@bp.delete("/conversations/<conversation_id>")
@require_operator
def delete_conversation(conversation_id):
    return _call(lambda: jsonify(_services()[0].delete_archived(
        g.operator, conversation_id)))


@bp.post("/conversations/<conversation_id>/turns")
@require_operator
def append_turn(conversation_id):
    return _append_turn(conversation_id)


@bp.post("/conversations/<conversation_id>/acceptance-turns")
@require_operator
def append_acceptance_turn(conversation_id):
    return _append_turn(conversation_id, acceptance=True)


def _append_turn(conversation_id, *, acceptance=False):
    def _turn():
        body = _body()
        overrides = body.get("overrides", {})
        if "overrides" in body and not isinstance(overrides, dict):
            raise ApplicationError("invalid_value", "请求数据不合法", 400)
        if "memory_context" in body and body["memory_context"] is not None \
                and not isinstance(body["memory_context"], dict):
            raise ApplicationError("invalid_memory_context", "memory_context 必须是 JSON 对象", 400)
        if "memory_context" in body and body["memory_context"] is None:
            raise ApplicationError("invalid_memory_context", "memory_context 必须是 JSON 对象", 400)
        return jsonify(_services()[0].turn(
            g.operator, conversation_id,
            text=body.get("text"), client_token=body.get("client_token"),
            overrides=overrides,
            memory_context=body.get("memory_context")
            if "memory_context" in body else None,
            acceptance=acceptance,
        )), 202
    return _call(_turn)


@bp.get("/runs/<run_id>")
@require_operator
def get_run(run_id):
    return _call(lambda: jsonify(_services()[1].get(g.operator, run_id)))


@bp.post("/runs/<run_id>/cancel")
@require_operator
def cancel_run(run_id):
    return _call(lambda: jsonify(_services()[1].cancel(g.operator, run_id)))


@bp.get("/runs/<run_id>/events")
@require_operator
def get_run_events(run_id):
    def _events():
        try:
            after = int(request.args.get("after", "0"))
        except (TypeError, ValueError):
            after = -1
        return jsonify(_services()[2].list(
            g.operator, run_id, after=after,
            limit=request.args.get("limit", 100),
        ))
    return _call(_events)


@bp.post("/runs/<run_id>/browser-approvals")
@require_operator
def grant_browser_approval(run_id: str):
    return _call(lambda: jsonify(_approvals().grant(
        g.operator, run_id, body=_body(), idempotency_key=request.headers.get("Idempotency-Key"),
    )))


@bp.delete("/runs/<run_id>/browser-approvals/<approval_id>")
@require_operator
def revoke_browser_approval(run_id: str, approval_id: str):
    return _call(lambda: jsonify(_approvals().revoke(g.operator, run_id, approval_id)))


@bp.get("/runs/<run_id>/browser-approvals/<approval_id>")
@require_operator
def get_browser_approval(run_id: str, approval_id: str):
    return _call(lambda: jsonify(_approvals().get(g.operator, run_id, approval_id)))


__all__ = ["bp"]
