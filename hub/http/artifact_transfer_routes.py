"""Node-authenticated, command-bound V2 artifact transfer endpoints."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from flask import Blueprint, Response, current_app, g, jsonify, request
from werkzeug.exceptions import ClientDisconnected, RequestEntityTooLarge

from hub.auth import require_platform_node
from hub.application.artifact_transfer_service import ArtifactTransferService
from hub.domain.artifact_transfer import ArtifactTransferError
from hub.http._common import json_object
from hub.http.errors import ApplicationError, error_response


bp = Blueprint("platform_artifact_transfers", __name__, url_prefix="/api/platform/v1/node/artifact-transfers")
MAX_CHUNK_BYTES = 1024 * 1024
HttpResult = Response | tuple[Response, int]
_ERRORS = {
    "transfer_not_found": (404, "传输不存在"),
    "command_not_found": (404, "命令不存在"),
    "invalid_size": (400, "文件大小不合法"),
    "invalid_hash": (400, "SHA-256 不合法"),
    "invalid_offset": (400, "分片偏移不合法"),
    "invalid_content_length": (400, "Content-Length 不匹配"),
    "artifact_too_large": (413, "文件超出传输上限"),
    "part_too_large": (413, "分片超出大小上限"),
    "invalid_part_size": (400, "分片长度不合法"),
    "part_hash_mismatch": (400, "分片校验失败"),
    "artifact_hash_mismatch": (409, "整文件校验失败"),
    "part_conflict": (409, "相同偏移已有不同分片"),
    "part_overlap": (409, "分片互相重叠"),
    "transfer_conflict": (409, "传输幂等内容冲突"),
    "transfer_state_conflict": (409, "传输状态不允许此操作"),
    "transfer_concurrency_limit": (429, "并发传输数已达上限"),
    "workspace_quota_exceeded": (413, "工作区产物配额不足"),
    "workflow_artifact_budget_exceeded": (413, "流程产物预算不足"),
    "reservation_expired": (409, "传输预留已过期"),
    "artifact_command_required": (409, "命令未授权导出产物"),
    "artifact_command_unavailable": (409, "原命令租约不可用"),
    "artifact_output_forbidden": (409, "命令输出不在固定合同中"),
    "artifact_output_already_bound": (409, "输出已绑定其他产物"),
    "artifact_output_too_large": (413, "产物超出步骤输出限制"),
    "transfer_reconciliation_required": (409, "传输需要恢复核对"),
    "transfer_corrupt": (409, "传输数据损坏"),
    "unsafe_staging_path": (409, "暂存路径不安全"),
    "artifact_corrupt": (409, "已发布文件校验失败"),
    "artifact_id_conflict": (409, "固定产物标识冲突"),
    "invalid_artifact_name": (400, "文件名不合法"),
    "invalid_content_type": (400, "文件类型不合法"),
    "too_many_parts": (413, "分片数量超出上限"),
    "part_out_of_bounds": (400, "分片超出文件边界"),
    "missing_or_overlapping_part": (409, "分片缺失或重叠"),
    "unexpected_parts": (409, "零字节文件不得包含分片"),
    "transfer_store": (503, "传输存储不可用"),
    "transfer_scan_limit": (503, "传输扫描超出上限"),
    "transfer_busy": (429, "该传输正在处理其他请求"),
    "transfer_schema_unknown": (503, "传输存储版本不兼容"),
    "capability_unavailable": (503, "当前平台未验证文件传输能力"),
    "artifact_scope_unavailable": (409, "命令作用域不可用"),
    "export_credential_revoked": (409, "原节点认证凭据已撤销"),
    "publication_failed": (503, "产物发布失败"),
    "staging_write_failed": (503, "分片暂存失败"),
    "staging_unavailable": (503, "分片暂存不可用"),
}


def _service() -> ArtifactTransferService:
    service = current_app.extensions.get("fleet", {}).get("services", {}).get("artifact_transfers")
    if service is None or not current_app.config.get("PLATFORM_ARTIFACT_TRANSFER_ENABLED"):
        raise ApplicationError("artifact_transfer_disabled", "产物传输未启用", 404)
    return service


def _headers() -> tuple[str, str]:
    command_id = request.headers.get("X-Platform-Command-ID", "")
    worker_id = request.headers.get("X-Platform-Worker-ID", "")
    if (not command_id or len(command_id) > 128 or not worker_id or len(worker_id) > 128
            or any(ord(char) < 0x20 or ord(char) == 0x7f for char in command_id + worker_id)):
        raise ApplicationError("invalid_transfer_headers", "传输命令头不合法", 400)
    return command_id, worker_id


def _call(operation: Callable[[], HttpResult]) -> HttpResult:
    try:
        return operation()
    except ApplicationError as exc:
        return error_response(exc)
    except ClientDisconnected:
        return error_response(ApplicationError("invalid_content_length", "请求正文未完整接收", 400))
    except RequestEntityTooLarge:
        return error_response(ApplicationError("part_too_large", "请求超出大小上限", 413))
    except ArtifactTransferError as exc:
        status, detail = _ERRORS.get(exc.code, (503, "产物传输不可用"))
        return error_response(ApplicationError(exc.code, detail, status))


@bp.post("")
@require_platform_node
def reserve() -> HttpResult:
    def operation() -> HttpResult:
        body = json_object()
        required = {"command_id", "output_key", "name", "size", "sha256"}
        if set(body) != required or any(type(body[key]) is not str or not body[key] for key in required - {"size"}):
            raise ApplicationError("invalid_transfer_request", "预留字段不合法", 400)
        command_id, worker_id = _headers()
        if body["command_id"] != command_id:
            raise ApplicationError("command_header_mismatch", "命令头与请求不匹配", 400)
        if type(body["size"]) is not int:
            raise ApplicationError("invalid_size", "文件大小不合法", 400)
        manifest = _service().reserve(command_id=command_id, owner_id=g.platform_node_owner,
            node_id=g.platform_node_id, worker_id=worker_id, output_key=body["output_key"],
            name=body["name"], size=body["size"], sha256=body["sha256"])
        return jsonify({"ok": True, "transfer": {
            "transfer_id": manifest.transfer_id, "artifact_id": manifest.artifact_id,
            "chunk_size": manifest.chunk_size, "expires_at": datetime.fromtimestamp(manifest.expires_at, timezone.utc).isoformat(),
        }}), 201
    return _call(operation)


@bp.get("/<transfer_id>")
@require_platform_node
def status(transfer_id: str) -> HttpResult:
    def operation() -> HttpResult:
        command_id, worker_id = _headers()
        result = _service().status(transfer_id, command_id=command_id,
            owner_id=g.platform_node_owner, node_id=g.platform_node_id, worker_id=worker_id,
            credential_fingerprint=g.platform_node_credential_fingerprint)
        result["expires_at"] = datetime.fromtimestamp(result["expires_at"], timezone.utc).isoformat()
        return jsonify({"ok": True, "transfer": result})
    return _call(operation)


@bp.put("/<transfer_id>/parts/<int:offset>")
@require_platform_node
def put_part(transfer_id: str, offset: int) -> HttpResult:
    request.max_content_length = MAX_CHUNK_BYTES
    def operation() -> HttpResult:
        command_id, worker_id = _headers()
        service = _service()
        length = request.content_length
        if type(length) is not int or length <= 0:
            raise ApplicationError("invalid_content_length", "Content-Length 不合法", 400)
        if length > MAX_CHUNK_BYTES:
            raise ApplicationError("part_too_large", "分片超出大小上限", 413)
        digest = request.headers.get("X-Content-SHA256", "")
        deadline, expiry_code = service.prepare_part(transfer_id, owner_id=g.platform_node_owner,
            node_id=g.platform_node_id, worker_id=worker_id, command_id=command_id,
            offset=offset, length=length, declared_hash=digest)
        payload = bytearray(length)
        cursor = 0
        while cursor < length:
            if float(service.clock()) >= deadline:
                raise ApplicationError(expiry_code, "传输读取已过期", 409)
            chunk = request.stream.read(min(64 * 1024, length - cursor))
            if float(service.clock()) >= deadline:
                raise ApplicationError(expiry_code, "传输读取已过期", 409)
            if not chunk:
                raise ApplicationError("invalid_content_length", "请求正文长度不匹配", 400)
            payload[cursor:cursor + len(chunk)] = chunk
            cursor += len(chunk)
        receipt = service.put_part(transfer_id, owner_id=g.platform_node_owner, command_id=command_id,
            node_id=g.platform_node_id, worker_id=worker_id, offset=offset,
            payload=bytes(payload), declared_hash=digest,
            content_length=length)
        return jsonify({"ok": True, "part": receipt})
    return _call(operation)


@bp.post("/<transfer_id>/commit")
@require_platform_node
def commit(transfer_id: str) -> HttpResult:
    def operation() -> HttpResult:
        body = json_object()
        if set(body) != {"command_id", "size", "sha256"}:
            raise ApplicationError("invalid_transfer_request", "提交字段不合法", 400)
        command_id, worker_id = _headers()
        if body["command_id"] != command_id or type(body["size"]) is not int:
            raise ApplicationError("invalid_transfer_request", "提交身份或大小不合法", 400)
        artifact = _service().commit(transfer_id, owner_id=g.platform_node_owner,
            node_id=g.platform_node_id, worker_id=worker_id, command_id=command_id,
            size=body["size"], sha256=body["sha256"], credential_fingerprint=g.platform_node_credential_fingerprint)
        return jsonify({"ok": True, "artifact": artifact})
    return _call(operation)


__all__ = ["bp"]
