"""Operator-only workflow transport; strict bodies and scoped public DTOs."""
from __future__ import annotations

import asyncio
import functools
import re
from typing import Any, Callable
from urllib.parse import urlsplit

from flask import Blueprint, current_app, g, jsonify, request

from hub.auth import require_operator
from hub.application.workflow_operator import WorkflowOperatorError, WorkflowOperatorService
from hub.domain.workflow_contract import parse_input_references
from hub.domain.workflow_control import WorkflowControlRequest
from hub.domain.workflow_errors import WorkflowPersistenceError, WorkflowValidationError
from hub.domain.workflow_json import decode_snapshot
from hub.domain.workflow_operator import WorkflowAcceptanceRequest, WorkflowCreateRequest
from hub.http.errors import ApplicationError, error_response

bp = Blueprint("platform_workflows", __name__, url_prefix="/api/platform/v1")
_MACHINE_HEADERS = ("X-Agent-Fleet-Token", "X-Runner-Credential", "X-Supervisor-Credential", "X-Platform-Node-Credential", "Authorization")
_NOT_FOUND = frozenset({"workflow_not_found", "template_not_found", "summary_not_found"})
_CONFLICTS = frozenset({"revision_conflict", "idempotency_conflict", "generation_conflict", "terminal_workflow", "reconciliation_not_required",
                       "reconciliation_in_progress", "unresolved_execution", "cancelled_workflow", "cancellation_in_progress", "policy_revision_conflict",
                       "generation_exhausted", "retry_requires_failure", "cancelled_generation", "delivery_not_ready", "admission_closed", "admission_frozen", "summary_disabled"})


def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            return None
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


@bp.before_request
def workflow_boundary() -> Any:
    if not current_app.config.get("PLATFORM_WORKFLOWS_ENABLED"):
        return error_response(ApplicationError("not_found", "接口不存在", 404))
    if any(header in request.headers for header in _MACHINE_HEADERS):
        return error_response(ApplicationError("unauthorized", "operator identity required", 401))
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    expected = current_app.config.get("ACCOUNT_ORIGIN") or request.host_url
    supplied = request.headers.get("Origin", "")
    if _origin(supplied) is None or _origin(supplied) != _origin(expected) or request.headers.get("Sec-Fetch-Site") == "cross-site":
        return error_response(ApplicationError("invalid_origin", "请求来源无效", 403))
    if not request.is_json:
        return error_response(ApplicationError("invalid_json", "请求体必须是 JSON 对象", 400))
    return None


def _service() -> WorkflowOperatorService:
    service = current_app.extensions.get("fleet", {}).get("services", {}).get("workflows")
    if service is None:
        raise ApplicationError("workflow_unavailable", "流程服务不可用", 503)
    return service


def _body(required: set[str], optional: set[str] | None = None) -> dict[str, Any]:
    raw = request.stream.read(65537)
    value = decode_snapshot(raw)
    if set(value) - required - (optional or set()) or required - set(value):
        raise WorkflowValidationError("invalid_object_fields")
    return value


def _query(allowed: set[str]) -> dict[str, str]:
    if set(request.args) - allowed or any(len(request.args.getlist(key)) != 1 for key in request.args):
        raise WorkflowValidationError("invalid_query")
    return request.args.to_dict()


def _number(value: str, *, minimum: int, maximum: int) -> int:
    if len(value) > 16 or re.fullmatch(r"0|[1-9][0-9]*", value) is None:
        raise WorkflowValidationError("invalid_integer")
    number = int(value)
    if not minimum <= number <= maximum:
        raise WorkflowValidationError("invalid_integer")
    return number


