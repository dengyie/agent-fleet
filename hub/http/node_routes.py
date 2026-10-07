"""Authenticated platform execution-node transport endpoints."""
from __future__ import annotations

import hashlib
import time
import zlib

from flask import Blueprint, current_app, g, jsonify, request

from hub.auth import require_platform_node
from hub.http.errors import ApplicationError, error_response
from hub.infrastructure.platform_db import PlatformRepositoryError
from hub.infrastructure.browser_repository import BrowserRepositoryError
from hub.infrastructure.execution_window_repository import ExecutionWindowRepositoryError
from tools.platform.artifacts import ArtifactError

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


def _browser_repo():
    repository = current_app.extensions.get("fleet", {}).get("repositories", {}).get("browser")
    if repository is None or not current_app.config.get("PLATFORM_BROWSER_ENABLED"):
        raise ApplicationError("browser_disabled", "浏览器能力未启用", 404)
    return repository


def _artifact_store():
    store = current_app.extensions.get("fleet", {}).get("services", {}).get("platform_artifacts")
    if store is None:
        raise ApplicationError("artifact_store", "文件存储不可用", 503)
    return store


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
    except (PlatformRepositoryError, BrowserRepositoryError, ArtifactError) as exc:
        status = {
            "invalid_node_status": 400, "node_not_found": 404,
            "artifact_ticket_invalid": 403, "artifact_ticket_scope": 403,
            "artifact_ticket_expired": 409, "artifact_upload_in_progress": 409,
            "artifact_idempotency_conflict": 409, "artifact_ticket_state": 409,
            "artifact_lease_mismatch": 409, "artifact_lease_expired": 409,
            "artifact_command_expired": 409,
            "invalid_content_type": 415, "invalid_png": 415, "artifact_too_large": 413,
            "session_scope": 409, "frame_scope": 409, "frame_dimensions": 415,
            "invalid_hash": 400, "invalid_size": 400,
        }.get(exc.code, 503)
        detail = {
            "invalid_node_status": "节点状态不合法", "node_not_found": "节点不存在",
            "artifact_ticket_invalid": "上传票据无效", "artifact_ticket_scope": "上传票据作用域不匹配",
            "artifact_ticket_expired": "上传票据已过期", "artifact_upload_in_progress": "上传正在进行",
            "artifact_idempotency_conflict": "上传幂等键冲突", "artifact_ticket_state": "上传票据状态不合法",
            "artifact_lease_mismatch": "命令租约不匹配", "artifact_lease_expired": "命令租约已过期",
            "artifact_command_expired": "命令已过期",
            "invalid_content_type": "文件类型不合法", "invalid_png": "PNG 图像头不合法",
            "artifact_too_large": "文件超出大小限制", "session_scope": "浏览器会话作用域不匹配",
            "frame_scope": "画面事件作用域不匹配", "frame_dimensions": "画面尺寸超出限制",
            "invalid_hash": "文件校验值不合法", "invalid_size": "文件大小不合法",
        }.get(exc.code, "平台存储不可用")
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




def _browser_session_command(command_id, worker_id, action, session_id=None):
    if not isinstance(command_id, str) or not command_id:
        raise ApplicationError("invalid_command", "command_id 不合法", 400)
    if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 128:
        raise ApplicationError("invalid_worker", "worker_id 不合法", 400)
    command = _delivery().repository.get(command_id)
    if not command or command.get("owner_id") != g.platform_node_owner:
        raise ApplicationError("command_not_found", "命令不存在", 404)
    if command.get("target_node") != g.platform_node_id:
        raise ApplicationError("node_mismatch", "命令不属于该节点", 403)
    if command.get("action") != action:
        raise ApplicationError("browser_session_command_required", "浏览器会话命令不匹配", 400)
    now = time.time()
    if command.get("status") not in {"leased", "accepted", "running"}:
        raise ApplicationError("browser_session_command_state", "命令不在可用状态", 409)
    if command.get("lease_owner") != worker_id:
        raise ApplicationError("artifact_lease_mismatch", "命令租约不匹配", 409)
    if not command.get("lease_until") or float(command["lease_until"]) <= now:
        raise ApplicationError("artifact_lease_expired", "命令租约已过期", 409)
    if not command.get("expires_at") or float(command["expires_at"]) <= now:
        raise ApplicationError("artifact_command_expired", "命令已过期", 409)
    if session_id is not None:
        arguments = command.get("arguments") or {}
        if arguments.get("session_id") != session_id:
            raise ApplicationError("browser_session_scope", "浏览器会话作用域不匹配", 409)
    return command


