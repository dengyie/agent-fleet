"""Incident contracts derived from service health evidence."""
from __future__ import annotations

from typing import Any, Mapping

from platform_schema import validate_id, validate_owner_id

INCIDENT_STATES = frozenset({"open", "closed"})
INCIDENT_FAILURE_CLASSES = frozenset({
    "health_unhealthy", "health_degraded", "source_unavailable",
})


class IncidentValidationError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = str(detail)[:200]
        super().__init__(code)


def validate_incident_input(data: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise IncidentValidationError("invalid_incident", "事件不合法")
    try:
        service_id = validate_id(data.get("service_id"), "service_id")
    except ValueError:
        raise IncidentValidationError("invalid_incident", "事件服务不合法") from None
    failure_class = data.get("failure_class")
    if failure_class not in INCIDENT_FAILURE_CLASSES:
        raise IncidentValidationError("invalid_failure_class", "事件类型不合法")
    fingerprint = data.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint or len(fingerprint) > 256:
        raise IncidentValidationError("invalid_fingerprint", "事件指纹不合法")
    evidence_id = data.get("evidence_id")
    if evidence_id is not None:
        try:
            evidence_id = validate_id(evidence_id, "evidence_id")
        except ValueError:
            raise IncidentValidationError("invalid_incident", "事件证据不合法") from None
    return {
        "service_id": service_id, "failure_class": failure_class,
        "fingerprint": fingerprint, "evidence_id": evidence_id,
    }


__all__ = ["INCIDENT_FAILURE_CLASSES", "INCIDENT_STATES", "IncidentValidationError", "validate_incident_input"]
