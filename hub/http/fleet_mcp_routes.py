"""Narrow, stateless MCP endpoint for pre-authorized Fleet workflows."""
from __future__ import annotations

import asyncio
from typing import Any

from flask import Blueprint, current_app, jsonify, request
from jsonschema import Draft202012Validator
from werkzeug.exceptions import RequestEntityTooLarge
from hub.auth import mark_machine_auth

from hub.application.integration_identity import IntegrationIdentityStore
from hub.domain.integration_identity import IntegrationIdentity, IntegrationIdentityError
from hub.application.workflow_operator import WorkflowOperatorError
from hub.domain.workflow_contract import parse_input_references
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_json import decode_snapshot
from hub.domain.workflow_operator import WorkflowCreateRequest

bp = Blueprint("fleet_mcp", __name__, url_prefix="/api/platform/mcp")
VERSION = "2026-07-28"
_TOOLS = (
    {"name": "workflow.create_from_template", "description": "Create one pre-authorized Workflow from a fixed template.",
     "inputSchema": {"type": "object", "properties": {
         "template_id": {"type": "string", "minLength": 1, "maxLength": 128},
         "revision": {"type": "integer", "minimum": 1, "maximum": 9007199254740991},
         "workspace_id": {"type": "string", "minLength": 1, "maxLength": 128}, "input_refs": {"type": "array", "maxItems": 16},
         "client_token": {"type": "string", "minLength": 1, "maxLength": 128},
         "expected_policy_revision": {"type": "integer", "minimum": 1, "maximum": 9007199254740991}},
         "required": ["template_id", "revision", "workspace_id", "input_refs", "client_token", "expected_policy_revision"],
         "additionalProperties": False}},
    {"name": "workflow.get", "description": "Read one Workflow created by this integration.",
     "inputSchema": {"type": "object", "properties": {"workflow_id": {"type": "string", "minLength": 1, "maxLength": 128}},
                      "required": ["workflow_id"], "additionalProperties": False}},
    {"name": "workflow.list_artifacts", "description": "List bounded artifact metadata for one Workflow.",
     "inputSchema": {"type": "object", "properties": {"workflow_id": {"type": "string", "minLength": 1, "maxLength": 128},
                                                            "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
                      "required": ["workflow_id"], "additionalProperties": False}},
)
_TOOL_MAP = {tool["name"]: tool for tool in _TOOLS}
_VALIDATORS = {name: Draft202012Validator(tool["inputSchema"]) for name, tool in _TOOL_MAP.items()}


def _error(request_id: object, code: int, message: str, *, status: int = 400, data: object = None):
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return jsonify({"jsonrpc": "2.0", "id": request_id, "error": error}), status


def _identity() -> IntegrationIdentity:
    if not current_app.config.get("PLATFORM_FLEET_MCP_ENABLED"):
        raise IntegrationIdentityError("mcp_disabled")
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer ") or not 32 <= len(authorization[7:]) <= 512:
        raise IntegrationIdentityError("unauthorized")
    identities: IntegrationIdentityStore | None = current_app.extensions.get("fleet", {}).get("repositories", {}).get("integration_identities")
    if not isinstance(identities, IntegrationIdentityStore):
        raise IntegrationIdentityError("integration_store_unavailable")
    identity = identities.authenticate(authorization[7:])
    if identity is not None:
        return identity
    raise IntegrationIdentityError("unauthorized")


def _request_id(value: object) -> str | int | None:
    if type(value) is int and -9007199254740991 <= value <= 9007199254740991:
        return value
    if type(value) is str and 1 <= len(value) <= 128 and "\x00" not in value:
        try:
            if len(value.encode("utf-8")) <= 128:
                return value
        except UnicodeEncodeError:
            return None
    return None


def _headers(body: dict[str, Any], request_id: str | int):
    params = body["params"]
    metadata = params.get("_meta")
    version = metadata.get("io.modelcontextprotocol/protocolVersion") if type(metadata) is dict else None
    if (not isinstance(version, str) or request.headers.get("MCP-Protocol-Version") != version
            or request.headers.get("Mcp-Method") != body["method"]
            or (body["method"] == "tools/call" and request.headers.get("Mcp-Name") != params.get("name"))):
        return _error(request_id, -32020, "header_mismatch")
    if version != VERSION:
        return _error(request_id, -32022, "unsupported_protocol_version", data={"supported": [VERSION], "requested": version})
    if request.mimetype != "application/json":
        return _error(request_id, -32600, "invalid_content_type", status=415)
    if (request.accept_mimetypes["application/json"] <= 0 or request.accept_mimetypes["text/event-stream"] <= 0):
        return _error(request_id, -32600, "invalid_accept", status=406)
    return None


async def _call(identity: IntegrationIdentity, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    identity.require(name)
    service = current_app.extensions.get("fleet", {}).get("services", {}).get("workflows")
    if service is None:
        raise WorkflowOperatorError("workflow_unavailable")
    if name == "workflow.create_from_template":
        value = WorkflowCreateRequest(identity.owner_id, arguments["workspace_id"], arguments["template_id"],
            arguments["revision"], parse_input_references(arguments["input_refs"]),
            identity.namespace_token(arguments["client_token"]), arguments["expected_policy_revision"])
        return await service.create(value, integration=identity)
    limit = arguments.get("limit", identity.max_artifacts)
    if name == "workflow.list_artifacts" and (type(limit) is not int or not 1 <= limit <= identity.max_artifacts):
        raise IntegrationIdentityError("invalid_params")
    result = await service.integration_detail(identity, arguments["workflow_id"], scope=name)
    if name == "workflow.get":
        return result
    artifacts: list[dict[str, Any]] = []
    for step in result["steps"]:
        for artifact in (*step["outputs"], *(item["report"] for item in step["evidence"])):
            artifacts.append(artifact)
            if len(artifacts) == limit:
                return {"workflow_id": arguments["workflow_id"], "artifacts": artifacts}
    return {"workflow_id": arguments["workflow_id"], "artifacts": artifacts}


@bp.post("")
@mark_machine_auth
def mcp():
    request_id: str | int | None = None
    try:
        identity = _identity()
        body = decode_snapshot(request.stream.read(65537))
        request_id = _request_id(body.get("id"))
        if (request_id is None or set(body) - {"jsonrpc", "id", "method", "params"}
                or body.get("jsonrpc") != "2.0" or type(body.get("method")) is not str):
            return _error(None, -32600, "invalid_request")
        params = body.get("params", {})
        if type(params) is not dict:
            return _error(request_id, -32602, "invalid_params")
        body["params"] = params
        rejected = _headers(body, request_id)
        if rejected is not None:
            return rejected
        method = body["method"]
        if method not in {"server/discover", "tools/list", "tools/call"}:
            return _error(request_id, -32601, "method_not_found", status=404)
        allowed_params = {"_meta", "name", "arguments"} if method == "tools/call" else {"_meta"}
        if set(params) - allowed_params:
            return _error(request_id, -32602, "invalid_params")
        if method == "server/discover":
            result = {"resultType": "complete", "supportedVersions": [VERSION], "capabilities": {"tools": {}},
                      "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "agent-fleet", "version": "1"}}}
        elif method == "tools/list":
            result = {"resultType": "complete", "tools": [tool for tool in _TOOLS if tool["name"] in identity.scopes]}
        else:
            name, arguments = params.get("name"), params.get("arguments", {})
            if type(name) is not str or name not in _TOOL_MAP or not _VALIDATORS[name].is_valid(arguments):
                return _error(request_id, -32602, "invalid_params")
            result = {"resultType": "complete", "structuredContent": asyncio.run(_call(identity, name, arguments)),
                      "content": [{"type": "text", "text": "workflow result"}]}
        return jsonify({"jsonrpc": "2.0", "id": request_id, "result": result})
    except IntegrationIdentityError as exc:
        status = 503 if exc.code in {"mcp_disabled", "integration_store_unavailable", "integration_snapshot_invalid"} else 401 if exc.code in {"unauthorized", "integration_revoked"} else 400 if exc.code.startswith("invalid_") else 403
        return _error(request_id, -32602 if status == 400 else -32000, exc.code, status=status)
    except WorkflowValidationError as exc:
        return _error(request_id, -32700, exc.code, status=413 if exc.code == "json_too_large" else 400)
    except WorkflowOperatorError as exc:
        status = 403 if exc.code in {"scope_forbidden", "workflow_forbidden", "budget_forbidden", "reference_forbidden", "template_not_found"} else 409 if exc.code in {"idempotency_conflict", "policy_revision_conflict", "admission_closed"} else 400 if exc.code.startswith("invalid_") else 503
        return _error(request_id, -32000, exc.code, status=status)
    except RequestEntityTooLarge:
        return _error(request_id, -32600, "json_too_large", status=413)

__all__ = ["bp"]
