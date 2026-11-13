"""Application service for explicit, owner-scoped MemoryItems."""
from __future__ import annotations

import time
from collections.abc import Mapping

from hub.application.task_service import ApplicationError
from hub.infrastructure.platform_memory_repository import (
    PlatformMemoryRepositoryError,
)
from platform_schema import validate_owner_id


class PlatformMemoryService:
    def __init__(self, repository, *, clock=time.time):
        self.repository = repository
        self.clock = clock

    @staticmethod
    def _owner(owner_id: str) -> str:
        try:
            return validate_owner_id(owner_id)
        except ValueError as exc:
            raise ApplicationError("invalid_owner", "owner_id 不合法", 400) from exc

    @staticmethod
    def _translate(exc: Exception) -> ApplicationError:
        code = getattr(exc, "code", "memory_store")
        status = {
            "memory_not_found": 404,
            "memory_conflict": 409,
            "revision_conflict": 409,
            "memory_search_unavailable": 503,
            "memory_store": 503,
            "memory_store_corrupt": 503,
        }.get(code, 400)
        details = {
            "memory_not_found": "记忆不存在",
            "memory_conflict": "记忆已存在",
            "revision_conflict": "记忆已被更新，请重新读取",
            "memory_search_unavailable": "记忆搜索不可用",
            "memory_store": "记忆存储不可用",
            "memory_store_corrupt": "记忆存储不可用",
        }
        return ApplicationError(code, details.get(code, "记忆参数不合法"), status)

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except PlatformMemoryRepositoryError as exc:
            raise self._translate(exc) from exc

    def create(self, owner_id: str, data: Mapping) -> dict:
        return {"ok": True, "memory": self._call(
            self.repository.create, self._owner(owner_id), data,
            now=float(self.clock()),
        )}

    def get(self, owner_id: str, memory_id: str) -> dict:
        row = self._call(self.repository.get, self._owner(owner_id), memory_id)
        if row is None:
            raise ApplicationError("memory_not_found", "记忆不存在", 404)
        return {"ok": True, "memory": row}

    def list(self, owner_id: str, *, limit: int = 50) -> dict:
        return {"ok": True, "memories": self._call(
            self.repository.list, self._owner(owner_id), limit=limit,
        )}

    def search(self, owner_id: str, query: str, *, limit: int = 20) -> dict:
        return {"ok": True, "memories": self._call(
            self.repository.search, self._owner(owner_id), query, limit=limit,
        )}

    def update(self, owner_id: str, memory_id: str, data: Mapping, *, expected_revision: int) -> dict:
        return {"ok": True, "memory": self._call(
            self.repository.update, self._owner(owner_id), memory_id, data,
            expected_revision=expected_revision, now=float(self.clock()),
        )}

    def delete(self, owner_id: str, memory_id: str, *, expected_revision: int) -> dict:
        deleted = self._call(
            self.repository.delete, self._owner(owner_id), memory_id,
            expected_revision=expected_revision,
        )
        if not deleted:
            raise ApplicationError("memory_not_found", "记忆不存在", 404)
        return {"ok": True, "deleted": True, "memory_id": memory_id}
