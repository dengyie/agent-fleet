"""Cookie-admin Fleet identity inspection and exact reviewed revocation."""
from __future__ import annotations

from dataclasses import asdict
from functools import wraps
import json
import re
from typing import Any, Callable

from flask import Blueprint, Response, current_app, g, request

from hub.application.integration_identity_admin import IntegrationIdentityAdminService
from hub.domain.integration_administration import IntegrationIdentityKey, IntegrationRevokeCommand, IntegrationRegisterCommand
from hub.domain.integration_identity import IntegrationIdentityError
from hub.domain.workflow_errors import WorkflowPersistenceError
from hub.http.errors import ApplicationError, error_response, get_request_id
from .integration_admin_boundary import require_account_admin, strict_admin_body

PREFIX = "/api/platform/v1/integrations/fleet"
bp = Blueprint("platform_integration_admin", __name__, url_prefix=PREFIX)


def _response(**payload: Any) -> Response:
    return Response(json.dumps({"ok": True, **payload}, ensure_ascii=False, allow_nan=False,
        separators=(",", ":")), mimetype="application/json")


def _service() -> IntegrationIdentityAdminService:
    service = current_app.extensions["fleet"]["services"].get("integration_identity_admin")
    if service is None:
        raise ApplicationError("integration_store_unavailable", "Fleet 身份管理不可用", 503)
    return service


def _admin(view: Callable[..., Response]) -> Callable[..., Any]:
    @wraps(view)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        try:
            require_account_admin()
            return view(*args, **kwargs)
        except ApplicationError as exc:
            return error_response(exc)
        except (IntegrationIdentityError, WorkflowPersistenceError) as exc:
            status = 503
            if exc.code in {"invalid_identity_command", "invalid_identity_page", "invalid_identity_reference", "invalid_identity_registration"}:
                status = 400
            if exc.code == "precondition_required":
                status = 428
            if exc.code == "integration_not_found":
                status = 404
            if exc.code in {"integration_revision_conflict", "integration_state_changed", "integration_registration_conflict",
                            "integration_token_conflict", "integration_workspace_unavailable", "integration_admission_unavailable"}:
                status = 409
            detail = {400: "身份管理请求格式无效", 404: "集成身份不存在", 409: "身份状态已变化，请重新读取并核对",
                428: "请先读取身份并确认当前有效状态", 503: "Fleet 身份存储暂时不可用"}[status]
            error = ApplicationError(exc.code, detail, status)
            error.__cause__ = exc
            return error_response(error)
    return guarded


@bp.after_app_request
def no_store(response: Response) -> Response:
    if request.path == PREFIX or request.path.startswith(PREFIX + "/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def _selector() -> IntegrationIdentityKey:
    if set(request.args) != {"owner_id", "integration_id"} or any(len(request.args.getlist(key)) != 1 for key in request.args):
        raise ApplicationError("invalid_identity_query", "需要唯一的归属与集成身份标识", 400)
    return IntegrationIdentityKey(request.args["owner_id"], request.args["integration_id"])


@bp.get("")
@_admin
def list_identities() -> Response:
    if set(request.args) - {"after_owner_id", "after_integration_id", "limit"} or any(len(request.args.getlist(key)) != 1 for key in request.args):
        raise ApplicationError("invalid_identity_query", "身份查询参数无效", 400)
    paired = {"after_owner_id", "after_integration_id"}.intersection(request.args)
    if len(paired) == 1:
        raise ApplicationError("invalid_identity_query", "分页必须提供完整身份游标", 400)
    raw_limit = request.args.get("limit", "8")
    if not re.fullmatch(r"[1-8]", raw_limit):
        raise ApplicationError("invalid_identity_page", "每页只允许 1–8 条记录", 400)
    after = IntegrationIdentityKey(request.args["after_owner_id"], request.args["after_integration_id"]) if paired else None
    return _response(**asdict(_service().list_page(after, int(raw_limit))))


@bp.get("/record")
@_admin
def get_identity() -> Response:
    key = _selector()
    return _response(identity=asdict(_service().detail(key.owner_id, key.integration_id)))


@bp.post("/revoke")
@_admin
def revoke_identity() -> Response:
    key = _selector()
    command = IntegrationRevokeCommand.parse(strict_admin_body())
    result = _service().revoke(key.owner_id, key.integration_id, command)
    context = {"request_id": get_request_id(), "actor": g.account["id"], "owner_id": result.owner_id,
        "integration_id": result.integration_id, "action": "revoke", "state": result.state}
    current_app.logger.info("integration_identity_admin_action %s", json.dumps(context, ensure_ascii=True, separators=(",", ":")))
    return _response(revocation=asdict(result))


@bp.post("/register")
@_admin
def register_identity() -> Response:
    if request.args:
        raise ApplicationError("invalid_identity_query", "身份登记不接受查询参数", 400)
    command = IntegrationRegisterCommand.parse(strict_admin_body(maximum_bytes=65536))
    result = _service().register(command)
    context = {"request_id": get_request_id(), "actor": g.account["id"], "owner_id": result.summary.owner_id,
        "integration_id": result.summary.integration_id, "action": "register", "state": result.summary.state}
    current_app.logger.info("integration_identity_admin_action %s", json.dumps(context, ensure_ascii=True, separators=(",", ":")))
    return _response(identity=asdict(result))