def _invoke(view: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(view)
    def handle(*args: Any, **kwargs: Any) -> Any:
        try:
            return view(*args, **kwargs)
        except ApplicationError as exc:
            return error_response(exc)
        except (WorkflowOperatorError, WorkflowValidationError, WorkflowPersistenceError) as exc:
            code = exc.code
            status = 400 if isinstance(exc, WorkflowValidationError) else 503
            if code in _NOT_FOUND or code == "reference_forbidden":
                status = 404
                code = "not_found"
            elif code in _CONFLICTS:
                status = 409
            elif code in ("json_too_large", "contract_too_large", "event_too_large"):
                status = 413
            elif code in ("capacity_exhausted", "budget_exhausted"):
                status = 429
            elif code.startswith("invalid_") or code in ("unexpected_step_control", "unexpected_control_reason", "unexpected_input_change", "duplicate_json_key", "input_binding_mismatch", "input_reference_conflict"):
                status = 400
            return error_response(ApplicationError(code, "流程请求不合法" if status < 500 else "流程服务不可用", status))
    return handle


def _summaries():
    service = current_app.extensions.get("fleet", {}).get("services", {}).get("workflow_summaries")
    if service is None:
        raise ApplicationError("workflow_unavailable", "摘要服务不可用", 503)
    return service


@bp.post("/workflow-summaries")
@require_operator
@_invoke
def publish_summary() -> Any:
    values = _body({"workspace_id", "summary_id", "title", "content", "client_token", "expected_epoch"})
    return jsonify({"ok": True, "summary": _summaries().publish(g.operator, values)}), 201


@bp.get("/workflow-summaries")
@require_operator
@_invoke
def list_summaries() -> Any:
    query = _query({"workspace_id", "limit", "cursor"})
    return jsonify({"ok": True, **_summaries().list(g.operator, query.get("workspace_id"),
        limit=_number(query.get("limit", "50"), minimum=1, maximum=100), cursor=query.get("cursor"))})


@bp.get("/workflow-summaries/version")
@require_operator
@_invoke
def get_summary_version() -> Any:
    query = _query({"workspace_id", "summary_id", "revision"})
    return jsonify({"ok": True, "summary": _summaries().get(g.operator, query.get("workspace_id"), query.get("summary_id"),
        revision=_number(query.get("revision", ""), minimum=1, maximum=9007199254740991))})


@bp.get("/workflow-summaries/head")
@require_operator
@_invoke
def get_summary_head() -> Any:
    query = _query({"workspace_id", "summary_id"})
    return jsonify({"ok": True, "summary": _summaries().head(g.operator, query.get("workspace_id"), query.get("summary_id"))})


@bp.post("/workflow-summaries/enabled")
@require_operator
@_invoke
def enable_summary() -> Any:
    values = _body({"workspace_id", "summary_id", "expected_epoch", "enabled"})
    return jsonify({"ok": True, "summary": _summaries().set_enabled(g.operator, values)})


@bp.get("/workflow-templates")
@require_operator
@_invoke
def templates() -> Any:
    _query(set())
    return jsonify({"ok": True, **_service().templates(g.operator)})


@bp.post("/workflows")
@require_operator
@_invoke
def create_workflow() -> Any:
    body = _body({"template_id", "revision", "workspace_id", "input_refs", "client_token", "expected_policy_revision"})
    value = WorkflowCreateRequest(g.operator, body["workspace_id"], body["template_id"], body["revision"],
                                  parse_input_references(body["input_refs"]), body["client_token"], body["expected_policy_revision"])
    return jsonify({"ok": True, "workflow": asyncio.run(_service().create(value))}), 201


@bp.get("/workflows")
@require_operator
@_invoke
def list_workflows() -> Any:
    query = _query({"limit", "cursor"})
    return jsonify({"ok": True, **asyncio.run(_service().list(g.operator, limit=_number(query.get("limit", "50"), minimum=1, maximum=100), cursor=query.get("cursor")))})


@bp.get("/workflows/<workflow_id>")
@require_operator
@_invoke
def get_workflow(workflow_id: str) -> Any:
    _query(set())
    return jsonify({"ok": True, "workflow": asyncio.run(_service().detail(g.operator, workflow_id))})


@bp.get("/workflows/<workflow_id>/events")
@require_operator
@_invoke
def workflow_events(workflow_id: str) -> Any:
    query = _query({"limit", "after"})
    return jsonify({"ok": True, **asyncio.run(_service().events(g.operator, workflow_id,
                   limit=_number(query.get("limit", "100"), minimum=1, maximum=100), after=_number(query.get("after", "0"), minimum=0, maximum=9007199254740991)))})


@bp.post("/workflows/<workflow_id>/<action>")
@require_operator
@_invoke
def control_workflow(workflow_id: str, action: str) -> Any:
    if action not in ("pause", "resume", "cancel"):
        raise ApplicationError("not_found", "接口不存在", 404)
    body = _body({"expected_revision", "client_token"})
    value = WorkflowControlRequest(g.operator, workflow_id, action, body["expected_revision"], body["client_token"])
    return jsonify({"ok": True, "operation": asyncio.run(_service().control(value))}), 202


@bp.post("/workflows/<workflow_id>/steps/<step_key>/<action>")
@require_operator
@_invoke
def control_step(workflow_id: str, step_key: str, action: str) -> Any:
    fields = {"expected_revision", "client_token"}
    if action == "invalidate":
        fields.update(("expected_generation", "reason"))
    elif action == "reconcile":
        fields.add("mode")
    elif action == "retry":
        fields.add("expected_generation")
    else:
        raise ApplicationError("not_found", "接口不存在", 404)
    body = _body(fields, {"input_refs"} if action == "invalidate" else set())
    if action == "reconcile" and body["mode"] != "inspect":
        raise WorkflowValidationError("invalid_reconciliation_mode")
    expected_generation = body.get("expected_generation")
    value = WorkflowControlRequest(g.operator, workflow_id, action, body["expected_revision"], body["client_token"], step_key,
                                    expected_generation, body.get("reason"),
                                    parse_input_references(body["input_refs"]) if "input_refs" in body else None)
    return jsonify({"ok": True, "operation": asyncio.run(_service().control(value))}), 202


@bp.post("/workflows/<workflow_id>/acceptance")
@require_operator
@_invoke
def accept_workflow(workflow_id: str) -> Any:
    body = _body({"decision", "delivery_digest", "expected_revision", "client_token"})
    value = WorkflowAcceptanceRequest(g.operator, workflow_id, body["decision"], body["delivery_digest"], body["expected_revision"], body["client_token"])
    return jsonify({"ok": True, "operation": asyncio.run(_service().acceptance(value))}), 202
