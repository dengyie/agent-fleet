"""Application-layer task service.

``TaskService`` owns the task creation/lifecycle business logic that
previously lived in the HTTP blueprint:

- input validation (delegated to the pure domain validators)
- machine reachability check ("online" = last observation is reachable)
- project whitelist check
- create / list / get / cancel / retry use cases
- task event emission (``task_queued`` / ``task_cancelled``)
- public task DTO serialization

Constructor-injected task repository, observation repository, event
publisher, and host policy keep this module free of Flask, request state, and
module-level path lookups. The service returns public/domain dictionaries and
raises :class:`ApplicationError` for stable application errors; it never
returns an HTTP response.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from typing import Any

from hub.domain.task import (
    DEFAULT_AGENT_TYPES,
    MAX_DIFF_PATCH,
    public_task,
    validate_task_input,
)
from session_schema import is_valid_session_id
from tools.result_files import (
    MAX_READS_PER_WINDOW,
    PathRejected,
    READ_WINDOW_S,
    normalize_path,
    public_file,
    public_file_meta,
)

logger = logging.getLogger(__name__)

#: Allowed task states for the list filter (mirrors the repository states).
TASK_STATES = (
    "queued", "leased", "running", "paused",
    "succeeded", "failed", "cancelled", "expired",
)

#: Bounded scan when projecting attempt_id → session_id (same cap as ControlRouter).
_SESSION_SCAN = 1000


class ApplicationError(Exception):
    """Stable, bounded application error.

    ``code`` is the public error key; ``detail`` is a user-visible bounded
    description; ``status`` is the HTTP status mapped by the adapter layer.
    """

    def __init__(self, code: str, detail: str = "", status: int = 400) -> None:
        self.code = code
        self.detail = str(detail)[:200]
        self.status = int(status)
        super().__init__(self.detail)


class TaskHostPolicy:
    """Deployment policy for the task project whitelist.

    Mirrors the legacy precedence: an explicit ``project_whitelist`` mapping
    (machine -> project list) wins; otherwise the whitelist is derived from
    ``hosts.yaml`` rows' ``projects`` fields, matching the old route helper.
    """

    def __init__(self, *, project_whitelist=None, hosts=()) -> None:
        self._whitelist = project_whitelist
        self._hosts = list(hosts or ())

    def project_allowed(self, machine: str, project: str) -> bool:
        if self._whitelist is not None:
            return project in (self._whitelist.get(machine) or [])
        for host in self._hosts:
            if host.get("name") == machine:
                return project in (host.get("projects") or [])
        return False


class TaskService:
    """Application service for the operator-facing task lifecycle."""

    #: Optional cancel->control router (Task 10).  ``None`` keeps the cancel
    #: behaviour byte-identical to the pre-Task-10 surface; wired additively in
    #: bootstrap only when supervisor/session control is enabled.
    cancel_router = None

    def __init__(self, task_repo, observation_repo, event_publisher,
                 host_policy: TaskHostPolicy, session_repo=None) -> None:
        self.task_repo = task_repo
        self.observation_repo = observation_repo
        self.publisher = event_publisher
        self.hosts = host_policy
        # Optional Phase 3 projection only. None = omit session_id (byte-identical
        # to the pre-Phase-3 public task DTO). Never creates bindings.
        self.session_repo = session_repo
        self._file_reads: dict[str, list[float]] = {}
        self._file_read_lock = threading.Lock()

    # -- helpers ------------------------------------------------------------

    def _machine_online(self, machine: str) -> bool:
        """True when the machine has a reachable observation snapshot."""
        current = self.observation_repo.read_current(machine)
        return bool(current and current.get("reachable", True))

    def _emit(self, event: str, machine=None, task_id=None, state=None) -> None:
        try:
            self.publisher.emit(event, machine=machine, task_id=task_id, state=state)
        except Exception:
            pass

    def _task_op(self, fn, *args, **kwargs):
        """Run a task-repository operation, converting infra failures to 503.

        Task-storage failures must degrade the task API (503 ``tasks_unavailable``)
        without disabling the observation/status routes. Application errors
        already raised by the repository are stable and re-raised untouched;
        unexpected infrastructure exceptions are bloomed into a bounded
        ``tasks_unavailable`` error and logged.
        """
        try:
            return fn(*args, **kwargs)
        except ApplicationError:
            raise
        except Exception as exc:
            logger.exception("task repository operation failed: %r", exc)
            raise ApplicationError(
                "tasks_unavailable",
                "任务存储不可用，观测链路不受影响",
                503,
            ) from None

    # -- write use case ------------------------------------------------------

    def create(self, data: Any, actor: str) -> dict:
        """Validate, apply policy, persist, and announce a new task.

        Returns ``{"ok": True, "created": bool, "task": <public>}``. Raises
        :class:`ApplicationError` for ``machine_offline`` /
        ``project_not_allowed`` and ``DomainError`` for malformed input.
        """
        validated = validate_task_input(
            data, agent_types=DEFAULT_AGENT_TYPES)
        machine = validated["machine"]
        project = validated["project"]
        if not self._machine_online(machine):
            raise ApplicationError("machine_offline", f"{machine} 不在线")
        if not self.hosts.project_allowed(machine, project):
            raise ApplicationError(
                "project_not_allowed", f"{machine} 未注册项目 {project}")
        task, created = self._task_op(
            self.task_repo.create_task,
            machine=machine, agent_type=validated["agent_type"],
            project=project, instruction=validated["instruction"],
            requested_by=actor,
            client_token=validated.get("client_token"),
            confirm=bool(validated.get("confirm")))
        if created:
            self._emit("task_queued", task["machine"], task["task_id"], "queued")
        return {"ok": True, "created": created, "task": self._public_task(task)}

    # -- read use cases ------------------------------------------------------

    def get(self, task_id: str) -> dict:
        """Public detail of one task, with its result when available."""
        task = self._task_op(self.task_repo.get_task, task_id)
        if not task:
            raise ApplicationError("not_found", "任务不存在", 404)
        return {"ok": True, "task": self._public_task(task, with_result=True)}

    def list(self, machine=None, state=None, limit=50) -> dict:
        """List tasks (optionally filtered), returning public DTOs."""
        if state is not None and state not in TASK_STATES:
            raise ApplicationError("invalid_state", "state 不合法")
        tasks = self._task_op(
            self.task_repo.list_tasks,
            machine=machine or None, state=state or None,
            limit=min(200, max(1, int(limit))))
        return {"ok": True, "tasks": self._public_tasks(tasks)}

    def list_files(self, task_id: str) -> dict:
        """Hub-local snapshot listing (no bodies). Missing task → 404."""
        task = self._task_op(self.task_repo.get_task, task_id)
        if not task:
            raise ApplicationError("not_found", "任务不存在", 404)
        rows = self._task_op(self.task_repo.list_result_files, task_id)
        return {
            "ok": True,
            "task_id": task_id,
            "files": [public_file_meta(row) for row in (rows or ())],
        }

    def get_file(self, task_id: str, relpath: str, actor: str) -> dict:
        """Hub-local snapshot body. Operator-auth, rate-limited, audited."""
        try:
            path = normalize_path(relpath)
        except PathRejected:
            raise ApplicationError("invalid_path", "路径不合法", 400) from None
        task = self._task_op(self.task_repo.get_task, task_id)
        if not task:
            raise ApplicationError("not_found", "任务不存在", 404)
        self._enforce_file_rate(actor or "operator")
        row = self._task_op(self.task_repo.get_result_file, task_id, path)
        if not row:
            raise ApplicationError("not_found", "文件不存在", 404)
        try:
            self.task_repo.audit_action(
                actor or "operator", "read_task_file", task_id,
                {"path": path, "bytes": int(row.get("bytes") or 0)})
        except Exception:
            logger.debug("read_task_file audit skipped", exc_info=True)
        return {"ok": True, **public_file(row)}

    def _enforce_file_rate(self, actor: str) -> None:
        now = time.time()
        key = str(actor or "operator")[:128]
        with self._file_read_lock:
            stamps = [ts for ts in self._file_reads.get(key, [])
                      if now - ts < READ_WINDOW_S]
            if len(stamps) >= MAX_READS_PER_WINDOW:
                self._file_reads[key] = stamps
                raise ApplicationError("rate_limited", "读取过频，请稍后重试", 429)
            stamps.append(now)
            self._file_reads[key] = stamps

    # -- lifecycle mutations --------------------------------------------------

    def cancel(self, task_id: str, actor: str) -> dict:
        """Cancel a queued/leased/running task; emit ``task_cancelled``.

        When a Task 10 control router is wired, a MANAGED active attempt is
        additionally routed through ``cancel_attempt``.  The router is invoked
        AFTER the repository transition is accepted and its success/failure can
        never alter the already-committed task state (bounded
        ``control_pending``/``control_failed`` audit is recorded by the router
        instead).  Unmanaged/queued/terminal cancels are runtime-identical.
        """
        row, changed = self._task_op(self.task_repo.cancel_task, task_id, actor)
        if row is None:
            raise ApplicationError("not_found", "任务不存在", 404)
        if changed:
            self._emit("task_cancelled", row["machine"], task_id, "cancelled")
            router = getattr(self, "cancel_router", None)
            if router is not None:
                try:
                    router.enqueue_cancel_attempt(
                        task_id=task_id,
                        attempt_id=row.get("attempt_id") or "",
                        operator=actor,
                        reason_code="operator_requested")
                except Exception:
                    # The router records its own bounded audit; the committed
                    # cancel transition is never rolled back or re-driven here.
                    logger.debug("cancel control enqueue skipped", exc_info=True)
        return {"ok": True, "changed": changed, "task": self._public_task(row)}

    def retry(self, task_id: str, actor: str) -> dict:
        """Requeue a terminal task; emit ``task_queued``."""
        row, changed = self._task_op(self.task_repo.retry_task, task_id, actor)
        if row is None:
            raise ApplicationError("not_found", "任务不存在", 404)
        if changed:
            self._emit("task_queued", row["machine"], task_id, "queued")
        return {"ok": True, "changed": changed, "task": self._public_task(row)}

    def pause(self, task_id: str, actor: str) -> dict:
        row, changed = self._task_op(self.task_repo.pause_task, task_id, actor)
        if row is None:
            raise ApplicationError("not_found", "任务不存在", 404)
        if not changed:
            raise ApplicationError("conflict", "当前状态不可暂停", 409)
        self._emit("task_paused", row["machine"], task_id, "paused")
        return {"ok": True, "changed": True, "task": self._public_task(row)}

    def continue_task(self, task_id: str, actor: str) -> dict:
        row, changed = self._task_op(self.task_repo.continue_task, task_id, actor)
        if row is None:
            raise ApplicationError("not_found", "任务不存在", 404)
        if not changed:
            raise ApplicationError("conflict", "当前状态不可继续", 409)
        self._emit("task_queued", row["machine"], task_id, "queued")
        return {"ok": True, "changed": True, "task": self._public_task(row)}

    def confirm(self, task_id: str, actor: str) -> dict:
        row, changed = self._task_op(self.task_repo.confirm_task, task_id, actor)
        if row is None:
            raise ApplicationError("not_found", "任务不存在", 404)
        if not changed:
            raise ApplicationError("not_found", "没有待确认的闸", 404)
        return {"ok": True, "changed": True, "task": self._public_task(row)}

    def reject(self, task_id: str, actor: str) -> dict:
        row, changed = self._task_op(self.task_repo.reject_task, task_id, actor)
        if row is None:
            raise ApplicationError("not_found", "任务不存在", 404)
        if not changed:
            raise ApplicationError("not_found", "没有待确认的闸", 404)
        self._emit("task_cancelled", row["machine"], task_id, "cancelled")
        return {"ok": True, "changed": True, "task": self._public_task(row)}

    def get_diff(self, task_id: str, actor: str) -> dict:
        task = self._task_op(self.task_repo.get_task, task_id)
        if not task:
            raise ApplicationError("not_found", "任务不存在", 404)
        result = task.get("result") or {}
        patch = result.get("diff_patch")
        if not isinstance(patch, str) or not patch:
            raise ApplicationError("not_found", "没有 patch", 404)
        truncated = len(patch) > MAX_DIFF_PATCH
        body = patch[:MAX_DIFF_PATCH]
        try:
            self.task_repo.audit_action(
                actor or "operator", "read_task_diff", task_id,
                {"bytes": len(body.encode("utf-8"))})
        except Exception:
            logger.debug("read_task_diff audit skipped", exc_info=True)
        return {
            "ok": True,
            "task_id": task_id,
            "diff_patch": body,
            "truncated": truncated,
        }

    # -- Phase 3 deep-link projection ----------------------------------------

    def _public_task(self, task: Mapping[str, Any] | None,
                     with_result: bool = False) -> dict[str, Any]:
        """Public task DTO plus optional session_id when a binding already exists."""
        if not isinstance(task, Mapping):
            return {}
        out = public_task(task, with_result=with_result)
        session_id = self._lookup_session_id(task)
        if session_id:
            out["session_id"] = session_id
        if with_result and "result" in out and isinstance(out["result"], dict):
            # Keep list/detail DTO free of full patch bodies; expose a flag.
            patch = out["result"].pop("diff_patch", None)
            if isinstance(patch, str) and patch:
                out["result"]["has_diff_patch"] = True
                out["result"]["diff_patch_bytes"] = len(patch.encode("utf-8"))
        return out

    def _public_tasks(self, tasks) -> list[dict[str, Any]]:
        if not tasks:
            return []
        index = self._session_index_for(tasks)
        out = []
        for task in tasks:
            if not isinstance(task, Mapping):
                continue
            row = public_task(task)
            session_id = index.get((task.get("machine"), task.get("attempt_id")))
            if session_id:
                row["session_id"] = session_id
            out.append(row)
        return out

    def _lookup_session_id(self, task: Mapping[str, Any]) -> str:
        machine = task.get("machine")
        attempt = task.get("attempt_id")
        if not isinstance(machine, str) or not machine:
            return ""
        if not isinstance(attempt, str) or not attempt:
            return ""
        return self._session_index_for([task]).get((machine, attempt), "")

    def _session_index_for(self, tasks) -> dict[tuple, str]:
        """Map (machine, attempt_id) → session_id from existing session rows.

        Fail-closed: missing repo, scan errors, or invalid ids omit the field.
        Does not create, write, or invent bindings. One bounded list per machine.
        """
        index: dict[tuple, str] = {}
        if self.session_repo is None:
            return index
        machines: list[str] = []
        seen: set[str] = set()
        for task in tasks:
            if not isinstance(task, Mapping):
                continue
            machine = task.get("machine")
            if isinstance(machine, str) and machine and machine not in seen:
                seen.add(machine)
                machines.append(machine)
        for machine in machines:
            try:
                rows = self.session_repo.list_sessions(
                    machine_id=machine, limit=_SESSION_SCAN)
            except Exception:
                continue
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                attempt = row.get("attempt_id")
                session_id = row.get("session_id")
                if not isinstance(attempt, str) or not attempt:
                    continue
                if not isinstance(session_id, str) or not is_valid_session_id(session_id):
                    continue
                key = (machine, attempt)
                if key not in index:
                    index[key] = session_id
        return index
