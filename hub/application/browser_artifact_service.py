"""浏览器截图产物上传的应用层编排。

原先整段逻辑内联在 ``hub/http/node_routes.upload_browser_artifact``：票据作用域
校验、命令租约校验、幂等重放、写入 artifact store、发布执行窗口画面帧、失败
回滚全部混在 Flask 视图里。这里把它收敛为可单测的服务，HTTP 适配层只负责读
头部/有界读取请求体并把结果序列化。

异常契约（全部由 node_routes._call 映射为 bounded JSON）：

* ``BrowserArtifactError``（``ApplicationError`` 子类）——本服务自身的校验失败，
  ``code``/``status`` 显式给出；
* ``BrowserRepositoryError`` —— 票据仓储拒绝（过期、作用域、幂等冲突、画面作用域）；
* ``ArtifactError`` —— 文件存储失败或产物缺失/损坏。
"""
from __future__ import annotations

import hashlib
import time
import zlib

from hub.application.task_service import ApplicationError
from hub.infrastructure.browser_repository import BrowserRepositoryError
from hub.infrastructure.execution_window_repository import ExecutionWindowRepositoryError
from hub.platform.artifacts import ArtifactError

__all__ = [
    "MAX_SCREENSHOT_BYTES",
    "SCREENSHOT_ACTION",
    "BrowserArtifactError",
    "BrowserArtifactService",
    "check_session_command",
    "png_dimensions",
    "validate_screenshot_payload",
]

MAX_SCREENSHOT_BYTES = 256 * 1024
SCREENSHOT_ACTION = "tool.browser.screenshot"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_ACTIVE_COMMAND_STATES = frozenset({"leased", "accepted", "running"})


class BrowserArtifactError(ApplicationError):
    """浏览器产物上传的显式业务错误（带稳定 code 与 HTTP status）。"""


def validate_screenshot_payload(content_type: str, raw: bytes) -> None:
    """截图负载的传输级校验：类型/PNG 魔数 → 415，超限 → 413。"""
    if content_type != "image/png" or not raw.startswith(_PNG_MAGIC):
        raise BrowserArtifactError("invalid_content_type", "文件类型不合法", 415)
    if len(raw) > MAX_SCREENSHOT_BYTES:
        raise BrowserArtifactError("artifact_too_large", "文件超出大小限制", 413)


def check_session_command(command, *, owner_id, node_id, worker_id, action,
                          session_id=None, now=None):
    """校验浏览器会话命令归属、动作、状态与租约；返回命令行。"""
    if not command or command.get("owner_id") != owner_id:
        raise BrowserArtifactError("command_not_found", "命令不存在", 404)
    if command.get("target_node") != node_id:
        raise BrowserArtifactError("node_mismatch", "命令不属于该节点", 403)
    if command.get("action") != action:
        raise BrowserArtifactError(
            "browser_session_command_required", "浏览器会话命令不匹配", 400)
    now = time.time() if now is None else now
    if command.get("status") not in _ACTIVE_COMMAND_STATES:
        raise BrowserArtifactError("browser_session_command_state", "命令不在可用状态", 409)
    if command.get("lease_owner") != worker_id:
        raise BrowserArtifactError("artifact_lease_mismatch", "命令租约不匹配", 409)
    if not command.get("lease_until") or float(command["lease_until"]) <= now:
        raise BrowserArtifactError("artifact_lease_expired", "命令租约已过期", 409)
    if not command.get("expires_at") or float(command["expires_at"]) <= now:
        raise BrowserArtifactError("artifact_command_expired", "命令已过期", 409)
    if session_id is not None:
        arguments = command.get("arguments") or {}
        if arguments.get("session_id") != session_id:
            raise BrowserArtifactError("browser_session_scope", "浏览器会话作用域不匹配", 409)
    return command


def png_dimensions(raw: bytes) -> tuple[int, int]:
    if (len(raw) < 33 or raw[8:12] != b"\x00\x00\x00\r"
            or raw[12:16] != b"IHDR"
            or (zlib.crc32(raw[12:29]) & 0xffffffff) != int.from_bytes(raw[29:33], "big")):
        raise BrowserArtifactError("invalid_png", "PNG 图像头不合法", 415)
    width = int.from_bytes(raw[16:20], "big")
    height = int.from_bytes(raw[20:24], "big")
    if not 1 <= width <= 4096 or not 1 <= height <= 4096 or width * height > 16_777_216:
        raise BrowserArtifactError("invalid_png", "PNG 图像尺寸超出限制", 415)
    bit_depth, color_type, compression, filtering, interlace = raw[24:29]
    channels_by_type = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    valid_depths = {
        0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8},
        4: {8, 16}, 6: {8, 16},
    }
    channels = channels_by_type.get(color_type)
    if (channels is None or bit_depth not in valid_depths[color_type]
            or compression != 0 or filtering != 0 or interlace not in (0, 1)):
        raise BrowserArtifactError("invalid_png", "PNG 图像头不合法", 415)

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
            raise BrowserArtifactError("invalid_png", "PNG 图像数据校验失败", 415)
        if offset == 8:
            if kind != b"IHDR" or length != 13:
                raise BrowserArtifactError("invalid_png", "PNG 图像头不合法", 415)
            seen_header = True
        elif kind == b"IDAT":
            seen_data = True
            idat_parts.append(payload)
        elif kind == b"IEND":
            if length != 0 or end != len(raw):
                raise BrowserArtifactError("invalid_png", "PNG 图像结束块不合法", 415)
            seen_end = True
            break
        offset = end
    if not (seen_header and seen_data and seen_end):
        raise BrowserArtifactError("invalid_png", "PNG 图像数据不完整", 415)
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
        raise BrowserArtifactError("invalid_png", "PNG 图像数据压缩流不合法", 415) from None
    return width, height


