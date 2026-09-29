"""Deterministic, read-only post-checks for ambiguous workspace writes."""
from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping

from hub.application.task_service import ApplicationError
from hub.domain.platform_command import PlatformCommand
from platform_schema import validate_id, validate_owner_id


KIND = "workspace_digest"
POSTCHECK_ACTION = "reconcile.workspace.digest"
SERVICE_KIND = "service_inspect"
SERVICE_POSTCHECK_ACTION = "reconcile.service.inspect"
SERVICE_LOGS_KIND = "service_logs"
SERVICE_LOGS_POSTCHECK_ACTION = "reconcile.service.logs"


class CommandPostcheckService:
    def __init__(self, repository, delivery, *, service_repository=None,
                 service_health=None, clock=time.time):
        self.repository = repository
        self.delivery = delivery
        self.service_repository = service_repository
        self.service_health = service_health
        self.clock = clock

    @staticmethod
    def _translate(exc):
        code = getattr(exc, "code", "command_store")
        status = {
            "command_not_found": 404,
            "reconcile_requires_unknown": 409,
            "postcheck_conflict": 409,
            "postcheck_not_found": 404,
            "idempotency_conflict": 409,
            "command_conflict": 409,
            "service_postcheck_conflict": 409,
            "service_version_conflict": 409,
            "service_not_found": 404,
            "postcheck_not_supported": 409,
        }.get(code, 503)
        detail = {
            "command_not_found": "命令不存在",
            "reconcile_requires_unknown": "只有 unknown 写命令可以执行 post-check",
            "postcheck_conflict": "post-check 状态冲突",
            "postcheck_not_found": "post-check 不存在",
            "idempotency_conflict": "post-check 命令幂等键冲突",
            "command_conflict": "post-check 命令冲突",
            "service_postcheck_conflict": "服务 post-check 状态冲突",
            "service_version_conflict": "服务定义已变化，请重新检查",
            "service_not_found": "服务不存在",
            "postcheck_not_supported": "该命令没有可用的确定性 post-check",
            "command_store": "命令存储不可用",
        }.get(code, "post-check 不可用")
        return ApplicationError(code, detail, status)

    @staticmethod
    def _public(row):
        result = row.get("result") if isinstance(row, dict) else None
        safe_result = {}
        if isinstance(result, Mapping):
            for key in ("path", "sha256", "size", "error_code"):
                if key in result:
                    safe_result[key] = result[key]
        return {
            "command_id": row.get("command_id"),
            "kind": row.get("kind"),
            "check_command_id": row.get("check_command_id"),
            "path": row.get("path"),
            "state": row.get("state"),
            "result": safe_result or None,
            "evidence_id": row.get("evidence_id"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

    @staticmethod
    def _check_command_id(command_id: str) -> str:
        return "pc_" + hashlib.sha256((KIND + ":" + command_id).encode("utf-8")).hexdigest()[:48]

    def _derive(self, owner_id: str, command_id: str):
        original = self.repository.get_for_owner(owner_id, command_id)
        if original is None:
            raise ApplicationError("command_not_found", "命令不存在", 404)
        if original.get("status") != "unknown":
            raise ApplicationError("reconcile_requires_unknown", "只有 unknown 写命令可以执行 post-check", 409)
        if original.get("action") != "tool.workspace.write":
            raise ApplicationError("postcheck_not_supported", "该命令没有可用的确定性 post-check", 409)
        arguments = original.get("arguments")
        if not isinstance(arguments, Mapping) or set(arguments) != {"path", "content"}:
            raise ApplicationError("postcheck_not_supported", "该命令没有可用的确定性 post-check", 409)
        path = arguments.get("path")
        content = arguments.get("content")
        if not isinstance(path, str) or not path or not isinstance(content, str):
            raise ApplicationError("postcheck_not_supported", "该命令没有可用的确定性 post-check", 409)
        expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
        check_id = self._check_command_id(command_id)
        return original, path, expected, check_id

    def _refresh(self, owner_id: str, row: dict):
        if row.get("state") != "pending":
            return row
        check = self.repository.get_for_owner(owner_id, row["check_command_id"])
        if not check or check.get("status") not in {"succeeded", "failed", "unknown", "expired"}:
            return row
        result = check.get("result") if isinstance(check.get("result"), Mapping) else {}
        inner = result.get("result") if isinstance(result.get("result"), Mapping) else result
        observed = dict(inner) if isinstance(inner, Mapping) else {}
        if isinstance(result.get("error_code"), str):
            observed["error_code"] = result["error_code"]
        expected = row.get("expected_sha256")
        if check.get("status") == "succeeded" and observed.get("sha256") == expected:
            state = "matched"
            outcome = "confirmed_succeeded"
        elif check.get("status") == "succeeded":
            state = "mismatch"
            outcome = "remains_unknown"
        else:
            # A failed read proves only that the post-check could not observe
            # the current state; it does not prove whether the original write
            # happened. Keep the original side effect unknown.
            state = "remains_unknown"
            outcome = "remains_unknown"
        # The check command id is deterministic, so the evidence id is too.
        # This makes concurrent/repeated reads converge on one audit record.
        evidence_id = "postcheck-" + row["check_command_id"]
        evidence = {
            key: observed[key] for key in ("path", "sha256", "size")
            if key in observed and isinstance(observed[key], (str, int, float))
        }
        saved = self.repository.finish_postcheck(
            owner_id, row["command_id"], kind=KIND, state=state,
            result=evidence | ({"error_code": observed.get("error_code")}
                               if check.get("status") != "succeeded" and isinstance(observed.get("error_code"), str) else {}),
            evidence_id=evidence_id, now=float(self.clock()),
        )
        self.repository.record_reconciliation(
            owner_id, row["command_id"], actor="postcheck",
            outcome=outcome, evidence_source="workspace_check",
            evidence_reference=evidence_id,
            reconciliation_id=evidence_id, now=float(self.clock()),
        )
        return saved

    @staticmethod
    def _service_check_command_id(command_id: str) -> str:
        return "spc_" + hashlib.sha256((SERVICE_KIND + ":" + command_id).encode("utf-8")).hexdigest()[:48]

    def _derive_service(self, owner_id: str, command_id: str):
        if self.service_repository is None:
            raise ApplicationError("postcheck_not_supported", "服务 post-check 不可用", 409)
        original = self.repository.get_for_owner(owner_id, command_id)
        if original is None:
            raise ApplicationError("command_not_found", "命令不存在", 404)
        if original.get("status") != "unknown":
            raise ApplicationError("reconcile_requires_unknown", "只有 unknown 命令可以执行 post-check", 409)
        if original.get("action") != "service.inspect":
            raise ApplicationError("postcheck_not_supported", "该命令没有可用的服务 post-check", 409)
        args = original.get("arguments")
        required = {"adapter", "target_alias", "service_version", "action"}
        if not isinstance(args, Mapping) or set(args) != required or args.get("action") != "inspect":
            raise ApplicationError("postcheck_not_supported", "该命令参数不是固定服务检查合同", 409)
        service_id = original.get("resource_id")
        service = self.service_repository.get_service(owner_id, service_id)
        if service is None or not service.get("enabled", True):
            raise ApplicationError("service_not_found", "服务不存在", 404)
        try:
            version = int(args.get("service_version"))
        except (TypeError, ValueError):
            raise ApplicationError("postcheck_not_supported", "服务版本不合法", 409) from None
        if (service.get("node_id") != original.get("target_node")
                or service.get("service_id") != service_id
                or service.get("adapter") != args.get("adapter")
                or service.get("target_alias") != args.get("target_alias")
                or int(service.get("version", 0)) != version):
            raise ApplicationError("service_version_conflict", "服务定义已变化，请重新检查", 409)
        if "inspect" not in service.get("allowed_actions", []):
            raise ApplicationError("postcheck_not_supported", "服务未声明 inspect 权限", 409)
        return original, service, version, self._service_check_command_id(command_id)

    @staticmethod
    def _service_public(row):
        result = row.get("result") if isinstance(row, dict) else None
        safe = {}
        if isinstance(result, Mapping):
            for key in ("state", "returncode", "duration_s", "error_code"):
                value = result.get(key)
                if isinstance(value, (str, int, float, bool)):
                    safe[key] = value
        return {
            "command_id": row.get("command_id"), "kind": row.get("kind"),
            "check_command_id": row.get("check_command_id"),
            "service_id": row.get("service_id"), "service_version": row.get("service_version"),
            "state": row.get("state"), "result": safe or None,
            "evidence_id": row.get("evidence_id"),
            "created_at": row.get("created_at"), "updated_at": row.get("updated_at"),
        }

    def _refresh_service(self, owner_id: str, row: dict):
        if row.get("state") != "pending":
            return row
        check = self.repository.get_for_owner(owner_id, row["check_command_id"])
        if not check or check.get("status") not in {"succeeded", "failed", "unknown", "expired"}:
            return row
        # Re-check the current definition at receipt time. A command queued
        # under an older service version must never write health evidence for
        # the newer definition, even when its Node receipt is successful.
        current = self.service_repository.get_service(owner_id, row["service_id"])
        version_matches = bool(
            current and current.get("enabled", True)
            and int(current.get("version", 0)) == int(row["service_version"])
        )
        result = check.get("result") if isinstance(check.get("result"), Mapping) else {}
        observed = result.get("result") if isinstance(result.get("result"), Mapping) else result
        observed = dict(observed) if isinstance(observed, Mapping) else {}
        if isinstance(result.get("error_code"), str):
            observed["error_code"] = result["error_code"]
        observed_state = observed.get("state")
        deterministic_state = observed_state in {"healthy", "unhealthy", "degraded", "unsupported"}
        if not version_matches:
            state, outcome = "remains_unknown", "remains_unknown"
            observed = {"error_code": "service_version_conflict"}
            deterministic_state = False
        elif check.get("status") == "succeeded" and deterministic_state:
            state, outcome = "matched", "confirmed_succeeded"
        else:
            state, outcome = "remains_unknown", "remains_unknown"
        evidence_id = "postcheck-" + row["check_command_id"]
        evidence = {key: observed[key] for key in ("state", "returncode", "duration_s", "error_code")
                    if key in observed and isinstance(observed[key], (str, int, float, bool))}
        if self.service_health is not None and deterministic_state:
            self.service_health.ingest(owner_id, {
                "service_id": row["service_id"], "dimension": "process_state",
                "source": "service_postcheck", "state": observed_state,
                "observed_at": float(self.clock()), "ttl_s": 180.0,
                "evidence_id": evidence_id, "detail": {
                    key: observed[key] for key in ("returncode", "duration_s")
                    if key in observed and isinstance(observed[key], (int, float))
                },
            })
        saved = self.repository.finish_service_postcheck(
            owner_id, row["command_id"], kind=SERVICE_KIND, state=state,
            result=evidence, evidence_id=evidence_id, now=float(self.clock()),
        )
        self.repository.record_reconciliation(
            owner_id, row["command_id"], actor="postcheck", outcome=outcome,
            evidence_source="service_check", evidence_reference=evidence_id,
            reconciliation_id=evidence_id, now=float(self.clock()),
        )
        return saved

    @staticmethod
    def _service_logs_public(row):
        result = row.get("result") if isinstance(row, dict) else None
        safe = {}
        if isinstance(result, Mapping):
            text = result.get("text")
            if isinstance(text, str):
                safe["text"] = text[:16 * 1024]
            for key in ("truncated", "observed_at", "error_code"):
                if isinstance(result.get(key), (str, int, float, bool)):
                    safe[key] = result[key]
            redaction = result.get("redaction")
            if isinstance(redaction, Mapping):
                categories = redaction.get("categories")
                safe["redaction"] = {
                    "replaced": redaction.get("replaced", 0) if isinstance(redaction.get("replaced"), int) else 0,
                    "categories": [str(item)[:64] for item in categories[:32]] if isinstance(categories, list) else [],
                    "uncertain": bool(redaction.get("uncertain", False)),
                }
        return {
            "command_id": row.get("command_id"), "kind": row.get("kind"),
            "check_command_id": row.get("check_command_id"),
            "service_id": row.get("service_id"), "service_version": row.get("service_version"),
            "state": row.get("state"), "result": safe or None,
            "evidence_id": row.get("evidence_id"), "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

    def _derive_service_logs(self, owner_id: str, command_id: str):
        original = self.repository.get_for_owner(owner_id, command_id)
        if self.service_repository is None:
            raise ApplicationError("postcheck_not_supported", "服务日志 post-check 不可用", 409)
        if original is None:
            raise ApplicationError("command_not_found", "命令不存在", 404)
        if original.get("status") != "unknown":
            raise ApplicationError("reconcile_requires_unknown", "只有 unknown 命令可以执行 post-check", 409)
        if original.get("action") != "tool.service.read_logs":
            raise ApplicationError("postcheck_not_supported", "该命令没有可用的服务日志 post-check", 409)
        args = original.get("arguments")
        allowed = {"service_id", "window_s", "max_bytes", "adapter", "target_alias", "service_version"}
        if not isinstance(args, Mapping) or set(args) - allowed:
            raise ApplicationError("postcheck_not_supported", "服务日志参数不是固定合同", 409)
        try:
            service_id = validate_id(args.get("service_id"), "service_id")
            window_s = float(args.get("window_s", 300.0))
            max_bytes = int(args.get("max_bytes", 16 * 1024))
            original_version = int(args["service_version"]) if "service_version" in args else None
        except (TypeError, ValueError):
            raise ApplicationError("postcheck_not_supported", "服务日志边界不合法", 409) from None
        if not (0 < window_s <= 3600) or not (0 < max_bytes <= 64 * 1024):
            raise ApplicationError("postcheck_not_supported", "服务日志边界不合法", 409)
        service = self.service_repository.get_service(owner_id, service_id)
        if service is None or not service.get("enabled", True):
            raise ApplicationError("service_not_found", "服务不存在", 404)
        if (service.get("service_id") != service_id
                or service.get("node_id") != original.get("target_node")
                or ("adapter" in args and service.get("adapter") != args.get("adapter"))
                or ("target_alias" in args and service.get("target_alias") != args.get("target_alias"))
                or (original_version is not None
                    and int(service.get("version", 0)) != original_version)):
            raise ApplicationError("service_version_conflict", "服务定义已变化，请重新检查", 409)
        check_id = "slc_" + hashlib.sha256((SERVICE_LOGS_KIND + ":" + command_id).encode("utf-8")).hexdigest()[:48]
        return original, service, int(service["version"]), window_s, max_bytes, check_id

    @staticmethod
    def _service_logs_fence_matches(current: Mapping | None, original: Mapping | None,
                                    check: Mapping | None, row: Mapping) -> bool:
        """Accept log evidence only for the exact definition that was queued."""
        if not isinstance(current, Mapping) or not current.get("enabled", True):
            return False
        if not isinstance(original, Mapping) or not isinstance(check, Mapping):
            return False
        arguments = check.get("arguments")
        if not isinstance(arguments, Mapping):
            return False
        original_arguments = original.get("arguments")
        if not isinstance(original_arguments, Mapping):
            return False
        try:
            identity_matches = (
                ("adapter" not in original_arguments
                 or original_arguments.get("adapter") == arguments.get("adapter"))
                and ("target_alias" not in original_arguments
                     or original_arguments.get("target_alias") == arguments.get("target_alias"))
                and ("service_version" not in original_arguments
                     or int(original_arguments.get("service_version")) == int(row.get("service_version", 0)))
                and int(current.get("version", 0)) == int(row.get("service_version", 0))
                and int(arguments.get("service_version", 0)) == int(row.get("service_version", 0))
            )
        except (TypeError, ValueError):
            return False
        return (
            current.get("service_id") == row.get("service_id")
            and current.get("node_id") == original.get("target_node") == check.get("target_node")
            and check.get("resource_id") == row.get("service_id")
            and check.get("action") == SERVICE_LOGS_POSTCHECK_ACTION
            and arguments.get("action") == "logs"
            and original_arguments.get("service_id") == row.get("service_id")
            and current.get("adapter") == arguments.get("adapter")
            and current.get("target_alias") == arguments.get("target_alias")
            and identity_matches
        )

    def _refresh_service_logs(self, owner_id: str, row: dict):
        if row.get("state") != "pending":
            return row
        check = self.repository.get_for_owner(owner_id, row["check_command_id"])
        if not check or check.get("status") not in {"succeeded", "failed", "unknown", "expired"}:
            return row
        current = self.service_repository.get_service(owner_id, row["service_id"])
        original = self.repository.get_for_owner(owner_id, row["command_id"])
        version_matches = self._service_logs_fence_matches(current, original, check, row)
        raw = check.get("result") if isinstance(check.get("result"), Mapping) else {}
        observed = raw.get("result") if isinstance(raw.get("result"), Mapping) else raw
        observed = dict(observed) if isinstance(observed, Mapping) else {}
        if not version_matches:
            state, outcome, observed = "remains_unknown", "remains_unknown", {"error_code": "service_version_conflict"}
        elif check.get("status") == "succeeded" and isinstance(observed.get("text"), str):
            state, outcome = "matched", "confirmed_succeeded"
            observed["text"] = observed["text"][:16 * 1024]
        else:
            state, outcome = "remains_unknown", "remains_unknown"
        evidence_id = "postcheck-" + row["check_command_id"]
        evidence = {key: observed[key] for key in ("text", "truncated", "redaction", "observed_at", "error_code") if key in observed}
        saved = self.repository.finish_service_postcheck(
            owner_id, row["command_id"], kind=SERVICE_LOGS_KIND, state=state,
            result=evidence, evidence_id=evidence_id, now=float(self.clock()),
        )
        self.repository.record_reconciliation(
            owner_id, row["command_id"], actor="postcheck", outcome=outcome,
            evidence_source="service_logs_check", evidence_reference=evidence_id,
            reconciliation_id=evidence_id, now=float(self.clock()),
        )
        return saved

    def request(self, owner_id: str, command_id: str):
        try:
            owner_id = validate_owner_id(owner_id)
            command_id = validate_id(command_id, "command_id")
            original, path, expected, check_id = self._derive(owner_id, command_id)
            row = self.repository.create_postcheck(
                owner_id, command_id, kind=KIND, check_command_id=check_id,
                path=path, expected_sha256=expected, now=float(self.clock()),
            )
            if row.get("state") == "pending":
                check_command = PlatformCommand.create(
                    command_id=check_id, target_node=original["target_node"],
                    owner_id=owner_id, action=POSTCHECK_ACTION,
                    resource_id=original["resource_id"], arguments={"path": path},
                    retry_class="read_only",
                    expires_at=float(self.clock()) + 300.0, run_id=original.get("run_id"),
                )
                self.delivery.enqueue(check_command, idempotency_key=check_id)
            return {"ok": True, "postcheck": self._public(self._refresh(owner_id, row))}
        except ApplicationError:
            raise
        except Exception as exc:
            raise self._translate(exc) from None

    def get(self, owner_id: str, command_id: str):
        try:
            owner_id = validate_owner_id(owner_id)
            command_id = validate_id(command_id, "command_id")
            row = self.repository.get_postcheck(owner_id, command_id, kind=KIND)
            if row is None:
                raise ApplicationError("postcheck_not_found", "post-check 不存在", 404)
            return {"ok": True, "postcheck": self._public(self._refresh(owner_id, row))}
        except ApplicationError:
            raise
        except Exception as exc:
            raise self._translate(exc) from None

    def request_service(self, owner_id: str, command_id: str):
        try:
            owner_id = validate_owner_id(owner_id)
            command_id = validate_id(command_id, "command_id")
            original, service, version, check_id = self._derive_service(owner_id, command_id)
            row = self.repository.create_service_postcheck(
                owner_id, command_id, kind=SERVICE_KIND, check_command_id=check_id,
                service_id=service["service_id"], service_version=version,
                now=float(self.clock()),
            )
            if row.get("state") == "pending":
                command = PlatformCommand.create(
                    command_id=check_id, target_node=original["target_node"],
                    owner_id=owner_id, action=SERVICE_POSTCHECK_ACTION,
                    resource_id=service["service_id"], arguments={
                        "adapter": service["adapter"], "target_alias": service["target_alias"],
                        "service_version": version, "action": "inspect",
                    }, retry_class="read_only",
                    expires_at=float(self.clock()) + 300.0, run_id=original.get("run_id"),
                )
                self.delivery.enqueue(command, idempotency_key=check_id)
            return {"ok": True, "postcheck": self._service_public(self._refresh_service(owner_id, row))}
        except ApplicationError:
            raise
        except Exception as exc:
            raise self._translate(exc) from None

    def request_service_logs(self, owner_id: str, command_id: str):
        try:
            owner_id = validate_owner_id(owner_id)
            command_id = validate_id(command_id, "command_id")
            original, service, version, window_s, max_bytes, check_id = self._derive_service_logs(owner_id, command_id)
            current = self.service_repository.get_service(owner_id, service["service_id"])
            if (not isinstance(current, Mapping)
                    or any(current.get(key) != service.get(key)
                           for key in ("service_id", "node_id", "adapter", "target_alias", "version", "enabled"))):
                raise ApplicationError("service_version_conflict", "服务定义已变化，请重新检查", 409)
            row = self.repository.create_service_postcheck(
                owner_id, command_id, kind=SERVICE_LOGS_KIND, check_command_id=check_id,
                service_id=service["service_id"], service_version=version, now=float(self.clock()),
            )
            if row.get("state") == "pending":
                command = PlatformCommand.create(
                    command_id=check_id, target_node=original["target_node"], owner_id=owner_id,
                    action=SERVICE_LOGS_POSTCHECK_ACTION, resource_id=service["service_id"],
                    arguments={"adapter": service["adapter"], "target_alias": service["target_alias"],
                               "service_version": version, "action": "logs",
                               "window_s": window_s, "max_bytes": max_bytes},
                    retry_class="read_only", expires_at=float(self.clock()) + 300.0,
                    run_id=original.get("run_id"),
                )
                self.delivery.enqueue(command, idempotency_key=check_id)
            return {"ok": True, "postcheck": self._service_logs_public(self._refresh_service_logs(owner_id, row))}
        except ApplicationError:
            raise
        except Exception as exc:
            raise self._translate(exc) from None

    def request_any(self, owner_id: str, command_id: str):
        """Dispatch only to a server-selected post-check policy."""
        try:
            owner_id = validate_owner_id(owner_id)
            command_id = validate_id(command_id, "command_id")
            original = self.repository.get_for_owner(owner_id, command_id)
            if original is not None and original.get("action") == "service.inspect":
                return self.request_service(owner_id, command_id)
            if original is not None and original.get("action") == "tool.service.read_logs":
                return self.request_service_logs(owner_id, command_id)
            return self.request(owner_id, command_id)
        except ApplicationError:
            raise
        except Exception as exc:
            raise self._translate(exc) from None

    def get_service(self, owner_id: str, command_id: str):
        try:
            owner_id = validate_owner_id(owner_id)
            command_id = validate_id(command_id, "command_id")
            row = self.repository.get_service_postcheck(owner_id, command_id, kind=SERVICE_KIND)
            if row is None:
                raise ApplicationError("postcheck_not_found", "服务 post-check 不存在", 404)
            return {"ok": True, "postcheck": self._service_public(self._refresh_service(owner_id, row))}
        except ApplicationError:
            raise
        except Exception as exc:
            raise self._translate(exc) from None

    def get_service_logs(self, owner_id: str, command_id: str):
        try:
            owner_id = validate_owner_id(owner_id)
            command_id = validate_id(command_id, "command_id")
            row = self.repository.get_service_postcheck(owner_id, command_id, kind=SERVICE_LOGS_KIND)
            if row is None:
                raise ApplicationError("postcheck_not_found", "服务 post-check 不存在", 404)
            return {"ok": True, "postcheck": self._service_logs_public(self._refresh_service_logs(owner_id, row))}
        except ApplicationError:
            raise
        except Exception as exc:
            raise self._translate(exc) from None

    def get_any(self, owner_id: str, command_id: str):
        try:
            owner_id = validate_owner_id(owner_id)
            command_id = validate_id(command_id, "command_id")
            service_row = self.repository.get_service_postcheck(owner_id, command_id, kind=SERVICE_KIND)
            if service_row is not None:
                return self.get_service(owner_id, command_id)
            logs_row = self.repository.get_service_postcheck(owner_id, command_id, kind=SERVICE_LOGS_KIND)
            if logs_row is not None:
                return self.get_service_logs(owner_id, command_id)
            return self.get(owner_id, command_id)
        except ApplicationError:
            raise
        except Exception as exc:
            raise self._translate(exc) from None


__all__ = ["CommandPostcheckService", "KIND", "POSTCHECK_ACTION", "SERVICE_KIND", "SERVICE_POSTCHECK_ACTION", "SERVICE_LOGS_KIND", "SERVICE_LOGS_POSTCHECK_ACTION"]
