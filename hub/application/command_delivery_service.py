"""Application facade for durable platform command delivery."""
from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

from hub.domain.browser_submit import SubmitApprovalStore
from hub.domain.execution_window import BROWSER_MUTATION_ACTIONS

from hub.application.task_service import ApplicationError
from hub.domain.platform_command import PlatformCommand, args_hash


class CommandDeliveryService:
    def __init__(self, repository, *, signing_key=None,
                 require_signature=False, browser_write_guard=None,
                 clock=time.time, sleep=time.sleep):
        self.repository = repository
        self.signing_key = signing_key
        self.require_signature = bool(require_signature)
        self.browser_write_guard = browser_write_guard
        self.clock = clock
        self.sleep = sleep

    @staticmethod
    def _translate(exc):
        code = getattr(exc, "code", "command_store")
        status = {"command_not_found": 404, "idempotency_conflict": 409,
                  "lease_mismatch": 409, "owner_mismatch": 403,
                  "node_mismatch": 403, "lease_expired": 409,
                  "result_too_large": 413, "invalid_command_state": 400,
                  "invalid_unknown_reason": 400,
                  "command_conflict": 409, "command_signature_required": 503,
                  "invalid_outbox_worker": 400, "writer_lease_active": 409}.get(code, 503)
        detail = {"command_not_found": "命令不存在", "idempotency_conflict": "命令幂等键冲突",
                  "lease_mismatch": "命令租约不匹配", "owner_mismatch": "命令不属于该节点作用域",
                  "node_mismatch": "命令不属于该节点", "lease_expired": "命令租约已过期",
                  "result_too_large": "回执结果超出大小限制",
                  "command_signature_required": "平台命令签名配置不可用",
                  "writer_lease_active": "浏览器当前由操作员接管",
                  "command_store": "命令存储不可用"}.get(code, "命令投递失败")
        return ApplicationError(code, detail, status)

    def enqueue(self, command: PlatformCommand, *, idempotency_key=None):
        try:
            if self.signing_key and not command.signature:
                command = command.signed(self.signing_key)
            prepare = None
            if (self.browser_write_guard is not None
                    and command.action in BROWSER_MUTATION_ACTIONS
                    and command.owner_id and command.run_id):
                def prepare(connection, pending):
                    self._assert_browser_write_allowed(connection, pending)
                    return pending
            return self.repository.enqueue(
                command, idempotency_key=idempotency_key,
                require_signature=self.require_signature,
                prepare=prepare,
            )
        except Exception as exc:
            raise self._translate(exc) from None

    def enqueue_submit(self, command: PlatformCommand, *, approvals: SubmitApprovalStore,
                       idempotency_key: str | None = None) -> dict[str, Any]:
        """Admit and sign the exact approval-bound command inside its durable write."""
        if (command.action != "tool.browser.submit" or command.retry_class != "manual_only"
                or not command.run_id or not command.owner_id
                or set(command.arguments) != {"session_id", "selector"}):
            raise ApplicationError("invalid_arguments", "提交命令参数不合法", 400)
        if not self.signing_key:
            raise ApplicationError("command_signature_required", "提交命令必须签名", 503)

        def prepare(connection: Any, pending: PlatformCommand) -> PlatformCommand:
            self._assert_browser_write_allowed(connection, pending)
            approval_id = approvals.consume_submit_approval(
                pending.owner_id, pending.run_id, session_id=pending.arguments["session_id"],
                selector=pending.arguments["selector"], command_id=pending.command_id,
                workspace_id=pending.resource_id, node_id=pending.target_node,
                now=float(self.clock()), connection=connection,
            )
            arguments = {**pending.arguments, "approval_id": approval_id}
            return replace(pending, arguments=arguments, args_digest=args_hash(arguments), signature=None).signed(self.signing_key)

        try:
            return self.repository.enqueue(command, idempotency_key=idempotency_key or command.command_id,
                                           require_signature=True, prepare=prepare)
        except Exception as exc:
            code = getattr(exc, "code", "command_store")
            if code in {"approval_required", "approval_expired", "approval_consumed", "approval_revoked", "approval_limit", "session_not_found", "invalid_selector", "sensitive_field_forbidden"}:
                raise ApplicationError(code, "提交审批不可用", 409) from exc
            raise self._translate(exc) from exc

    def _assert_browser_write_allowed(self, connection, command: PlatformCommand) -> None:
        if (self.browser_write_guard is not None
                and command.action in BROWSER_MUTATION_ACTIONS
                and command.owner_id and command.run_id):
            self.browser_write_guard(
                connection, command.owner_id, command.run_id,
                now=float(self.clock()),
            )

    def poll(self, node_id: str, worker_id: str, *, owner_id=None, limit=20, lease_s=60):
        try:
            return self.repository.claim_for_node(node_id, worker_id, owner_id=owner_id, limit=limit, lease_s=lease_s)
        except Exception as exc:
            raise self._translate(exc) from None

    def receipt(self, command_id: str, status: str, *, result=None, worker_id=None, owner_id=None, node_id=None):
        try:
            return self.repository.record_receipt(command_id, status, result=result, worker_id=worker_id, owner_id=owner_id, node_id=node_id)
        except Exception as exc:
            raise self._translate(exc) from None

    def wait_for_receipt(self, command_id: str, *, timeout_s=30.0,
                         poll_interval_s=0.1):
        """Wait for a durable terminal receipt, then fence uncertainty."""
        try:
            timeout = max(0.0, min(float(timeout_s), 300.0))
            interval = max(0.01, min(float(poll_interval_s), 5.0))
        except (TypeError, ValueError):
            raise ApplicationError("invalid_wait", "回执等待参数不合法", 400) from None
        started = float(self.clock())
        polls = 0
        max_polls = max(1, int(timeout / interval) + 1)
        while True:
            polls += 1
            row = self.repository.get(command_id)
            if row is None:
                raise self._translate(type("Missing", (), {"code": "command_not_found"})())
            if row.get("status") in {"succeeded", "failed", "unknown", "expired"}:
                return row
            if float(self.clock()) - started >= timeout or polls >= max_polls:
                return self.repository.mark_unknown(command_id, reason="receipt_timeout")
            self.sleep(interval)


__all__ = ["CommandDeliveryService"]
