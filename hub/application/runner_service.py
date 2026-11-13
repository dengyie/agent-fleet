"""Application-layer runner service.

``RunnerService`` owns the runner-facing use cases that previously lived in
the HTTP blueprint:

- poll (lease the next queued task for an authenticated machine)
- heartbeat (renew the lease and forward bounded log lines)
- result (persist the final outcome and announce ``task_finished``)

Lease fencing, nonce checks, result idempotency, and TTLs remain in the task
repository (``hub.infrastructure.task_repository``); this service only
orchestrates them, applies the log-line bounds, and emits events.

The authenticated machine is passed as an explicit argument by the HTTP
adapter. The service never reads a machine field from the runner payload, so
runner-controlled JSON can never widen which machine's tasks it services.
"""

import asyncio
import logging

from hub.application.task_service import ApplicationError
from hub.application.workflow_coding_delivery import WorkflowCodingDeliveryError
from fleet_core.result_files import redact_patch, sanitize_files
from fleet_core.session.redact import redact_secrets_text
from fleet_core.task_result import PatchError, TestSummaryError, normalize_test_summary

logger = logging.getLogger(__name__)

#: Bounded log-line delivery: at most ``MAX_LOG_LINES`` lines per heartbeat,
#: each truncated to ``MAX_LOG_LINE`` characters.
MAX_LOG_LINES = 50
MAX_LOG_LINE = 500
MAX_LOG_SUMMARY = 10240
MAX_DIFF_STAT = 5120


