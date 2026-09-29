"""Service definitions and health evidence contracts for the platform domain."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

from platform_schema import bounded_text, validate_id, validate_owner_id

SERVICE_ADAPTERS = frozenset({"systemd", "supervisor", "docker", "http"})
SERVICE_ACTIONS = frozenset({"inspect", "restart"})
HEALTH_DIMENSIONS = (
    "host_reachability",
    "process_state",
    "application_health",
    "external_availability",
)
HEALTH_STATES = frozenset({
    "healthy", "degraded", "unhealthy", "unknown", "unsupported", "stale",
})


class ServiceValidationError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = str(detail)[:200]
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


def _bounded_alias(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ServiceValidationError("invalid_target_alias", "target_alias 不合法")
    value = value.strip()
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in value):
        raise ServiceValidationError("invalid_target_alias", "target_alias 不合法")
    return value


def validate_service_definition(data: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise ServiceValidationError("invalid_service", "服务定义不合法")
    try:
        service_id = validate_id(data.get("service_id"), "service_id")
        node_id = validate_id(data.get("node_id"), "node_id")
    except ValueError:
        raise ServiceValidationError("invalid_service", "服务定义不合法") from None
    name = bounded_text(data.get("name") or service_id, "name", 120)
    adapter = bounded_text(data.get("adapter"), "adapter", 40)
    if adapter not in SERVICE_ADAPTERS:
        raise ServiceValidationError("unsupported_adapter", "服务采集器不支持")
    target_alias = _bounded_alias(data.get("target_alias"))
    checks = data.get("checks") or {}
    if not isinstance(checks, Mapping):
        raise ServiceValidationError("invalid_checks", "服务检查配置不合法")
    if "http_probe" in checks:
        if adapter != "http":
            raise ServiceValidationError(
                "invalid_http_probe_adapter", "HTTP 探针只适用于 http 服务",
            )
        from hub.integrations.http_probe import HttpProbeError, parse_http_probe_policy
        try:
            policy = parse_http_probe_policy(checks.get("http_probe"))
        except HttpProbeError as exc:
            raise ServiceValidationError(
                "invalid_http_probe_" + exc.code, "HTTP 探针策略不合法",
            ) from None
        checks = dict(checks)
        checks["http_probe"] = {
            "base_url": policy.base_url,
            "path": policy.path,
            "interval_s": policy.interval_s,
            "timeout_s": policy.timeout_s,
            "ttl_s": policy.ttl_s,
            "allow_loopback": policy.allow_loopback,
            "probe_id": policy.probe_id,
            "enabled": policy.enabled,
        }
    control_authority = data.get("control_authority")
    if control_authority is not None:
        control_authority = bounded_text(control_authority, "control_authority", 120)
    allowed_actions = data.get("allowed_actions") or []
    if not isinstance(allowed_actions, (list, tuple, set, frozenset)):
        raise ServiceValidationError("invalid_actions", "服务动作策略不合法")
    normalized_actions = []
    for action in allowed_actions:
        if not isinstance(action, str) or action not in SERVICE_ACTIONS:
            raise ServiceValidationError("unsupported_action", "服务动作不支持")
        if action not in normalized_actions:
            normalized_actions.append(action)
    normalized_actions.sort()
    try:
        version = int(data.get("version", 1))
    except (TypeError, ValueError):
        raise ServiceValidationError("invalid_service", "服务版本不合法") from None
    if version < 1 or version > 1_000_000:
        raise ServiceValidationError("invalid_service", "服务版本不合法")
    return {
        "service_id": service_id,
        "node_id": node_id,
        "name": name,
        "adapter": adapter,
        "target_alias": target_alias,
        "checks": {str(key): value for key, value in checks.items() if isinstance(key, str)},
        "control_authority": control_authority,
        "allowed_actions": normalized_actions,
        "version": version,
        "enabled": bool(data.get("enabled", True)),
    }


def validate_health_evidence(data: Mapping[str, Any], *, now: float) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise ServiceValidationError("invalid_evidence", "健康证据不合法")
    try:
        service_id = validate_id(data.get("service_id"), "service_id")
    except ValueError:
        raise ServiceValidationError("invalid_evidence", "健康证据不合法") from None
    dimension = data.get("dimension")
    if dimension not in HEALTH_DIMENSIONS:
        raise ServiceValidationError("invalid_dimension", "健康维度不合法")
    state = data.get("state")
    if state not in HEALTH_STATES:
        raise ServiceValidationError("invalid_health_state", "健康状态不合法")
    source = data.get("source")
    if not isinstance(source, str) or not source.strip() or len(source) > 120:
        raise ServiceValidationError("invalid_evidence_source", "证据来源不合法")
    evidence_id = data.get("evidence_id") or ""
    if not isinstance(evidence_id, str) or len(evidence_id) > 256 or any(
        ord(char) < 0x20 or ord(char) == 0x7f for char in evidence_id
    ):
        raise ServiceValidationError("invalid_evidence", "证据标识不合法")
    try:
        observed_at = float(data.get("observed_at", now))
        ttl_s = float(data.get("ttl_s", 300.0))
    except (TypeError, ValueError):
        raise ServiceValidationError("invalid_evidence_time", "证据时间不合法") from None
    if (not math.isfinite(observed_at) or not math.isfinite(ttl_s)
            or observed_at < 0 or observed_at > now + 300
            or ttl_s <= 0 or ttl_s > 7 * 24 * 3600):
        raise ServiceValidationError("invalid_evidence_time", "证据时间不合法")
    detail = data.get("detail") or {}
    if not isinstance(detail, Mapping):
        raise ServiceValidationError("invalid_evidence_detail", "证据详情不合法")
    if "service_version" in detail and (
        type(detail["service_version"]) is not int
        or not 1 <= detail["service_version"] <= 1_000_000
    ):
        raise ServiceValidationError("invalid_evidence_detail", "服务版本不合法")
    # Details are diagnostic metadata, never raw command output. The repository
    # applies a serialized size bound before persistence.
    return {
        "service_id": service_id,
        "dimension": dimension,
        "state": state,
        "source": source.strip(),
        "observed_at": observed_at,
        "ttl_s": ttl_s,
        "detail": {str(key): value for key, value in detail.items() if isinstance(key, str)},
        "evidence_id": evidence_id,
    }


@dataclass(frozen=True)
class ServiceDefinition:
    service_id: str
    owner_id: str
    node_id: str
    name: str
    adapter: str
    target_alias: str
    checks: dict[str, Any]
    control_authority: str | None
    allowed_actions: list[str]
    version: int
    enabled: bool


__all__ = [
    "HEALTH_DIMENSIONS", "HEALTH_STATES", "SERVICE_ADAPTERS", "SERVICE_ACTIONS",
    "ServiceDefinition", "ServiceValidationError",
    "validate_health_evidence", "validate_service_definition",
]
