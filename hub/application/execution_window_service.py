"""Operator-facing execution-window control-plane service."""
from __future__ import annotations

from collections.abc import Mapping

from hub.application.task_service import ApplicationError
from hub.infrastructure.execution_window_repository import ExecutionWindowRepositoryError
from platform_schema import validate_id, validate_owner_id


class ExecutionWindowService:
    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def _owner(owner_id):
        try:
            return validate_owner_id(owner_id)
        except ValueError:
            raise ApplicationError("invalid_owner", "owner_id 不合法", 400) from None

    @staticmethod
    def _window_id(window_id):
        try:
            return validate_id(window_id, "window_id")
        except ValueError:
            raise ApplicationError("invalid_window", "窗口标识不合法", 400) from None

    @staticmethod
    def _translate(exc):
        code = getattr(exc, "code", "window_store")
        status = {
            "window_not_found": 404, "run_not_found": 404,
            "window_expired": 409, "window_closed": 409,
            "window_not_attached": 409, "ticket_used": 409,
            "ticket_expired": 409, "lease_conflict": 409,
            "lease_not_found": 409, "lease_expired": 409,
            "invalid_window": 400, "invalid_ticket": 400,
            "invalid_holder": 400, "invalid_lease": 400,
            "invalid_window_ttl": 400, "invalid_ticket_ttl": 400,
            "invalid_lease_ttl": 400, "invalid_metadata": 400,
            "metadata_too_large": 413, "invalid_event_id": 400,
            "invalid_event_kind": 400, "invalid_event_payload": 400,
            "forbidden_event_field": 400, "event_payload_too_large": 413,
            "invalid_cursor": 400, "invalid_run": 400, "event_conflict": 409,
            "frame_not_found": 404, "frame_scope": 409, "frame_dimensions": 415,
            "window_store": 503,
        }.get(code, 400)
        detail = {
            "window_not_found": "执行窗口不存在", "run_not_found": "运行不存在",
            "window_expired": "执行窗口已过期", "window_closed": "执行窗口已关闭",
            "window_not_attached": "执行窗口尚未接入", "ticket_used": "接入票据已使用",
            "ticket_expired": "接入票据已过期", "lease_conflict": "窗口已有写入者",
            "lease_not_found": "写入租约不存在", "lease_expired": "写入租约已过期",
            "invalid_event_id": "事件幂等标识不合法", "invalid_event_kind": "事件类型不受支持",
            "invalid_event_payload": "事件载荷不合法", "forbidden_event_field": "事件载荷包含受限字段",
            "event_payload_too_large": "事件载荷超出限制", "invalid_cursor": "事件游标不合法",
            "invalid_run": "运行标识不合法", "event_conflict": "事件写入冲突",
            "window_store": "执行窗口存储不可用", "frame_not_found": "画面不存在",
            "frame_scope": "画面事件作用域不匹配", "frame_dimensions": "画面尺寸超出限制",
        }.get(code, "执行窗口请求不合法")
        return ApplicationError(code, detail, status)

    def _call(self, fn):
        try:
            return fn()
        except ApplicationError:
            raise
        except ExecutionWindowRepositoryError as exc:
            raise self._translate(exc) from exc

    def create(self, owner_id: str, run_id: str, *, ttl_s=900.0, metadata: Mapping | None = None):
        owner_id = self._owner(owner_id)
        try:
            run_id = validate_id(run_id, "run_id")
        except ValueError:
            raise ApplicationError("invalid_run", "运行标识不合法", 400) from None
        if metadata is not None and not isinstance(metadata, Mapping):
            raise ApplicationError("invalid_metadata", "窗口元数据不合法", 400)
        return self._call(lambda: {"ok": True, **self.repository.create_window(owner_id, run_id, ttl_s=ttl_s, metadata=metadata)})

    def get(self, owner_id, window_id):
        owner_id = self._owner(owner_id); window_id = self._window_id(window_id)
        row = self._call(lambda: self.repository.get_window(owner_id, window_id))
        if row is None:
            raise ApplicationError("window_not_found", "执行窗口不存在", 404)
        return {"ok": True, "window": row}

    def list(self, owner_id, *, run_id=None, limit=20):
        owner_id = self._owner(owner_id)
        if run_id is not None:
            try:
                run_id = validate_id(run_id, "run_id")
            except ValueError:
                raise ApplicationError("invalid_run", "运行标识不合法", 400) from None
        return self._call(lambda: self.repository.list_windows(
            owner_id, run_id=run_id, limit=limit,
        ))

    def attach(self, owner_id, window_id, ticket):
        owner_id = self._owner(owner_id); window_id = self._window_id(window_id)
        return self._call(lambda: {"ok": True, **self.repository.redeem_ticket(owner_id, window_id, ticket)})

    def reconnect(self, owner_id, window_id, *, ttl_s=300.0):
        owner_id = self._owner(owner_id); window_id = self._window_id(window_id)
        return self._call(lambda: {"ok": True, **self.repository.reconnect(owner_id, window_id, ttl_s=ttl_s)})

    def acquire_writer(self, owner_id, window_id, holder_id, *, ttl_s=60.0):
        owner_id = self._owner(owner_id); window_id = self._window_id(window_id)
        return self._call(lambda: {"ok": True, **self.repository.acquire_writer(owner_id, window_id, holder_id, ttl_s=ttl_s)})

    def renew_writer(self, owner_id, window_id, holder_id, lease_token, *, ttl_s=60.0):
        owner_id = self._owner(owner_id); window_id = self._window_id(window_id)
        return self._call(lambda: {"ok": True, **self.repository.renew_writer(owner_id, window_id, holder_id, lease_token, ttl_s=ttl_s)})

    def release_writer(self, owner_id, window_id, holder_id, lease_token):
        owner_id = self._owner(owner_id); window_id = self._window_id(window_id)
        return self._call(lambda: {"ok": True, **self.repository.release_writer(owner_id, window_id, holder_id, lease_token)})

    def close(self, owner_id, window_id):
        owner_id = self._owner(owner_id); window_id = self._window_id(window_id)
        return self._call(lambda: {"ok": True, **self.repository.close_window(owner_id, window_id)})

    def append_event(self, owner_id, window_id, holder_id, lease_token,
                     client_event_id, kind, payload=None):
        owner_id = self._owner(owner_id); window_id = self._window_id(window_id)
        return self._call(lambda: {
            "ok": True,
            "event": self.repository.append_event(
                owner_id, window_id, holder_id, lease_token,
                client_event_id, kind, payload,
            ),
        })

    def list_events(self, owner_id, window_id, *, after=0, limit=100):
        owner_id = self._owner(owner_id); window_id = self._window_id(window_id)
        return self._call(lambda: self.repository.list_events(
            owner_id, window_id, after=after, limit=limit,
        ))

    def get_frame_artifact(self, owner_id, window_id, artifact_id):
        owner_id = self._owner(owner_id); window_id = self._window_id(window_id)
        return self._call(lambda: self.repository.get_frame_artifact(
            owner_id, window_id, artifact_id,
        ))


__all__ = ["ExecutionWindowService"]
