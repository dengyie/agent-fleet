"""Private Run event and state application facade."""
from __future__ import annotations

import time

from hub.application.task_service import ApplicationError
from platform_schema import validate_owner_id


class RunEventService:
    def __init__(self, repository, *, clock=time.time):
        self.repository = repository
        self.clock = clock

    @staticmethod
    def _translate(exc):
        code = getattr(exc, "code", "platform_store")
        status = {"run_not_found": 404, "invalid_cursor": 400, "invalid_event": 400, "invalid_run_state": 400, "lease_mismatch": 409}.get(code, 503)
        detail = {"run_not_found": "运行不存在", "invalid_cursor": "游标不合法", "lease_mismatch": "运行租约已失效", "platform_store": "平台存储不可用"}.get(code, "运行事件不可用")
        return ApplicationError(code, detail, status)

    def append(self, owner_id, run_id, kind, payload=None, *, lease_id=None, worker_id=None):
        try:
            return self.repository.append_run_event(validate_owner_id(owner_id), run_id, kind, payload, now=float(self.clock()), lease_id=lease_id, worker_id=worker_id)
        except Exception as exc:
            raise self._translate(exc) from None

    def list(self, owner_id, run_id, after=0, limit=100):
        try:
            return self.repository.list_run_events(validate_owner_id(owner_id), run_id, after=after, limit=limit)
        except Exception as exc:
            raise self._translate(exc) from None

    def state(self, owner_id, run_id, state, *, result_text="", usage=None, lease_id=None, worker_id=None):
        try:
            if lease_id is not None:
                return self.repository.finish_run(validate_owner_id(owner_id), run_id, lease_id=lease_id, worker_id=worker_id, state=state, now=float(self.clock()), result_text=result_text, usage=usage)
            return self.repository.set_run_state(validate_owner_id(owner_id), run_id, state, now=float(self.clock()), result_text=result_text, usage=usage)
        except Exception as exc:
            raise self._translate(exc) from None


__all__ = ["RunEventService"]