class RunnerService:
    """Application service for the runner credential-facing lease flow."""

    def __init__(self, task_repo, event_publisher) -> None:
        self.repo = task_repo
        self.publisher = event_publisher
        self.workflow_delivery = None

    def _emit(self, event: str, machine=None, task_id=None, **extra) -> None:
        try:
            self.publisher.emit(event, machine=machine, task_id=task_id, **extra)
        except Exception:
            pass

    def _task_op(self, fn, *args, **kwargs):
        """Run a task-repository operation, converting infra failures to 503.

        The runner lifecycle depends on the task repository; when it is
        unavailable the runner API degrades to a bounded 503
        ``tasks_unavailable`` instead of surfacing an internal 500.
        """
        try:
            return fn(*args, **kwargs)
        except ApplicationError:
            raise
        except Exception as exc:
            logger.exception("task repository operation failed: %r", exc)
            raise ApplicationError(
                "tasks_unavailable",
                "任务存储不可用，runner 不可用",
                503,
            ) from None

    def poll(self, machine: str, runner_id: str) -> dict:
        """Lease the next queued task for ``machine`` and announce it.

        ``machine`` is the authenticated machine from the HTTP adapter; the
        repository only leases tasks whose machine matches it.
        """
        runner_id = str(runner_id or machine)[:64]
        task = self._task_op(
            self.repo.lease_task, machine=machine, runner_id=runner_id)
        if task:
            if task.get("source_capture"):
                if self.workflow_delivery is None:
                    raise ApplicationError("source_unavailable", "源码交付不可用", 503)
                try:
                    policy = asyncio.run(self.workflow_delivery.task_source_policy(machine, task["attempt_id"], task["nonce"]))
                except Exception as exc:
                    raise _source_error(exc) from exc
                task.update(policy)
            self._emit("task_leased", machine, task["task_id"], state="leased")
        return {"ok": True, "task": task}

    def heartbeat(self, machine: str, attempt_id: str, nonce: str,
                  log_lines=None) -> dict:
        """Renew a lease and forward bounded log lines as ``task_log`` events.

        Raises :class:`ApplicationError` code ``lease_expired`` (409) when
        the repository rejects the heartbeat.
        """
        result = self._task_op(
            self.repo.heartbeat,
            attempt_id=attempt_id, nonce=str(nonce or ""), machine=machine)
        if result is None:
            raise ApplicationError(
                "lease_expired", "lease 不存在或已过期，runner 应放弃任务", 409)
        lines = log_lines or []
        if isinstance(lines, list):
            for line in lines[:MAX_LOG_LINES]:
                # 先脱敏再截断：截断不能把半截密钥留在日志里。
                self._emit("task_log", machine, result["task_id"],
                           line=redact_secrets_text(
                               str(line)[:MAX_LOG_LINE * 4])[0][:MAX_LOG_LINE])
        source = None
        if self.workflow_delivery is not None:
            source = self._task_op(self.repo.source_origin, machine, attempt_id, str(nonce or ""))
        return {"ok": True, **result, "source_receipt": self._task_op(self.repo.source_receipt, machine, attempt_id, str(nonce or "")) if source else None}

    def result(self, machine: str, attempt_id: str, nonce: str, exit_code,
               log_summary: str = "", diff_stat: str = "",
               duration_s=None, files=None, diff_patch=None, test_summary=None) -> dict:
        """Persist the task result and emit ``task_finished``.

        The exit code is validated at application level. Repository-level
        nonce/attempt fencing and result idempotency are preserved: a full
        (``None``) repository answer maps to a 409
        ``lease_mismatch`` error. Optional ``files`` is re-validated here so
        a runner cannot smuggle traversal/secret/binary bodies into Hub.
        """
        try:
            exit_code = int(exit_code)
        except (TypeError, ValueError):
            raise ApplicationError("invalid_exit_code", "exit_code 必须是整数") from None
        if not isinstance(duration_s, (int, float)):
            duration_s = None
        snapshot = sanitize_files(files)
        if diff_patch is not None:
            try:
                diff_patch, _ = redact_patch(diff_patch)
            except (PatchError, UnicodeError) as exc:
                raise ApplicationError("invalid_diff_patch", "patch 必须是有效 UTF-8 文本", 400) from exc
        try:
            test_summary = normalize_test_summary(test_summary)
        except TestSummaryError as exc:
            raise ApplicationError("invalid_test_summary", "测试摘要不合法", 400) from exc
        out = self._task_op(
            self.repo.complete_task,
            attempt_id=attempt_id, nonce=str(nonce or ""), machine=machine,
            exit_code=exit_code,
            log_summary=redact_secrets_text(
                str(log_summary)[:MAX_LOG_SUMMARY * 2])[0][:MAX_LOG_SUMMARY],
            diff_stat=str(diff_stat)[:MAX_DIFF_STAT],
            duration_s=duration_s,
            files=snapshot,
            diff_patch=diff_patch,
            test_summary=test_summary)
        if out is None:
            raise ApplicationError(
                "lease_mismatch", "attempt_id/nonce 不匹配", 409)
        self._emit("task_finished", machine, out["task_id"], state=out["state"])
        return {"ok": True, **out}

    def source(self, machine: str, attempt_id: str, nonce: str, content: bytes) -> dict:
        if self.workflow_delivery is None or type(content) is not bytes:
            raise ApplicationError("source_unavailable", "源码交付不可用", 404)
        try:
            receipt = self.workflow_delivery.record_task_source(machine, attempt_id, nonce, content)
            receipt = asyncio.run(receipt) if hasattr(receipt, "__await__") else receipt
            bundle = next((item.artifact for item in receipt.outputs if item.output_key == "source_bundle"), None)
            if bundle is None:
                raise WorkflowCodingDeliveryError("invalid_source_receipt")
            return {"ok": True, "artifact_id": bundle.artifact_id, "sha256": bundle.sha256, "size": bundle.size}
        except Exception as exc:
            raise _source_error(exc) from exc


def _source_error(exc: Exception) -> ApplicationError:
    code = getattr(exc, "code", "source_upload_failed")
    conflicts = {"lease_mismatch", "source_upload_conflict", "artifact_id_conflict", "revision_conflict", "binding_conflict", "idempotency_conflict",
                 "delivery_execution_unavailable", "admission_frozen", "source_active_deadline_exceeded"}
    unavailable = {"delivery_io_failed", "workflow_store", "invalid_source_receipt", "read_failed", "write_failed"}
    status = 503 if not isinstance(exc, WorkflowCodingDeliveryError) or code in unavailable else 409 if code in conflicts else 400
    return ApplicationError(code, "源码交付被拒绝" if status < 500 else "源码交付暂不可用", status)
