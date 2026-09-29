"""Controlled service actions with approval and version fencing."""
from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Mapping

from hub.application.task_service import ApplicationError
from hub.domain.platform_command import PlatformCommand, args_hash
from hub.infrastructure.approval_repository import ApprovalRepository, ApprovalRepositoryError
from hub.infrastructure.service_repository import ServiceRepositoryError
from platform_schema import validate_id, validate_owner_id


class ServiceActionService:
    """Issue only adapter-owned service commands.

    The service definition is the policy boundary. User supplied arguments are
    intentionally rejected; the node receives only a fixed adapter/alias/action
    tuple derived from the registered definition.
    """

    def __init__(self, service_repository, approval_repository, delivery,
                 command_repository, *, clock=time.time, approval_ttl_s=600.0,
                 command_ttl_s=900.0):
        self.service_repository = service_repository
        self.approval_repository = approval_repository
        self.delivery = delivery
        self.command_repository = command_repository
        self.clock = clock
        self.approval_ttl_s = max(30.0, min(float(approval_ttl_s), 3600.0))
        self.command_ttl_s = max(60.0, min(float(command_ttl_s), 3600.0))

    @staticmethod
    def _owner(owner_id):
        try:
            return validate_owner_id(owner_id)
        except ValueError:
            raise ApplicationError("invalid_owner", "owner_id 不合法", 400) from None

    @staticmethod
    def _translate(exc):
        code = getattr(exc, "code", "service_action_store")
        status = {
            "service_not_found": 404, "unsupported_action": 400,
            "action_not_allowed": 409, "approval_required": 409,
            "grant_not_found": 404, "grant_not_pending": 409,
            "grant_not_usable": 409, "approval_expired": 409,
            "service_version_conflict": 409, "grant_state_conflict": 409,
            "idempotency_conflict": 409, "command_conflict": 409,
            "invalid_idempotency_key": 400,
            "command_store": 503, "approval_store": 503,
        }.get(code, 400)
        details = {
            "service_not_found": "服务不存在",
            "action_not_allowed": "服务未声明该动作",
            "approval_required": "该动作需要审批",
            "grant_not_found": "授权不存在",
            "grant_not_pending": "授权已处理",
            "grant_not_usable": "授权不可消费",
            "approval_expired": "授权已过期",
            "service_version_conflict": "服务定义已变化，请重新授权",
            "idempotency_conflict": "授权幂等键冲突",
            "command_enqueue_failed": "命令入队失败",
        }
        return ApplicationError(code, details.get(code, "服务动作请求不合法"), status)

    @staticmethod
    def _args(service, action):
        return {
            "adapter": service["adapter"],
            "target_alias": service["target_alias"],
            "service_version": int(service["version"]),
            "action": action,
        }

    @staticmethod
    def _public_grant(row):
        return ApprovalRepository._public(row)

    def _service(self, owner_id, service_id):
        try:
            service_id = validate_id(service_id, "service_id")
        except ValueError:
            raise ApplicationError("invalid_service", "服务标识不合法", 400) from None
        service = self.service_repository.get_service(owner_id, service_id)
        if service is None or not service.get("enabled", True):
            raise ServiceRepositoryError("service_not_found")
        return service

    def request(self, owner_id: str, service_id: str, action: str, *,
                arguments: Mapping | None = None, idempotency_key: str | None = None):
        owner_id = self._owner(owner_id)
        if arguments not in (None, {}):
            raise ApplicationError("invalid_arguments", "服务动作参数必须为空", 400)
        if action not in {"inspect", "restart"}:
            raise ApplicationError("unsupported_action", "服务动作不支持", 400)
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 200):
            raise ApplicationError("invalid_idempotency_key", "幂等键不合法", 400)
        try:
            service = self._service(owner_id, service_id)
            if action not in service.get("allowed_actions", []):
                raise ApprovalRepositoryError("action_not_allowed")
            fixed = self._args(service, action)
            digest = args_hash(fixed)
            if action == "restart":
                grant = self.approval_repository.create(
                    owner_id, actor=owner_id, service=service, action=action,
                    arguments=fixed, args_hash=digest,
                    policy_version=f"service-v{service['version']}",
                    expires_at=float(self.clock()) + self.approval_ttl_s,
                    idempotency_key=idempotency_key,
                )
                return {"ok": True, "approval_required": True,
                        "grant": self._public_grant(grant)}
            command_id = "cmd_" + secrets.token_urlsafe(18)
            command = PlatformCommand.create(
                command_id=command_id, target_node=service["node_id"],
                action=f"service.{action}", resource_id=service["service_id"],
                arguments=fixed, retry_class="read_only",
                expires_at=float(self.clock()) + self.command_ttl_s,
                owner_id=owner_id,
            )
            row = self.delivery.enqueue(command, idempotency_key=idempotency_key)
            return {"ok": True, "command": self._command_public(row)}
        except ApplicationError:
            raise
        except (ServiceRepositoryError, ApprovalRepositoryError) as exc:
            raise self._translate(exc) from None

    @staticmethod
    def _command_public(row):
        return {key: row.get(key) for key in (
            "command_id", "target_node", "action", "resource_id",
            "retry_class", "expires_at", "args_hash", "grant_id",
            "status",
        )}

    def get_grant(self, owner_id: str, grant_id: str):
        owner_id = self._owner(owner_id)
        try:
            try:
                grant_id = validate_id(grant_id, "grant_id")
            except ValueError:
                raise ApplicationError("invalid_grant", "授权标识不合法", 400) from None
            grant = self.approval_repository.get(owner_id, grant_id)
            if grant is None:
                raise ApprovalRepositoryError("grant_not_found")
            return {"ok": True, "grant": self._public_grant(grant)}
        except ApprovalRepositoryError as exc:
            raise self._translate(exc) from None

    def decide(self, owner_id: str, grant_id: str, *, approve: bool):
        owner_id = self._owner(owner_id)
        try:
            try:
                grant_id = validate_id(grant_id, "grant_id")
            except ValueError:
                raise ApplicationError("invalid_grant", "授权标识不合法", 400) from None
            grant = self.approval_repository.get(owner_id, grant_id)
            if grant is None:
                raise ApprovalRepositoryError("grant_not_found")
            if not approve:
                if grant["state"] != "pending":
                    return {"ok": True, "grant": self._public_grant(grant)}
                return {"ok": True, "grant": self._public_grant(
                    self.approval_repository.decide(owner_id, grant_id, approve=False))}
            command_id = "cmd_" + hashlib.sha256(grant_id.encode("utf-8")).hexdigest()[:48]
            if grant["state"] in {"consuming", "consumed"}:
                existing = self.command_repository.get(command_id)
                if existing is not None and existing.get("owner_id") == owner_id and existing.get("grant_id") == grant_id:
                    return {"ok": True, "grant": self._public_grant(grant),
                            "command": self._command_public(existing)}
                if grant["state"] == "consumed":
                    raise ApprovalRepositoryError("grant_not_usable")
            if grant["state"] not in {"pending", "approved"}:
                raise ApprovalRepositoryError("grant_not_pending")
            service = self._service(owner_id, grant["service_id"])
            if int(service["version"]) != int(grant["service_version"]):
                raise ApprovalRepositoryError("service_version_conflict")
            if grant["action"] not in service.get("allowed_actions", []):
                raise ApprovalRepositoryError("action_not_allowed")
            fixed = self._args(service, grant["action"])
            if args_hash(fixed) != grant["args_hash"]:
                raise ApprovalRepositoryError("grant_state_conflict")
            # Move pending -> approved before the one-time consume transition.
            # The repository performs this under an immediate transaction so a
            # concurrent approval cannot enqueue two commands.
            if grant["state"] == "pending":
                grant = self.approval_repository.decide(owner_id, grant_id, approve=True)
            existing = self.command_repository.get(command_id)
            if existing is not None:
                if existing.get("owner_id") != owner_id or existing.get("grant_id") != grant_id:
                    raise ApprovalRepositoryError("grant_state_conflict")
                if grant["state"] in {"approved", "consuming"}:
                    grant = self.approval_repository.finish_consume(
                        owner_id, grant_id, command_id=command_id, success=True)
                return {"ok": True, "grant": self._public_grant(grant),
                        "command": self._command_public(existing)}
            consuming = self.approval_repository.begin_consume(
                owner_id, grant_id, command_id=command_id)
            command = PlatformCommand.create(
                command_id=command_id, target_node=service["node_id"],
                action=f"service.{grant['action']}", resource_id=service["service_id"],
                arguments=fixed, retry_class="manual_only",
                expires_at=min(float(grant["expires_at"]), float(self.clock()) + self.command_ttl_s),
                grant_id=grant_id, owner_id=owner_id,
            )
            try:
                row = self.delivery.enqueue(command, idempotency_key=f"grant:{grant_id}")
            except Exception:
                self.approval_repository.finish_consume(
                    owner_id, grant_id, command_id=command_id, success=False)
                raise
            consumed = self.approval_repository.finish_consume(
                owner_id, grant_id, command_id=command_id, success=True)
            return {"ok": True, "grant": self._public_grant(consumed),
                    "command": self._command_public(row)}
        except ApplicationError:
            raise
        except (ServiceRepositoryError, ApprovalRepositoryError) as exc:
            raise self._translate(exc) from None


__all__ = ["ServiceActionService"]
