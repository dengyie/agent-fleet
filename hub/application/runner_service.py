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

import logging

from hub.application.task_service import ApplicationError
from tools.result_files import sanitize_files

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
            attempt_id=attempt_id, nonce=str(nonce or ""))
        if result is None:
            raise ApplicationError(
                "lease_expired", "lease 不存在或已过期，runner 应放弃任务", 409)
        lines = log_lines or []
        if isinstance(lines, list):
            for line in lines[:MAX_LOG_LINES]:
                self._emit("task_log", machine, result["task_id"],
                           line=str(line)[:MAX_LOG_LINE])
        return {"ok": True, **result}

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
        out = self._task_op(
            self.repo.complete_task,
            attempt_id=attempt_id, nonce=str(nonce or ""), exit_code=exit_code,
            log_summary=str(log_summary)[:MAX_LOG_SUMMARY],
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