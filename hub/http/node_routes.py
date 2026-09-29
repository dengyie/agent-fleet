"""Authenticated platform execution-node transport endpoints."""
from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, request

from hub.auth import require_platform_node
from hub.http.errors import ApplicationError, error_response
from hub.infrastructure.platform_db import PlatformRepositoryError

bp = Blueprint("platform_nodes", __name__, url_prefix="/api/platform/v1/nodes")


def _body():
    data = request.get_json(silent=True)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ApplicationError("invalid_json", "请求体必须是 JSON 对象", 400)
    return data


def _repo():
    repo = current_app.extensions.get("fleet", {}).get("platform_repository")
    if repo is None:
        raise ApplicationError("platform_store", "平台存储不可用", 503)
    return repo


def _delivery():
    service = current_app.extensions.get("fleet", {}).get("services", {}).get("platform_delivery")
    if service is None:
        raise ApplicationError("platform_store", "平台投递不可用", 503)
    return service


def _health():
    service = current_app.extensions.get("fleet", {}).get("services", {}).get("service_health")
    if service is None:
        raise ApplicationError("service_monitoring_unavailable", "服务监控不可用", 503)
    return service


def _bounded_number(value, name, *, default, minimum, maximum, integer=False):
    if value is None:
        return default
    try:
        parsed = int(value) if integer else float(value)
    except (TypeError, ValueError):
        raise ApplicationError("invalid_" + name, f"{name} 不合法", 400) from None
    if parsed < minimum or parsed > maximum:
        raise ApplicationError("invalid_" + name, f"{name} 不合法", 400)
    return parsed


def _call(fn):
    try:
        return fn()
    except ApplicationError as exc:
        return error_response(exc)
    except PlatformRepositoryError as exc:
        status = {"invalid_node_status": 400, "node_not_found": 404}.get(exc.code, 503)
        detail = {"invalid_node_status": "节点状态不合法", "node_not_found": "节点不存在"}.get(exc.code, "平台存储不可用")
        return error_response(ApplicationError(exc.code, detail, status))


@bp.post("/poll")
@require_platform_node
def poll():
    def _poll():
        body = _body()
        worker_id = body.get("worker_id") or g.platform_node_id
        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 128:
            raise ApplicationError("invalid_worker", "worker_id 不合法", 400)
        limit = _bounded_number(body.get("limit"), "limit", default=20, minimum=1, maximum=100, integer=True)
        lease_s = _bounded_number(body.get("lease_s"), "lease_s", default=60.0, minimum=1.0, maximum=3600.0)
        rows = _delivery().poll(
            g.platform_node_id, worker_id, owner_id=g.platform_node_owner,
            limit=limit, lease_s=lease_s,
        )
        safe = []
        for row in rows:
            safe.append({key: row.get(key) for key in (
                "command_id", "target_node", "action", "resource_id",
                "arguments", "retry_class", "expires_at", "args_hash",
                "run_id", "grant_id", "signature",
            )})
        return jsonify({"ok": True, "node_id": g.platform_node_id, "commands": safe})
    return _call(_poll)


@bp.post("/receipts")
@require_platform_node
def receipt():
    def _receipt():
        body = _body()
        command_id = body.get("command_id")
        status = body.get("status")
        if not isinstance(command_id, str) or not command_id or not isinstance(status, str):
            raise ApplicationError("invalid_receipt", "回执字段不完整", 400)
        result = body.get("result")
        if result is not None and not isinstance(result, dict):
            raise ApplicationError("invalid_receipt", "result 必须是对象", 400)
        worker_id = body.get("worker_id")
        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 128:
            raise ApplicationError("invalid_worker", "worker_id 不合法", 400)
        row = _delivery().receipt(
            command_id, status, result=result or {}, worker_id=worker_id,
            owner_id=g.platform_node_owner, node_id=g.platform_node_id,
        )
        return jsonify({"ok": True, "receipt": {
            "command_id": row["command_id"], "status": row["status"],
            "result": row.get("result"),
        }})
    return _call(_receipt)


@bp.post("/heartbeat")
@require_platform_node
def heartbeat():
    def _heartbeat():
        body = _body()
        status = body.get("status", "online")
        row = _repo().record_node_heartbeat(
            g.platform_node_owner, g.platform_node_id, status=status,
        )
        return jsonify({"ok": True, "node": {
            "node_id": row["node_id"], "status": row["status"],
            "last_seen_at": row["last_seen_at"],
        }})
    return _call(_heartbeat)


@bp.post("/health-evidence")
@require_platform_node
def health_evidence():
    def _ingest():
        body = _body()
        return jsonify(_health().node_evidence(g.platform_node_owner, g.platform_node_id, body))
    return _call(_ingest)


__all__ = ["bp"]
