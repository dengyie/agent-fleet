"""Application service for platform defaults and capability catalogs."""
from __future__ import annotations

from collections.abc import Mapping

from hub.application.task_service import ApplicationError
from hub.domain.platform import resolve_run_config
from platform_schema import PlatformValidationError, public_model, validate_owner_id
from hub.infrastructure.platform_db import PlatformRepositoryError


class DefaultsService:
    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def _owner(owner_id: str) -> str:
        try:
            return validate_owner_id(owner_id)
        except PlatformValidationError as exc:
            raise ApplicationError(exc.code, exc.detail, 400) from None

    @staticmethod
    def _translate(exc: Exception) -> ApplicationError:
        code = getattr(exc, "code", "platform_store")
        status = {
            "revision_conflict": 409,
            "reference_forbidden": 409,
            "invalid_revision": 400,
            "invalid_id": 400,
            "invalid_owner": 400,
            "invalid_value": 400,
            "value_too_large": 413,
        }.get(code, 503)
        details = {
            "revision_conflict": "默认配置已更新，请重新读取后再保存",
            "reference_forbidden": "引用的模型、工作区或节点不可用",
            "invalid_revision": "revision 不合法",
            "platform_store": "平台存储不可用",
        }
        return ApplicationError(code, details.get(code, "平台配置不可用"), status)

    def get_defaults(self, owner_id: str) -> dict:
        owner_id = self._owner(owner_id)
        try:
            defaults = self.repository.get_defaults(owner_id)
            return {
                "ok": True,
                "defaults": defaults,
                "models": [public_model(row) for row in self.repository.list_models(owner_id)],
                "workspaces": self.repository.list_workspaces(owner_id),
                "nodes": self.repository.list_nodes(owner_id),
            }
        except ApplicationError:
            raise
        except Exception as exc:
            raise self._translate(exc) from None

    def list_models(self, owner_id: str) -> dict:
        owner_id = self._owner(owner_id)
        try:
            return {"ok": True, "models": [public_model(row) for row in self.repository.list_models(owner_id)]}
        except Exception as exc:
            raise self._translate(exc) from None

    def update_defaults(self, owner_id: str, values: Mapping, expected_revision: int) -> dict:
        owner_id = self._owner(owner_id)
        if not isinstance(values, Mapping):
            raise ApplicationError("invalid_json", "请求体必须是 JSON 对象", 400)
        try:
            return {"ok": True, "defaults": self.repository.update_defaults(owner_id, values, expected_revision)}
        except Exception as exc:
            raise self._translate(exc) from None

    def resolve_run_config(self, owner_id: str, *, conversation=None, workspace=None, overrides=None) -> dict:
        owner_id = self._owner(owner_id)
        try:
            owner = self.repository.get_defaults(owner_id)
            snapshot = resolve_run_config(owner_id, owner=owner, conversation=conversation, workspace=workspace, overrides=overrides)
            # Snapshot references are checked against the same owner's enabled
            # catalog before a worker can consume them.
            catalogs = {
                "model_profile_id": {row["profile_id"] for row in self.repository.list_models(owner_id) if row["enabled"]},
                "workspace_id": {row["workspace_id"] for row in self.repository.list_workspaces(owner_id) if row["enabled"]},
                "execution_node_id": {row["node_id"] for row in self.repository.list_nodes(owner_id) if row["enabled"]},
            }
            for key, value in (("model_profile_id", snapshot.model_profile_id), ("workspace_id", snapshot.workspace_id), ("execution_node_id", snapshot.execution_node_id)):
                if value is not None and value not in catalogs[key]:
                    raise ApplicationError("reference_forbidden", "运行配置引用了不可用资源", 409)
            result = snapshot.as_dict()
            # Freeze non-secret provider inputs with the Run.  The secret
            # reference itself stays in the owner-scoped catalog and is
            # resolved only by the runtime SecretBroker; it never enters the
            # durable snapshot or public DTO.
            if snapshot.model_profile_id is not None:
                profile = self.repository.get_model(owner_id, snapshot.model_profile_id)
                if not profile or not profile.get("enabled"):
                    raise ApplicationError("reference_forbidden", "运行配置引用了不可用模型", 409)
                result["provider_snapshot"] = {
                    "provider": profile.get("provider"),
                    "model": profile.get("model"),
                    "provider_config": dict(profile.get("provider_config") or {}),
                    "capabilities": dict(profile.get("capabilities") or {}),
                }
            return result
        except ApplicationError:
            raise
        except PlatformValidationError as exc:
            raise ApplicationError(exc.code, exc.detail, 400) from None
        except Exception as exc:
            raise self._translate(exc) from None


__all__ = ["DefaultsService"]
