"""Owner-scoped inspection and audit-only reconciliation for commands."""
from __future__ import annotations

import secrets
import time

from hub.application.task_service import ApplicationError
from platform_schema import validate_id, validate_owner_id


_OUTCOMES = frozenset({"confirmed_succeeded", "confirmed_failed", "remains_unknown"})
_EVIDENCE_SOURCES = frozenset({"operator", "node_health", "workspace_check"})


class CommandInspectionService:
    def __init__(self, repository, *, clock=time.time):
        self.repository = repository
        self.clock = clock

    @staticmethod
    def _translate(exc):
        code = getattr(exc, "code", "command_store")
        status = {
            "command_not_found": 404,
            "reconcile_requires_unknown": 409,
            "reconciliation_conflict": 409,
        }.get(code, 503)
        detail = {
            "command_not_found": "命令不存在",
            "reconcile_requires_unknown": "只有 unknown 命令可以提交 reconcile",
            "reconciliation_conflict": "reconcile 记录冲突",
            "command_store": "命令存储不可用",
        }.get(code, "命令检查不可用")
        return ApplicationError(code, detail, status)

    @staticmethod
    def _public(row):
        result = row.get("result") if isinstance(row, dict) else None
        safe_result = {}
        if isinstance(result, dict) and isinstance(result.get("reason"), str):
            safe_result["reason"] = result["reason"][:120]
        return {
            key: row.get(key) for key in (
                "command_id", "target_node", "action", "resource_id",
                "run_id", "status", "retry_class", "attempt",
                "created_at", "updated_at",
            )
        } | {"result": safe_result}

    def get(self, owner_id: str, command_id: str):
        try:
            owner_id = validate_owner_id(owner_id)
            command_id = validate_id(command_id, "command_id")
            row = self.repository.get_for_owner(owner_id, command_id)
            if row is None:
                raise ApplicationError("command_not_found", "命令不存在", 404)
            return {"ok": True, "command": self._public(row)}
        except ApplicationError:
            raise
        except Exception as exc:
            raise self._translate(exc) from None

    def list_unknown(self, owner_id: str, *, limit=50):
        try:
            owner_id = validate_owner_id(owner_id)
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                raise ApplicationError("invalid_limit", "limit 不合法", 400) from None
            if limit < 1 or limit > 100:
                raise ApplicationError("invalid_limit", "limit 不合法", 400)
            rows = self.repository.list_unknown_for_owner(owner_id, limit=limit)
            return {"ok": True, "commands": [self._public(row) for row in rows]}
        except ApplicationError:
            raise
        except Exception as exc:
            raise self._translate(exc) from None

    def reconcile(self, owner_id: str, command_id: str, *, actor: str,
                  outcome: str, evidence_source: str, evidence_reference: str):
        try:
            owner_id = validate_owner_id(owner_id)
            command_id = validate_id(command_id, "command_id")
            if not isinstance(actor, str) or not actor or len(actor) > 320:
                raise ApplicationError("invalid_actor", "actor 不合法", 400)
            if outcome not in _OUTCOMES:
                raise ApplicationError("invalid_reconcile", "outcome 不合法", 400)
            if evidence_source not in _EVIDENCE_SOURCES:
                raise ApplicationError("invalid_reconcile", "evidence source 不合法", 400)
            if (not isinstance(evidence_reference, str) or not evidence_reference.strip()
                    or len(evidence_reference) > 256
                    or any(ch in evidence_reference for ch in ("\x00", "\r", "\n"))):
                raise ApplicationError("invalid_reconcile", "evidence reference 不合法", 400)
            record = self.repository.record_reconciliation(
                owner_id, command_id, actor=actor, outcome=outcome,
                evidence_source=evidence_source,
                evidence_reference=evidence_reference.strip(),
                reconciliation_id="rec-" + secrets.token_hex(16),
                now=float(self.clock()),
            )
            # Keep the response explicit about audit-only semantics.
            return {
                "ok": True,
                "reconciliation": {
                    "reconciliation_id": record["reconciliation_id"],
                    "command_id": record["command_id"],
                    "outcome": record["outcome"],
                    "evidence_source": record["evidence_source"],
                    "created_at": record["created_at"],
                    "status_effect": "audit_only",
                },
                "command": self.get(owner_id, command_id)["command"],
            }
        except ApplicationError:
            raise
        except Exception as exc:
            raise self._translate(exc) from None


__all__ = ["CommandInspectionService"]