def _png_dimensions(raw: bytes) -> tuple[int, int]:
    if (len(raw) < 33 or raw[8:12] != b"\x00\x00\x00\r"
            or raw[12:16] != b"IHDR"
            or (zlib.crc32(raw[12:29]) & 0xffffffff) != int.from_bytes(raw[29:33], "big")):
        raise ApplicationError("invalid_png", "PNG 图像头不合法", 415)
    width = int.from_bytes(raw[16:20], "big")
    height = int.from_bytes(raw[20:24], "big")
    if not 1 <= width <= 4096 or not 1 <= height <= 4096 or width * height > 16_777_216:
        raise ApplicationError("invalid_png", "PNG 图像尺寸超出限制", 415)
    bit_depth, color_type, compression, filtering, interlace = raw[24:29]
    channels_by_type = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    valid_depths = {
        0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8},
        4: {8, 16}, 6: {8, 16},
    }
    channels = channels_by_type.get(color_type)
    if (channels is None or bit_depth not in valid_depths[color_type]
            or compression != 0 or filtering != 0 or interlace not in (0, 1)):
        raise ApplicationError("invalid_png", "PNG 图像头不合法", 415)

    def pass_size(length: int, start: int, step: int) -> int:
        return max(0, (length - start + step - 1) // step)

    if interlace:
        passes = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8),
                  (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2),
                  (0, 1, 1, 2))
    else:
        passes = ((0, 0, 1, 1),)
    expected_decoded = 0
    for x_start, y_start, x_step, y_step in passes:
        pass_width = pass_size(width, x_start, x_step)
        pass_height = pass_size(height, y_start, y_step)
        if pass_width and pass_height:
            row_bytes = (pass_width * channels * bit_depth + 7) // 8
            expected_decoded += (row_bytes + 1) * pass_height
    offset = 8
    seen_header = False
    seen_data = False
    seen_end = False
    idat_parts = []
    while offset + 12 <= len(raw):
        length = int.from_bytes(raw[offset:offset + 4], "big")
        end = offset + 12 + length
        if end > len(raw):
            break
        kind = raw[offset + 4:offset + 8]
        payload = raw[offset + 8:offset + 8 + length]
        checksum = int.from_bytes(raw[offset + 8 + length:end], "big")
        if (zlib.crc32(kind + payload) & 0xffffffff) != checksum:
            raise ApplicationError("invalid_png", "PNG 图像数据校验失败", 415)
        if offset == 8:
            if kind != b"IHDR" or length != 13:
                raise ApplicationError("invalid_png", "PNG 图像头不合法", 415)
            seen_header = True
        elif kind == b"IDAT":
            seen_data = True
            idat_parts.append(payload)
        elif kind == b"IEND":
            if length != 0 or end != len(raw):
                raise ApplicationError("invalid_png", "PNG 图像结束块不合法", 415)
            seen_end = True
            break
        offset = end
    if not (seen_header and seen_data and seen_end):
        raise ApplicationError("invalid_png", "PNG 图像数据不完整", 415)
    # Validate the bounded zlib stream without retaining decoded pixel rows.
    try:
        decoder = zlib.decompressobj()
        decoded_size = 0
        for part in idat_parts:
            pending = part
            while pending:
                decoded = decoder.decompress(
                    pending, min(64 * 1024, expected_decoded - decoded_size + 1),
                )
                decoded_size += len(decoded)
                if (decoded_size > expected_decoded or decoder.unused_data
                        or (not decoded and decoder.unconsumed_tail == pending)):
                    raise ValueError("invalid PNG IDAT stream")
                pending = decoder.unconsumed_tail
                if decoder.eof and pending:
                    raise ValueError("trailing PNG IDAT data")
        if (not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
                or decoded_size != expected_decoded):
            raise ValueError("incomplete PNG IDAT stream")
    except (ValueError, zlib.error):
        raise ApplicationError("invalid_png", "PNG 图像数据压缩流不合法", 415) from None
    return width, height


@bp.post("/browser-sessions")
@require_platform_node
def register_browser_session():
    def _register():
        _browser_repo()
        body = _body()
        session_id = body.get("session_id")
        backend = body.get("backend", "cdp_local")
        if not isinstance(session_id, str) or not 16 <= len(session_id) <= 128:
            raise ApplicationError("invalid_session", "session_id 不合法", 400)
        if backend != "cdp_local":
            raise ApplicationError("invalid_backend", "浏览器 backend 不合法", 400)
        command = _browser_session_command(
            body.get("command_id"), body.get("worker_id"), "tool.browser.open")
        workspace_id = command.get("resource_id")
        run_id = command.get("run_id")
        if not isinstance(workspace_id, str) or not isinstance(run_id, str):
            raise ApplicationError("browser_session_scope", "会话作用域不可用", 409)
        repo = _browser_repo()
        existing = repo.get_session(g.platform_node_owner, session_id)
        if existing is not None:
            if (existing["workspace_id"], existing["run_id"], existing["node_id"], existing["backend"]) != (
                    workspace_id, run_id, g.platform_node_id, backend):
                raise ApplicationError("browser_session_scope", "会话作用域不匹配", 409)
            if existing["state"] != "open":
                raise ApplicationError("browser_session_terminal", "浏览器会话已结束", 409)
            return jsonify({"ok": True, "session": existing})
        session = repo.create_session(
            g.platform_node_owner, workspace_id=workspace_id, run_id=run_id,
            node_id=g.platform_node_id, profile_id="ephemeral", backend=backend,
            session_id=session_id,
        )
        return jsonify({"ok": True, "session": session})
    return _call(_register)


