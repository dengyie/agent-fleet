"""Bounded, read-only diagnostic queries for the assistant tool broker."""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

from hub.application.task_service import ApplicationError
from tools.session.redact import Redactor
from platform_schema import validate_id, validate_owner_id

DEFAULT_LOG_WINDOW_S = 300.0
MAX_LOG_WINDOW_S = 3600.0
DEFAULT_LOG_BYTES = 16 * 1024
MAX_LOG_BYTES = 64 * 1024


class DiagnosticService:
    """Read monitoring facts without exposing repository internals."""

    def __init__(self, health_service, incident_repository, *, log_reader: Callable[..., Any] | None = None):
        self.health_service = health_service
        self.incident_repository = incident_repository
        self.log_reader = log_reader
        self.service_repository = health_service.repository
        self._redactor = Redactor(passthrough=False)

    @staticmethod
    def _owner(owner_id: str) -> str:
        try:
            return validate_owner_id(owner_id)
        except ValueError:
            raise ApplicationError("invalid_owner", "owner_id 不合法", 400) from None

    @staticmethod
    def _without_owner(row: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in row.items() if key != "owner_id"}

    @classmethod
    def _health_public(cls, payload: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(payload)
        if isinstance(result.get("service"), Mapping):
            result["service"] = cls._without_owner(result["service"])
        if isinstance(result.get("evidence"), list):
            result["evidence"] = [
                cls._without_owner(item) for item in result["evidence"]
                if isinstance(item, Mapping)
            ][:500]
        return result

    @staticmethod
    def _bounded_number(value: Any, *, default: float, maximum: float, name: str) -> float:
        if value is None:
            return default
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            raise ApplicationError("invalid_" + name, f"{name} 不合法", 400) from None
        if not math.isfinite(parsed) or parsed <= 0 or parsed > maximum:
            raise ApplicationError("invalid_" + name, f"{name} 不合法", 400)
        return parsed

    def list_services(self, owner_id: str) -> dict[str, Any]:
        owner_id = self._owner(owner_id)
        try:
            result = self.health_service.list(owner_id)
        except ApplicationError:
            raise
        except Exception:
            raise ApplicationError("diagnostics_unavailable", "服务证据不可用", 503) from None
        return {
            "ok": True,
            "services": [
                {"service": self._without_owner(item["service"]), "health": item["health"]}
                for item in result.get("services", [])
                if isinstance(item, Mapping) and isinstance(item.get("service"), Mapping)
            ],
        }

    def get_health(self, owner_id: str, service_id: str) -> dict[str, Any]:
        owner_id = self._owner(owner_id)
        try:
            service_id = validate_id(service_id, "service_id")
        except ValueError:
            raise ApplicationError("invalid_id", "service_id 不合法", 400) from None
        try:
            return self._health_public(self.health_service.get(owner_id, service_id))
        except ApplicationError:
            raise
        except Exception:
            raise ApplicationError("diagnostics_unavailable", "服务证据不可用", 503) from None

    def get_incident_evidence(self, owner_id: str, incident_id: str) -> dict[str, Any]:
        owner_id = self._owner(owner_id)
        try:
            row = self.incident_repository.get(owner_id, incident_id)
        except Exception:
            raise ApplicationError("diagnostics_unavailable", "事件证据不可用", 503) from None
        if row is None:
            raise ApplicationError("not_found", "事件不存在", 404)
        try:
            service_id = validate_id(row["service_id"], "service_id")
        except ValueError:
            raise ApplicationError("invalid_id", "service_id 不合法", 400) from None
        try:
            evidence = self.service_repository.list_evidence(owner_id, service_id, limit=500)
        except Exception:
            raise ApplicationError("diagnostics_unavailable", "事件证据不可用", 503) from None
        wanted = set(row.get("evidence_ids") or [])
        if wanted:
            evidence = [item for item in evidence if item.get("evidence_id") in wanted]
        return {
            "ok": True,
            "incident": self._without_owner(row),
            "evidence": [self._without_owner(item) for item in evidence[:100]],
        }

    def read_logs(self, owner_id: str, service_id: str, *, window_s: Any = None, max_bytes: Any = None) -> dict[str, Any]:
        owner_id = self._owner(owner_id)
        try:
            service_id = validate_id(service_id, "service_id")
        except ValueError:
            raise ApplicationError("invalid_id", "service_id 不合法", 400) from None
        window = self._bounded_number(window_s, default=DEFAULT_LOG_WINDOW_S, maximum=MAX_LOG_WINDOW_S, name="window_s")
        byte_limit = self._bounded_number(max_bytes, default=float(DEFAULT_LOG_BYTES), maximum=float(MAX_LOG_BYTES), name="max_bytes")
        if int(byte_limit) != byte_limit:
            raise ApplicationError("invalid_max_bytes", "max_bytes 不合法", 400)
        try:
            service = self.service_repository.get_service(owner_id, service_id)
        except Exception:
            raise ApplicationError("diagnostics_unavailable", "日志证据不可用", 503) from None
        if service is None:
            raise ApplicationError("not_found", "服务不存在", 404)
        if self.log_reader is None:
            raise ApplicationError("diagnostics_unavailable", "日志证据不可用", 503)
        try:
            raw = self.log_reader(service, window_s=float(window), max_bytes=int(byte_limit))
        except Exception:
            raise ApplicationError("diagnostics_unavailable", "日志证据不可用", 503) from None
        if isinstance(raw, Mapping):
            text, truncated, observed_at = raw.get("text", ""), bool(raw.get("truncated", False)), raw.get("observed_at")
        else:
            text, truncated, observed_at = raw, False, None
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        if not isinstance(text, str):
            raise ApplicationError("diagnostics_unavailable", "日志证据不可用", 503)
        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) > int(byte_limit):
            text = encoded[:int(byte_limit)].decode("utf-8", errors="ignore")
            truncated = True
        sanitized, report = self._redactor.redact_event({"payload": {"text": text}})
        safe_text = str(((sanitized.get("payload") or {}).get("text")) or "")
        safe_encoded = safe_text.encode("utf-8", errors="replace")
        if len(safe_encoded) > int(byte_limit):
            safe_text = safe_encoded[:int(byte_limit)].decode("utf-8", errors="ignore")
            truncated = True
        logs: dict[str, Any] = {
            "service_id": service_id, "window_s": window, "text": safe_text,
            "truncated": truncated,
            "redaction": {"replaced": int(report.replaced), "categories": list(report.categories)[:32], "uncertain": bool(report.uncertain)},
        }
        if isinstance(observed_at, (int, float)) and math.isfinite(float(observed_at)):
            logs["observed_at"] = float(observed_at)
        return {"ok": True, "logs": logs}


__all__ = ["DEFAULT_LOG_BYTES", "DEFAULT_LOG_WINDOW_S", "MAX_LOG_BYTES", "MAX_LOG_WINDOW_S", "DiagnosticService"]
