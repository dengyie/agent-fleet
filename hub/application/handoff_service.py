"""Read-only evidence previews and confirmed new-task handoffs."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from hub.application.task_service import ApplicationError, TaskService
from hub.domain.handoff import HandoffConflict, MAX_OVERRIDE_CHARS, build_handoff_snapshot, render_handoff_prompt
from hub.domain.task import DomainError, validate_task_input
from session_schema import is_valid_session_id


class SessionReader(Protocol):
    def get_session(self, session_id: str) -> dict[str, Any] | None: ...


class HandoffTranscriptReader(Protocol):
    def read_handoff_events(self, session_id: str, *, limit: int = 1000) -> tuple[list[dict], bool]: ...

    def read_handoff_signals(self, session_id: str) -> list[dict]: ...


class HandoffService:
    def __init__(self, session_repo: SessionReader, transcript_repo: HandoffTranscriptReader,
                 tasks: TaskService, *, enabled: bool = True, task_store_available: bool = True) -> None:
        self.sessions = session_repo
        self.transcript = transcript_repo
        self.tasks = tasks
        self.enabled = enabled
        self.task_store_available = task_store_available

    @staticmethod
    def _io(operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return operation(*args, **kwargs)
        except (ApplicationError, DomainError):
            raise
        except Exception as exc:
            raise ApplicationError("handoff_unavailable", "接力存储暂不可用", 503) from exc

    def _source(self, session_id: str) -> tuple[dict, dict | None, str | None]:
        if not is_valid_session_id(session_id):
            raise ApplicationError("invalid_session_id", "会话标识不合法")
        session = self._io(self.sessions.get_session, session_id)
        if session is None:
            raise ApplicationError("session_not_found", "会话不存在", 404)
        task = None
        if self.task_store_available and session.get("attempt_id"):
            task = self._io(self.tasks.task_repo.get_task_for_attempt, session["attempt_id"])
            if task and task.get("machine") != session.get("machine_id"):
                task = None
        project = task.get("project") if task else session.get("project_id")
        if project and not self.tasks.hosts.project_allowed(session.get("machine_id"), project):
            project = None
        return session, task, project

    def preview(self, session_id: str) -> dict[str, Any]:
        session, source_task, project = self._source(session_id)
        events, truncated = self._io(self.transcript.read_handoff_events, session_id, limit=1000)
        signals = self._io(self.transcript.read_handoff_signals, session_id)
        snapshot = build_handoff_snapshot(
            {**session, "handoff_truncated": truncated, "handoff_signals": signals}, events,
            source_task.get("result") if source_task else None)
        managed = session.get("managed") is True or session.get("managed") == 1
        return {"snapshot": snapshot, "prompt": render_handoff_prompt(snapshot),
                "source_project": project, "can_create": bool(self.enabled and managed and project),
                "targets": self.tasks.hosts.handoff_targets(project) if project else []}

    @staticmethod
    def _request(data: Any) -> dict[str, str]:
        fields = {"target_machine", "target_agent", "target_project", "instruction_override", "client_token"}
        if not isinstance(data, Mapping) or set(data) - fields:
            raise ApplicationError("invalid_handoff", "接力请求含未知字段或格式不正确")
        override = data.get("instruction_override", "")
        token = data.get("client_token")
        if not isinstance(override, str) or len(override) > MAX_OVERRIDE_CHARS:
            raise ApplicationError("invalid_handoff", "接力指令最多 500 字符")
        if not isinstance(token, str) or not token.strip() or len(token) > 128:
            raise ApplicationError("invalid_handoff", "接力凭据不能为空且最多 128 字符")
        try:
            valid = validate_task_input({"machine": data.get("target_machine"),
                "agent_type": data.get("target_agent"), "project": data.get("target_project"),
                "instruction": override or "接力", "client_token": token})
        except DomainError as exc:
            raise ApplicationError(exc.code, exc.detail) from exc
        return {"target_machine": valid["machine"], "target_agent": valid["agent_type"],
                "target_project": valid["project"], "instruction_override": override,
                "client_token": token}

    @staticmethod
    def _receipt(row: Mapping[str, Any], digest: str) -> dict[str, str]:
        if row["request_digest"] != digest:
            raise ApplicationError("handoff_token_conflict", "同一接力凭据不能用于不同请求", 409)
        return {key: row[key] for key in ("task_id", "attempt_id", "handoff_id")}

    def create(self, session_id: str, data: Any, actor: str) -> dict[str, str]:
        request = self._request(data)
        if not self.enabled:
            raise ApplicationError("tasks_unavailable", "任务存储不可用", 503)
        token = "handoff-" + hashlib.sha256((actor + "\0" + request["client_token"]).encode()).hexdigest()
        digest = hashlib.sha256(json.dumps({"session_id": session_id, **request},
            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        existing = self._io(self.tasks.task_repo.get_handoff_receipt, token)
        if existing:
            return self._receipt(existing, digest)
        session, _, project = self._source(session_id)
        if session.get("managed") not in (True, 1):
            raise ApplicationError("session_not_managed", "仅受管会话可以创建接力任务", 409)
        if not project or request["target_project"] != project:
            raise ApplicationError("handoff_project_mismatch", "接力仅允许同一已注册项目", 409)
        snapshot = self.preview(session_id)["snapshot"]
        if not snapshot["objective"] and not request["instruction_override"].strip():
            raise ApplicationError("invalid_handoff", "未采集到用户目标，请填写接力指令")
        instruction = render_handoff_prompt(snapshot, request["instruction_override"])
        try:
            self.tasks.create({"machine": request["target_machine"], "agent_type": request["target_agent"],
                "project": project, "instruction": instruction, "client_token": token, "confirm": True},
                actor, handoff={"request_digest": digest, "source_session_id": session_id})
        except (DomainError, HandoffConflict) as exc:
            status = 409 if exc.code == "handoff_token_conflict" else 400
            raise ApplicationError(exc.code, exc.detail, status) from exc
        receipt = self._io(self.tasks.task_repo.get_handoff_receipt, token)
        if receipt is None:
            raise ApplicationError("handoff_unavailable", "接力回执尚不可用，请使用同一凭据重试", 503)
        return self._receipt(receipt, digest)
