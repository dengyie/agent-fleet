"""Application facade for durable platform command delivery."""
from __future__ import annotations

import time
import math
import sqlite3
from dataclasses import replace
from typing import Any, Protocol

from fleet_core.browser_submit import SubmitApprovalStore
from hub.domain.execution_window import BROWSER_MUTATION_ACTIONS

from hub.application.task_service import ApplicationError
from fleet_core.platform_command import PlatformCommand, args_hash
from fleet_core.command_timing import CommandTiming, MAX_COMMAND_LIFETIME_S
from hub.domain.capability_execution import CAPABILITY_ARGUMENT


class CapabilityCommandPreparation(Protocol):
    def prepare_command(self, connection: sqlite3.Connection, command: PlatformCommand, *, now: float) -> PlatformCommand: ...


class CommandDeliveryService:
    def __init__(self, repository, *, signing_key=None,
                 require_signature=False, browser_write_guard=None,
                 clock=time.time, sleep=time.sleep,
                 capability_dispatch: CapabilityCommandPreparation | None = None):
        self.repository = repository
        self.signing_key = signing_key
        self.require_signature = bool(require_signature)
        self.browser_write_guard = browser_write_guard
        self.clock = clock
        self.sleep = sleep
        self.capability_dispatch = capability_dispatch

    @staticmethod
    def _translate(exc):
        code = getattr(exc, "code", "command_store")
        status = {"command_not_found": 404, "idempotency_conflict": 409,
                  "invalid_capability_command": 400, "capability_unavailable": 503,
                  "capability_changed": 409, "capability_dispatch_blocked": 409,
                  "arguments_too_large": 413, "approval_required": 409,
                  "invalid_receive_receipt": 409, "receive_scope_unavailable": 409,
                  "receive_expired": 409, "receive_lease_unavailable": 409,
                  "receive_credential_revoked": 409,
                  "invalid_export_command": 409, "invalid_receive_command": 409,
                  "invalid_signature": 409, "command_credential_revoked": 409,
                  "lease_mismatch": 409, "owner_mismatch": 403,
                  "node_mismatch": 403, "lease_expired": 409,
                  "result_too_large": 413, "invalid_command_state": 400,
                  "invalid_unknown_reason": 400, "execution_cancelled": 409,
                  "invalid_command_timing": 400,
                  "invalid_export_receipt": 409, "export_credential_revoked": 409,
                  "command_conflict": 409, "command_signature_required": 503,
                  "invalid_outbox_worker": 400, "writer_lease_active": 409}.get(code, 503)
        detail = {"command_not_found": "命令不存在", "idempotency_conflict": "命令幂等键冲突",
                  "lease_mismatch": "命令租约不匹配", "owner_mismatch": "命令不属于该节点作用域",
                  "node_mismatch": "命令不属于该节点", "lease_expired": "命令租约已过期",
                  "result_too_large": "回执结果超出大小限制",
                  "invalid_export_receipt": "导出回执与已提交产物不匹配",
                  "export_credential_revoked": "导出命令原节点凭据已失效",
                  "command_signature_required": "平台命令签名配置不可用",
                  "writer_lease_active": "浏览器当前由操作员接管",
                  "command_store": "命令存储不可用"}.get(code, "命令投递失败")
        return ApplicationError(code, detail, status)

    def enqueue(self, command: PlatformCommand, *, idempotency_key=None, timing: CommandTiming | None = None):
        try:
            if CAPABILITY_ARGUMENT in command.arguments:
                raise ApplicationError("invalid_capability_command", "能力上下文只能由 Hub 准备", 400)

            def prepare(connection: sqlite3.Connection, pending: PlatformCommand) -> PlatformCommand:
                self._assert_run_not_cancelled(connection, pending)
                self._assert_browser_write_allowed(connection, pending)
                return self._prepare_command(connection, pending)
            return self.repository.enqueue(
                command, idempotency_key=idempotency_key,
                require_signature=self.require_signature,
                prepare=prepare,
                timing=timing,
            )
        except Exception as exc:
            raise self._translate(exc) from exc

    def _prepare_command(self, connection: sqlite3.Connection, command: PlatformCommand) -> PlatformCommand:
        pending = self.capability_dispatch.prepare_command(connection, command, now=float(self.clock())) if self.capability_dispatch is not None else command
        if CAPABILITY_ARGUMENT in pending.arguments and not self.signing_key:
            raise ApplicationError("command_signature_required", "能力命令必须签名", 503)
        return pending.signed(self.signing_key) if self.signing_key and not pending.signature else pending

    def enqueue_submit(self, command: PlatformCommand, *, approvals: SubmitApprovalStore,
                       idempotency_key: str | None = None, timing: CommandTiming | None = None) -> dict[str, Any]:
        """Admit and sign the exact approval-bound command inside its durable write."""
        if (command.action != "tool.browser.submit" or command.retry_class != "manual_only"
                or not command.run_id or not command.owner_id
                or set(command.arguments) != {"session_id", "selector"}):
            raise ApplicationError("invalid_arguments", "提交命令参数不合法", 400)
        if not self.signing_key:
            raise ApplicationError("command_signature_required", "提交命令必须签名", 503)

        def prepare(connection: Any, pending: PlatformCommand) -> PlatformCommand:
            self._assert_run_not_cancelled(connection, pending)
            self._assert_browser_write_allowed(connection, pending)
            approval_id = approvals.consume_submit_approval(
                pending.owner_id, pending.run_id, session_id=pending.arguments["session_id"],
                selector=pending.arguments["selector"], command_id=pending.command_id,
                workspace_id=pending.resource_id, node_id=pending.target_node,
                now=float(self.clock()), connection=connection,
            )
            arguments = {**pending.arguments, "approval_id": approval_id}
            return self._prepare_command(connection, replace(pending, arguments=arguments, args_digest=args_hash(arguments), signature=None))

        try:
            return self.repository.enqueue(command, idempotency_key=idempotency_key or command.command_id,
                                           require_signature=True, prepare=prepare, timing=timing)
        except Exception as exc:
            code = getattr(exc, "code", "command_store")
            if code in {"approval_required", "approval_expired", "approval_consumed", "approval_revoked", "approval_limit", "session_not_found", "invalid_selector", "sensitive_field_forbidden"}:
                raise ApplicationError(code, "提交审批不可用", 409) from exc
            raise self._translate(exc) from exc

    @staticmethod
    def _assert_run_not_cancelled(connection, command):
        # Run cancellation and command insertion serialize with Node claim.
        # A callback checked before opening this transaction can race cancel.
        if (command.run_id and command.owner_id
                and command.command_id.startswith(command.run_id + ':step:')
                and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone()
                and connection.execute('SELECT 1 FROM runs WHERE owner_id=? AND run_id=? AND cancel_requested=1',
                                       (command.owner_id, command.run_id)).fetchone()):
            raise ApplicationError('execution_cancelled', '运行已取消', 409)

    def _assert_browser_write_allowed(self, connection, command: PlatformCommand) -> None:
        if (self.browser_write_guard is not None
                and command.action in BROWSER_MUTATION_ACTIONS
                and command.owner_id and command.run_id):
            self.browser_write_guard(
                connection, command.owner_id, command.run_id,
                now=float(self.clock()),
            )

    def poll(self, node_id: str, worker_id: str, *, owner_id=None, limit=20, lease_s=60,
             credential_fingerprint: str | None = None):
        try:
            return self.repository.claim_for_node(node_id, worker_id, owner_id=owner_id, limit=limit, lease_s=lease_s,
                                                  credential_fingerprint=credential_fingerprint)
        except Exception as exc:
            raise self._translate(exc) from exc

    def renew_command(self, command_id: str, worker_id: str, *, owner_id: str,
                      node_id: str, lease_s: float = 15.0,
                      credential_fingerprint: str | None = None):
        try:
            return self.repository.renew_command(command_id, worker_id, owner_id=owner_id,
                node_id=node_id, lease_s=lease_s, credential_fingerprint=credential_fingerprint)
        except Exception as exc:
            raise self._translate(exc) from exc

    def receipt(self, command_id: str, status: str, *, result=None, worker_id=None, owner_id=None, node_id=None,
                credential_fingerprint: str | None = None):
        try:
            return self.repository.record_receipt(command_id, status, result=result, worker_id=worker_id, owner_id=owner_id, node_id=node_id,
                                                 credential_fingerprint=credential_fingerprint)
        except Exception as exc:
            raise self._translate(exc) from exc

    def wait_for_receipt(self, command_id: str, *, timeout_s=30.0,
                         poll_interval_s=0.1, should_cancel=None):
        """Observe timed commands without replacing their durable deadlines.

        Legacy callers retain their explicit timeout fence. For timed commands
        a shorter local wait returns the current row; only durable queue,
        receipt or lease expiry settles the execution outcome.
        """
        try:
            if not math.isfinite(float(timeout_s)) or not math.isfinite(float(poll_interval_s)):
                raise ValueError("invalid wait")
            timeout = max(0.0, min(float(timeout_s), MAX_COMMAND_LIFETIME_S))
            interval = max(0.01, min(float(poll_interval_s), 5.0))
        except (TypeError, ValueError):
            raise ApplicationError("invalid_wait", "回执等待参数不合法", 400) from None
        started = float(self.clock())
        timing = self.repository.get_timing(command_id)
        polls = 0
        max_polls = max(1, int(timeout / interval) + 1)
        while True:
            polls += 1
            row = self.repository.get(command_id)
            if row is None:
                raise self._translate(type("Missing", (), {"code": "command_not_found"})())
            if row.get("status") in {"succeeded", "failed", "unknown", "expired"}:
                return row
            if should_cancel is not None and should_cancel():
                return self.repository.cancel_wait(command_id)
            # Waiting is read-only until an actual durable boundary is due;
            # otherwise every remote waiter would take SQLite's write lock
            # ten times per second while the Node is still executing.
            if timing is not None and timing.get("claimed_at") is None and row.get("status") != "queued":
                timing = self.repository.get_timing(command_id)
            deadline = float(row["expires_at"])
            if row.get("lease_until") is not None:
                deadline = min(deadline, float(row["lease_until"]))
            if timing is not None:
                boundary = timing["queue_deadline"] if row.get("status") == "queued" else timing["receipt_deadline"]
                if boundary is not None:
                    deadline = min(deadline, float(boundary))
            if float(self.clock()) >= deadline:
                row = self.repository.expire_command(command_id)
                if row.get("status") in {"succeeded", "failed", "unknown", "expired"}:
                    return row
            if float(self.clock()) - started >= timeout or polls >= max_polls:
                if timing is not None:
                    return row
                return self.repository.mark_unknown(command_id, reason="receipt_timeout")
            self.sleep(interval)


__all__ = ["CommandDeliveryService"]
