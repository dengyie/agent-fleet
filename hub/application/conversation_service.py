"""Conversation and Run application services for the platform assistant."""
from __future__ import annotations

import secrets
import time
from collections.abc import Mapping

from hub.application.task_service import ApplicationError
from hub.domain.conversation import UserTurn
from hub.domain.run import Run
from hub.application.platform_memory_context_service import (
    PlatformMemoryContextError,
)
from platform_schema import PlatformValidationError, validate_id, validate_owner_id


def _opaque(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


class ConversationService:
    """Owns the durable conversation/turn boundary.

    Model execution is intentionally outside this service. It only commits the
    user message, immutable run configuration snapshot, and initial state.
    """

    def __init__(self, repository, defaults_service, *, memory_context_service=None,
                 clock=time.time):
        self.repository = repository
        self.defaults = defaults_service
        self.memory_context = memory_context_service
        self.clock = clock

    @staticmethod
    def _translate(exc: Exception) -> ApplicationError:
        code = getattr(exc, "code", "platform_store")
        status = {
            "conversation_not_found": 404,
            "run_not_found": 404,
            "conversation_conflict": 409,
            "idempotency_conflict": 409,
            "reference_forbidden": 409,
            "invalid_id": 400,
            "invalid_owner": 400,
            "invalid_client_token": 400,
            "invalid_message": 400,
            "invalid_limit": 400,
            "message_too_large": 413,
            "memory_context_disabled": 409,
            "memory_selection_stale": 409,
            "revision_conflict": 409,
            "memory_search_unavailable": 503,
        }.get(code, 503)
        detail = {
            "conversation_not_found": "对话不存在",
            "run_not_found": "运行不存在",
            "conversation_conflict": "对话标识冲突",
            "idempotency_conflict": "幂等键对应的请求内容不同",
            "reference_forbidden": "工作区或运行资源不可用",
            "invalid_limit": "limit 不合法",
            "platform_store": "平台存储不可用",
            "memory_context_disabled": "记忆上下文未启用",
            "memory_selection_stale": "记忆选择已失效",
            "revision_conflict": "记忆版本已变化，请重新选择",
            "memory_search_unavailable": "记忆搜索不可用",
        }.get(code, "平台操作失败")
        return ApplicationError(code, detail, status)

    @staticmethod
    def _owner(owner_id: str) -> str:
        try:
            return validate_owner_id(owner_id)
        except PlatformValidationError as exc:
            raise ApplicationError(exc.code, exc.detail, 400) from None

    def create(self, owner_id: str, *, title: str = "", workspace_id: str | None = None, overrides: Mapping | None = None) -> dict:
        owner_id = self._owner(owner_id)
        conversation_id = _opaque("conv")
        try:
            row = self.repository.create_conversation(
                owner_id, conversation_id, title=str(title or "")[:120],
                workspace_id=workspace_id, overrides=overrides or {})
            return {"ok": True, "conversation": row}
        except Exception as exc:
            raise self._translate(exc) from None

    def get(self, owner_id: str, conversation_id: str) -> dict:
        owner_id = self._owner(owner_id)
        try:
            row = self.repository.get_conversation(owner_id, conversation_id)
            if row is None:
                raise ApplicationError("conversation_not_found", "对话不存在", 404)
            # Public response exposes message text only to its owner, which is
            # already enforced by the repository query and operator domain.
            conversation = dict(row)
            conversation["runs"] = [
                Run(
                    run_id=run["run_id"],
                    conversation_id=run["conversation_id"],
                    owner_id=run["owner_id"],
                    trigger_message_id=run["trigger_message_id"],
                    state=run["state"],
                    config_snapshot=run.get("config_snapshot") or {},
                    cancel_requested=run.get("cancel_requested", False),
                    result_text=run.get("result_text", ""),
                    usage=run.get("usage") or {},
                ).public()
                for run in row.get("runs", [])
            ]
            return {"ok": True, "conversation": conversation}
        except ApplicationError:
            raise
        except Exception as exc:
            raise self._translate(exc) from None

    def list(self, owner_id: str, *, limit: int = 50) -> dict:
        owner_id = self._owner(owner_id)
        try:
            return {"ok": True, "conversations": self.repository.list_conversations(owner_id, limit=limit)}
        except Exception as exc:
            raise self._translate(exc) from None

    @staticmethod
    def _turn_response(owner_id, conversation_id, result):
        run = Run(
            run_id=result["run"]["run_id"],
            conversation_id=result["run"]["conversation_id"],
            owner_id=owner_id,
            trigger_message_id=result["run"]["trigger_message_id"],
            state=result["run"]["state"],
            config_snapshot=result["run"]["config_snapshot"],
            cancel_requested=result["run"]["cancel_requested"],
            result_text=result["run"].get("result_text", ""),
        )
        return {
            "ok": True,
            "created": result["created"],
            "conversation_id": conversation_id,
            "message_id": result["message"]["message_id"],
            "run": run.public(),
        }

    def turn(self, owner_id: str, conversation_id: str, *, text: str, client_token: str,
             overrides: Mapping | None = None, memory_context: Mapping | None = None) -> dict:
        owner_id = self._owner(owner_id)
        try:
            turn = UserTurn.from_input(text, client_token)
            conversation = self.repository.get_conversation(owner_id, conversation_id)
            if conversation is None:
                raise ApplicationError("conversation_not_found", "对话不存在", 404)
            # Reconcile an already accepted token before consulting mutable
            # model/workspace/memory catalogs. A retry must return the frozen
            # Run even after its model is disabled or its memory is edited.
            prior = next((message for message in conversation.get("messages", [])
                          if message.get("client_token") == turn.client_token), None)
            if prior is not None:
                if prior["content"] != turn.text:
                    raise ApplicationError("idempotency_conflict", "幂等键对应的请求内容不同", 409)
                run = next((item for item in conversation.get("runs", [])
                            if item["trigger_message_id"] == prior["message_id"]), None)
                if run is None:
                    raise ApplicationError("run_not_found", "运行不存在", 404)
                return self._turn_response(owner_id, conversation_id, {
                    "created": False, "message": prior, "run": run,
                })
            workspace = None
            if conversation.get("workspace_id"):
                workspace = self.repository.get_workspace(owner_id, conversation["workspace_id"])
            # Workspace defaults are represented by the node binding in the
            # workspace row; DefaultsService validates all references against
            # the owner-scoped catalogs before the snapshot is committed.
            workspace_defaults = {}
            if workspace:
                workspace_defaults = {
                    "workspace_id": workspace["workspace_id"],
                    "execution_node_id": workspace.get("default_node_id"),
                }
            snapshot = self.defaults.resolve_run_config(
                owner_id,
                conversation=conversation.get("overrides") or {},
                workspace=workspace_defaults,
                overrides=overrides or {},
            )
            if memory_context is not None:
                if not isinstance(memory_context, Mapping):
                    raise ApplicationError("invalid_memory_context", "memory_context 必须是 JSON 对象", 400)
                if type(memory_context.get("enabled", False)) is not bool:
                    raise ApplicationError("invalid_memory_context", "memory_context.enabled 不合法", 400)
                if self.memory_context is None:
                    if memory_context.get("enabled"):
                        raise ApplicationError("memory_context_disabled", "记忆上下文未启用", 409)
                    resolved_context = {
                        "enabled": False, "mode": "none", "max_items": 0,
                        "max_bytes": 0, "items": [], "item_count": 0, "bytes": 0,
                    }
                else:
                    resolved_context = self.memory_context.resolve(owner_id, memory_context)
                snapshot["memory_context"] = resolved_context
            result = self.repository.append_turn(
                owner_id, conversation_id, _opaque("msg"), _opaque("run"),
                text=turn.text, client_token=turn.client_token,
                config_snapshot=snapshot, now=float(self.clock()),
            )
            return self._turn_response(owner_id, conversation_id, result)
        except ApplicationError:
            raise
        except PlatformMemoryContextError as exc:
            raise self._translate(exc) from None
        except Exception as exc:
            raise self._translate(exc) from None


class RunService:
    def __init__(self, repository, *, clock=time.time):
        self.repository = repository
        self.clock = clock

    def get(self, owner_id: str, run_id: str) -> dict:
        try:
            owner_id = validate_owner_id(owner_id)
            row = self.repository.get_run(owner_id, validate_id(run_id, "run_id"))
            if row is None:
                raise ApplicationError("run_not_found", "运行不存在", 404)
            return Run(
                run_id=row["run_id"], conversation_id=row["conversation_id"],
                owner_id=row["owner_id"], trigger_message_id=row["trigger_message_id"],
                state=row["state"], config_snapshot=row["config_snapshot"],
                cancel_requested=row["cancel_requested"],
                result_text=row.get("result_text", ""),
                usage=row.get("usage", {}),
            ).public() | {"ok": True}
        except ApplicationError:
            raise
        except Exception as exc:
            raise ConversationService._translate(exc) from None

    def cancel(self, owner_id: str, run_id: str) -> dict:
        try:
            owner_id = validate_owner_id(owner_id)
            row = self.repository.cancel_run(owner_id, validate_id(run_id, "run_id"), now=float(self.clock()))
            return Run(
                run_id=row["run_id"], conversation_id=row["conversation_id"],
                owner_id=row["owner_id"], trigger_message_id=row["trigger_message_id"],
                state=row["state"], config_snapshot=row["config_snapshot"],
                cancel_requested=row["cancel_requested"],
                result_text=row.get("result_text", ""),
                usage=row.get("usage", {}),
            ).public() | {"ok": True}
        except Exception as exc:
            if isinstance(exc, ApplicationError):
                raise
            raise ConversationService._translate(exc) from None


__all__ = ["ConversationService", "RunService"]