@bp.post("/browser-sessions/<session_id>/close")
@require_platform_node
def close_browser_session(session_id):
    def _close():
        _browser_repo()
        body = _body()
        command = _browser_session_command(
            body.get("command_id"), body.get("worker_id"), "tool.browser.close", session_id)
        session = _browser_repo().get_session(g.platform_node_owner, session_id)
        if session is None:
            raise ApplicationError("session_not_found", "浏览器会话不存在", 404)
        if session["node_id"] != g.platform_node_id or session["run_id"] != command.get("run_id"):
            raise ApplicationError("browser_session_scope", "会话作用域不匹配", 409)
        closed = _browser_repo().close_session(g.platform_node_owner, session_id)
        return jsonify({"ok": True, "session": closed})
    return _call(_close)


@bp.post("/browser-artifact-tickets")
@require_platform_node
def issue_browser_artifact_ticket():
    def _issue():
        body = _body()
        command_id = body.get("command_id")
        idempotency_key = body.get("idempotency_key")
        worker_id = body.get("worker_id")
        if not isinstance(command_id, str) or not command_id:
            raise ApplicationError("invalid_command", "command_id 不合法", 400)
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ApplicationError("invalid_idempotency_key", "幂等键不合法", 400)
        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 128:
            raise ApplicationError("invalid_worker", "worker_id 不合法", 400)
        command = _delivery().repository.get(command_id)
        if not command or command.get("owner_id") != g.platform_node_owner:
            raise ApplicationError("command_not_found", "命令不存在", 404)
        if command.get("target_node") != g.platform_node_id:
            raise ApplicationError("node_mismatch", "命令不属于该节点", 403)
        if command.get("action") != "tool.browser.screenshot":
            raise ApplicationError("artifact_command_required", "仅允许 screenshot 命令上传", 400)
        now = __import__("time").time()
        if command.get("status") not in {"leased", "accepted", "running"}:
            raise ApplicationError("artifact_command_state", "命令不在可上传状态", 409)
        if command.get("lease_owner") != worker_id:
            raise ApplicationError("artifact_lease_mismatch", "命令租约不匹配", 409)
        if not command.get("lease_until") or float(command["lease_until"]) <= now:
            raise ApplicationError("artifact_lease_expired", "命令租约已过期", 409)
        if not command.get("expires_at") or float(command["expires_at"]) <= now:
            raise ApplicationError("artifact_command_expired", "命令已过期", 409)
        run_id = command.get("run_id")
        arguments = command.get("arguments") or {}
        session_id = arguments.get("session_id") if isinstance(arguments, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise ApplicationError("artifact_scope_unavailable", "浏览器会话作用域不可用", 409)
        workspace_id = command.get("resource_id")
        if not isinstance(run_id, str) or not isinstance(workspace_id, str):
            raise ApplicationError("artifact_scope_unavailable", "命令作用域不可用", 409)
        expires_at = min(float(command.get("expires_at") or 0), __import__("time").time() + 300.0)
        ticket = _browser_repo().issue_artifact_ticket(
            g.platform_node_owner, workspace_id=workspace_id, run_id=run_id,
            node_id=g.platform_node_id, command_id=command_id,
            expires_at=expires_at, idempotency_key=idempotency_key,
            session_id=session_id,
            capture_frame=(current_app.extensions.get("fleet", {}).get(
                "repositories", {}).get("execution_windows") is not None),
        )
        if ticket.get("state") == "consumed" and ticket.get("artifact_id"):
            artifact = _artifact_store().get(
                g.platform_node_owner, workspace_id, ticket["artifact_id"])
            if artifact is None:
                raise ArtifactError("artifact_not_found")
            ticket = dict(ticket)
            ticket["artifact"] = artifact
        return jsonify({"ok": True, "ticket": ticket})
    return _call(_issue)


@bp.post("/browser-artifact-tickets/<ticket_id>/content")
@require_platform_node
def upload_browser_artifact(ticket_id):
    def _upload():
        token = request.headers.get("X-Platform-Artifact-Upload-Token", "")
        command_id = request.headers.get("X-Platform-Command-ID", "")
        worker_id = request.headers.get("X-Platform-Worker-ID", "")
        idempotency_key = request.headers.get("X-Platform-Artifact-Idempotency-Key", "")
        if not token or not command_id or not worker_id or not idempotency_key:
            raise ApplicationError("invalid_upload_headers", "上传凭据不完整", 400)
        content_type = request.headers.get("Content-Type", "")
        max_bytes = 256 * 1024
        content_length = request.content_length
        if content_length is not None and content_length > max_bytes:
            raise ApplicationError("artifact_too_large", "文件超出大小限制", 413)
        raw = request.stream.read(max_bytes + 1)
        if not isinstance(raw, bytes):
            raise ApplicationError("invalid_upload", "上传内容不合法", 400)
        if content_type != "image/png" or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ApplicationError("invalid_content_type", "文件类型不合法", 415)
        if len(raw) > max_bytes:
            raise ApplicationError("artifact_too_large", "文件超出大小限制", 413)
        repo = _browser_repo()
        command = _delivery().repository.get(command_id)
        if not command or command.get("owner_id") != g.platform_node_owner:
            raise ApplicationError("command_not_found", "命令不存在", 404)
        if command.get("target_node") != g.platform_node_id:
            raise ApplicationError("node_mismatch", "命令不属于该节点", 403)
        if command.get("action") != "tool.browser.screenshot":
            raise ApplicationError("artifact_command_required", "仅允许 screenshot 命令上传", 400)
        ticket_scope = repo.validate_artifact_upload_scope(
            ticket_id, g.platform_node_owner,
            workspace_id=command.get("resource_id"), run_id=command.get("run_id"),
            node_id=g.platform_node_id, command_id=command_id,
            idempotency_key=idempotency_key,
            session_id=(command.get("arguments") or {}).get("session_id"),
        )
        dimensions = _png_dimensions(raw) if ticket_scope["window_id"] else None
        if ticket_scope["expires_at"] <= time.time():
            raise BrowserRepositoryError("artifact_ticket_expired")
        if ticket_scope["state"] == "consumed":
            ticket = repo.begin_artifact_upload(
                ticket_id, token, node_id=g.platform_node_id, command_id=command_id,
                idempotency_key=idempotency_key,
            )
            artifact_id = ticket.get("artifact_id")
            if not artifact_id:
                raise ApplicationError("artifact_ticket_state", "上传票据状态不合法", 409)
            artifact = _artifact_store().get(
                g.platform_node_owner, ticket["workspace_id"], artifact_id)
            if artifact is None:
                raise ArtifactError("artifact_not_found")
            return jsonify({"ok": True, "artifact": artifact})
        _browser_session_command(command_id, worker_id, "tool.browser.screenshot")
        ticket = repo.begin_artifact_upload(
            ticket_id, token, node_id=g.platform_node_id, command_id=command_id,
            idempotency_key=idempotency_key,
        )
        if ticket.get("state") == "consumed":
            artifact_id = ticket.get("artifact_id")
            if not artifact_id:
                raise ApplicationError("artifact_ticket_state", "上传票据状态不合法", 409)
            return jsonify({"ok": True, "artifact": _artifact_store().get(
                g.platform_node_owner, ticket["workspace_id"], artifact_id)})
        digest = hashlib.sha256(raw).hexdigest()
        artifact = None
        try:
            artifact = _artifact_store().put_bytes(
                ticket["owner_id"], ticket["workspace_id"], raw,
                name="screenshot.png", content_type="image/png",
            )
            if artifact["sha256"] != digest or artifact["size"] != len(raw):
                raise ArtifactError("artifact_corrupt")
            def publish_frame(connection, consumed_ticket, frame_dimensions):
                execution_windows = current_app.extensions.get("fleet", {}).get(
                    "repositories", {}).get("execution_windows")
                if execution_windows is None:
                    raise BrowserRepositoryError("frame_scope")
                try:
                    execution_windows.append_browser_frame_event(
                        connection, owner_id=consumed_ticket["owner_id"],
                        ticket_id=consumed_ticket["ticket_id"],
                        artifact_id=consumed_ticket["artifact_id"],
                        sha256=consumed_ticket["sha256"], width=frame_dimensions[0],
                        height=frame_dimensions[1],
                    )
                except ExecutionWindowRepositoryError as exc:
                    raise BrowserRepositoryError(exc.code) from exc

            repo.complete_artifact_upload(
                ticket_id, sha256=digest, size=len(raw),
                artifact_id=artifact["artifact_id"],
                frame_publisher=publish_frame if dimensions is not None else None,
                frame_dimensions=dimensions,
            )
            return jsonify({"ok": True, "artifact": artifact})
        except Exception:
            repo.reset_artifact_upload(ticket_id)
            if artifact is not None:
                _artifact_store().delete(ticket["owner_id"], ticket["workspace_id"], artifact["artifact_id"])
            raise
    return _call(_upload)


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
