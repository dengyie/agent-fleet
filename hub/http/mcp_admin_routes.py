"""Cookie-admin-only offline MCP catalog management HTTP boundary."""
from __future__ import annotations

from dataclasses import asdict
from functools import wraps
import json
import re
from typing import Any, Callable

from flask import Blueprint, Response, current_app, g, request

from hub.application.mcp_catalog_admin import MCPCatalogAdminService
from hub.application.mcp_registration import MCPRegistrationCommand
from hub.domain.mcp_administration import MCPAdminCommand
from hub.domain.mcp_catalog import MCPCatalogError
from hub.http.errors import ApplicationError, error_response, get_request_id
from .integration_admin_boundary import require_account_admin, strict_admin_body

PREFIX = "/api/platform/v1/integrations/mcp"
bp = Blueprint("platform_mcp_admin", __name__, url_prefix=PREFIX)


def _response(**payload: Any) -> Response:
    return Response(json.dumps({"ok": True, **payload}, ensure_ascii=False, allow_nan=False,
        separators=(",", ":")), mimetype="application/json")


def _service() -> MCPCatalogAdminService:
    service = current_app.extensions["fleet"]["services"].get("mcp_catalog_admin")
    if service is None:
        raise ApplicationError("mcp_catalog_unavailable", "MCP 目录管理不可用", 503)
    return service


def _admin(view: Callable[..., Response]) -> Callable[..., Any]:
    @wraps(view)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        try:
            require_account_admin()
            return view(*args, **kwargs)
        except ApplicationError as exc:
            return error_response(exc)
        except MCPCatalogError as exc:
            status = 503
            if exc.code in {"invalid_catalog_command", "invalid_catalog_page", "invalid_connector_id",
                    "invalid_mcp_registration_command", "invalid_mcp_registration_fields", "invalid_connector_scope",
                    "invalid_secret_ref", "invalid_endpoint", "invalid_tool_selection", "unsupported_risk",
                    "unsupported_retry_class", "invalid_timeout", "invalid_result_limit", "invalid_tool_limit",
                    "mcp_registration_requires_environment_secret_ref"}:
                status = 400
            if exc.code == "precondition_required":
                status = 428
            if exc.code == "connector_not_found":
                status = 404
            if exc.code in {"connector_revision_changed", "connector_revoked", "connector_registration_conflict", "connector_revision_exhausted",
                    "mcp_registration_workspace_unavailable", "too_many_connectors", "connector_tool_name_conflict"}:
                status = 409
            detail = {400: "MCP 管理请求格式无效", 404: "连接登记不存在", 409: "目录状态已变化，请重新读取并审阅",
                428: "请先读取目录并提供精确版本", 503: "MCP 目录暂时不可用"}[status]
            error = ApplicationError(exc.code, detail, status)
            error.__cause__ = exc
            return error_response(error)
    return guarded


@bp.after_app_request
def no_store(response: Response) -> Response:
    if request.path == PREFIX or request.path.startswith(PREFIX + "/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def _connector_id() -> str:
    if set(request.args) != {"connector_id"} or len(request.args.getlist("connector_id")) != 1:
        raise ApplicationError("invalid_catalog_query", "需要唯一的连接登记标识", 400)
    return request.args["connector_id"]


@bp.get("")
@_admin
def list_connectors() -> Response:
    if set(request.args) - {"after", "limit"} or any(len(request.args.getlist(key)) != 1 for key in request.args):
        raise ApplicationError("invalid_catalog_query", "目录查询参数无效", 400)
    raw_limit = request.args.get("limit", "8")
    if not re.fullmatch(r"[1-8]", raw_limit):
        raise ApplicationError("invalid_catalog_page", "每页只允许 1–8 条记录", 400)
    page = _service().list_page(request.args.get("after"), int(raw_limit))
    return _response(**asdict(page))


@bp.get("/record")
@_admin
def get_connector() -> Response:
    return _response(connector=asdict(_service().detail(_connector_id())))


def _manage(action: str) -> Response:
    connector_id = _connector_id()
    command = MCPAdminCommand.parse(action, strict_admin_body())
    result = _service().manage(connector_id, command)
    current_app.logger.info("mcp_catalog_admin_action request_id=%s actor=%s connector=%s action=%s epoch=%s",
        get_request_id(), g.account["id"], result.summary.connector_id, action, result.summary.epoch)
    return _response(connector=asdict(result))


@bp.post("/admit")
@_admin
def admit_connector() -> Response:
    return _manage("admit")


@bp.post("/revoke")
@_admin
def revoke_connector() -> Response:
    return _manage("revoke")


@bp.post("/register")
@_admin
def register_connector() -> Response:
    if request.args:
        raise ApplicationError("invalid_catalog_query", "登记请求不接受查询参数", 400)
    command = MCPRegistrationCommand.parse(strict_admin_body(maximum_bytes=16384))
    result = _service().register(command)
    current_app.logger.info("mcp_catalog_admin_action request_id=%s actor=%s connector=%s action=register epoch=%s",
        get_request_id(), g.account["id"], result.summary.connector_id, result.summary.epoch)
    return _response(connector=asdict(result))


@bp.post("/discover")
@_admin
def discover_connector() -> Response:
    connector_id = _connector_id()
    payload = strict_admin_body()
    if type(payload) is not dict or payload.keys() != {"expected_epoch"}:
        raise MCPCatalogError("invalid_catalog_command")
    result = _service().discover(connector_id, payload["expected_epoch"])
    current_app.logger.info("mcp_catalog_admin_action request_id=%s actor=%s connector=%s action=discover epoch=%s",
        get_request_id(), g.account["id"], result.summary.connector_id, result.summary.epoch)
    return _response(connector=asdict(result))