class BrowserArtifactService:
    """截图上传编排：票据作用域 → 命令租约 → 幂等重放或写入 + 完成票据。"""

    def __init__(self, *, browser_repository, command_repository, artifact_store,
                 execution_windows=None):
        self.browser_repository = browser_repository
        self.command_repository = command_repository
        self.artifact_store = artifact_store
        self.execution_windows = execution_windows

    def upload_screenshot(self, ticket_id, *, owner_id, node_id, token, command_id,
                          worker_id, idempotency_key, raw: bytes):
        """``raw`` 必须已通过 ``validate_screenshot_payload``；返回 artifact 行。"""
        repo = self.browser_repository
        command = self.command_repository.get(command_id)
        if not command or command.get("owner_id") != owner_id:
            raise BrowserArtifactError("command_not_found", "命令不存在", 404)
        if command.get("target_node") != node_id:
            raise BrowserArtifactError("node_mismatch", "命令不属于该节点", 403)
        if command.get("action") != SCREENSHOT_ACTION:
            raise BrowserArtifactError(
                "artifact_command_required", "仅允许 screenshot 命令上传", 400)
        ticket_scope = repo.validate_artifact_upload_scope(
            ticket_id, owner_id,
            workspace_id=command.get("resource_id"), run_id=command.get("run_id"),
            node_id=node_id, command_id=command_id,
            idempotency_key=idempotency_key,
            session_id=(command.get("arguments") or {}).get("session_id"),
        )
        dimensions = png_dimensions(raw) if ticket_scope["window_id"] else None
        if ticket_scope["expires_at"] <= time.time():
            raise BrowserRepositoryError("artifact_ticket_expired")
        if ticket_scope["state"] == "consumed":
            # 幂等重放：已完成的票据直接返回已有产物，不再校验租约。
            ticket = repo.begin_artifact_upload(
                ticket_id, token, node_id=node_id, command_id=command_id,
                idempotency_key=idempotency_key,
            )
            artifact_id = ticket.get("artifact_id")
            if not artifact_id:
                raise BrowserArtifactError("artifact_ticket_state", "上传票据状态不合法", 409)
            artifact = self.artifact_store.get(owner_id, ticket["workspace_id"], artifact_id)
            if artifact is None:
                raise ArtifactError("artifact_not_found")
            return artifact
        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 128:
            raise BrowserArtifactError("invalid_worker", "worker_id 不合法", 400)
        check_session_command(
            command, owner_id=owner_id, node_id=node_id, worker_id=worker_id,
            action=SCREENSHOT_ACTION,
        )
        ticket = repo.begin_artifact_upload(
            ticket_id, token, node_id=node_id, command_id=command_id,
            idempotency_key=idempotency_key,
        )
        if ticket.get("state") == "consumed":
            artifact_id = ticket.get("artifact_id")
            if not artifact_id:
                raise BrowserArtifactError("artifact_ticket_state", "上传票据状态不合法", 409)
            return self.artifact_store.get(owner_id, ticket["workspace_id"], artifact_id)
        return self._store_and_complete(ticket_id, ticket, raw, dimensions)

    def _publish_frame(self, connection, consumed_ticket, frame_dimensions):
        if self.execution_windows is None:
            raise BrowserRepositoryError("frame_scope")
        try:
            self.execution_windows.append_browser_frame_event(
                connection, owner_id=consumed_ticket["owner_id"],
                ticket_id=consumed_ticket["ticket_id"],
                artifact_id=consumed_ticket["artifact_id"],
                sha256=consumed_ticket["sha256"], width=frame_dimensions[0],
                height=frame_dimensions[1],
            )
        except ExecutionWindowRepositoryError as exc:
            raise BrowserRepositoryError(exc.code) from exc

    def _store_and_complete(self, ticket_id, ticket, raw, dimensions):
        repo = self.browser_repository
        digest = hashlib.sha256(raw).hexdigest()
        artifact = None
        try:
            artifact = self.artifact_store.put_bytes(
                ticket["owner_id"], ticket["workspace_id"], raw,
                name="screenshot.png", content_type="image/png",
            )
            if artifact["sha256"] != digest or artifact["size"] != len(raw):
                raise ArtifactError("artifact_corrupt")
            repo.complete_artifact_upload(
                ticket_id, sha256=digest, size=len(raw),
                artifact_id=artifact["artifact_id"],
                frame_publisher=self._publish_frame if dimensions is not None else None,
                frame_dimensions=dimensions,
            )
            return artifact
        except Exception:
            # 任一步失败：票据回到可重试状态，并清理已落盘的孤儿产物。
            repo.reset_artifact_upload(ticket_id)
            if artifact is not None:
                self.artifact_store.delete(
                    ticket["owner_id"], ticket["workspace_id"], artifact["artifact_id"])
            raise
