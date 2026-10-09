"""Service catalog and deterministic health aggregation."""
from __future__ import annotations

import time

from hub.application.task_service import ApplicationError
from hub.domain.service import HEALTH_DIMENSIONS, ServiceValidationError
from hub.infrastructure.service_repository import ServiceRepositoryError
from hub.integrations.http_probe import canonical_probe_origin, parse_http_probe_policy
from platform_schema import validate_id, validate_owner_id


class ServiceHealthService:
    HOST_TTL_S = 300.0

    def __init__(
        self,
        repository,
        *,
        clock=time.time,
        probe_allowed_origins=(),
        probe_allow_loopback=False,
    ):
        self.repository = repository
        self.clock = clock
        self.probe_allowed_origins = (
            frozenset(
                canonical_probe_origin(value) for value in probe_allowed_origins
            )
            if probe_allowed_origins is not None else None
        )
        self.probe_allow_loopback = bool(probe_allow_loopback)

    @staticmethod
    def _owner(owner_id: str) -> str:
        try:
            return validate_owner_id(owner_id)
        except ValueError:
            raise ApplicationError("invalid_owner", "owner_id 不合法", 400) from None

    @staticmethod
    def _translate(exc: Exception) -> ApplicationError:
        code = getattr(exc, "code", "service_store")
        status = {
            "reference_forbidden": 409, "service_not_found": 404,
            "evidence_conflict": 409, "invalid_limit": 400,
            "evidence_detail_too_large": 413,
            "invalid_evidence": 400, "invalid_dimension": 400,
            "invalid_health_state": 400, "invalid_evidence_source": 400,
            "invalid_evidence_time": 400, "invalid_evidence_detail": 400,
            "invalid_service": 400, "unsupported_adapter": 400,
            "invalid_target_alias": 400, "invalid_checks": 400,
            "invalid_http_probe_invalid_policy": 400,
            "invalid_http_probe_invalid_endpoint": 400,
            "invalid_http_probe_invalid_interval": 400,
            "invalid_http_probe_invalid_timeout": 400,
            "invalid_http_probe_invalid_ttl": 400,
            "invalid_http_probe_adapter": 400,
            "http_probe_endpoint_not_allowlisted": 409,
            "http_probe_loopback_forbidden": 400,
            "invalid_service_version": 400,
            "service_version_conflict": 409,
            "invalid_http_probe_adapter": 400,
            "invalid_actions": 400, "unsupported_action": 400,
        }.get(code, 503)
        details = {
            "reference_forbidden": "服务节点不可用",
            "service_not_found": "服务不存在",
            "evidence_conflict": "健康证据冲突",
            "service_store": "服务目录不可用",
        }
        return ApplicationError(code, details.get(code, "服务监控请求不合法"), status)

    def register(self, owner_id: str, data: dict) -> dict:
        owner_id = self._owner(owner_id)
        try:
            checks = data.get("checks") if isinstance(data, dict) else None
            if isinstance(checks, dict) and "http_probe" in checks:
                policy = parse_http_probe_policy(checks["http_probe"])
                if (
                    self.probe_allowed_origins is not None
                    and policy.base_url not in self.probe_allowed_origins
                ):
                    raise ServiceRepositoryError("http_probe_endpoint_not_allowlisted")
                if policy.allow_loopback and not self.probe_allow_loopback:
                    raise ServiceRepositoryError("http_probe_loopback_forbidden")
            return {"ok": True, "service": self.repository.upsert_service(owner_id, data)}
        except Exception as exc:
            raise self._translate(exc) from exc

    def _freshness(self, evidence: dict, *, now: float) -> str:
        age = max(0.0, now - float(evidence["observed_at"]))
        if age > float(evidence["ttl_s"]):
            return "stale"
        return "fresh"

    @staticmethod
    def _http_probe_version_matches(evidence: dict, service: dict) -> bool:
        if evidence.get("dimension") != "application_health" or evidence.get("source") != "http_probe_v1":
            return True
        captured = (evidence.get("detail") or {}).get("service_version")
        # M2.10 evidence predates the version field. It remains valid for the
        # initial service definition, but never crosses a later rotation.
        return captured == service.get("version") or (
            captured is None and int(service.get("version", 0)) == 1
        )

    def _summary(self, owner_id: str, service: dict) -> dict:
        now = float(self.clock())
        evidence = self.repository.list_evidence(owner_id, service["service_id"], limit=500)
        latest = {}
        for item in evidence:
            if not self._http_probe_version_matches(item, service):
                continue
            if item["dimension"] not in latest:
                latest[item["dimension"]] = item
        node = self.repository.get_node_status(owner_id, service["node_id"])
        dimensions = {}
        for dimension in HEALTH_DIMENSIONS:
            item = latest.get(dimension)
            if dimension == "host_reachability" and item is None and node is not None:
                last_seen = node.get("last_seen_at")
                if last_seen is not None and now - float(last_seen) <= self.HOST_TTL_S:
                    item = {
                        "evidence_id": None, "source": "node_heartbeat",
                        "state": "degraded" if node.get("status") == "degraded" else "healthy",
                        "observed_at": float(last_seen), "received_at": float(last_seen),
                        "ttl_s": self.HOST_TTL_S, "detail": {},
                    }
            if item is None:
                dimensions[dimension] = {"state": "unknown", "freshness": "missing", "evidence": None}
                continue
            freshness = self._freshness(item, now=now)
            state = "stale" if freshness == "stale" else item["state"]
            dimensions[dimension] = {
                "state": state, "freshness": freshness,
                "evidence": {
                    "evidence_id": item.get("evidence_id"), "source": item["source"],
                    "observed_at": item["observed_at"], "received_at": item["received_at"],
                    "ttl_s": item["ttl_s"], "detail": item.get("detail") or {},
                },
            }
        states = [row["state"] for row in dimensions.values()]
        if any(state == "unhealthy" for state in states):
            overall = "unhealthy"
        elif any(state in {"unknown", "stale", "unsupported"} for state in states):
            overall = "unknown"
        elif any(state == "degraded" for state in states):
            overall = "degraded"
        else:
            overall = "healthy"
        return {"overall": overall, "evaluated_at": now, "dimensions": dimensions}

    def _public(self, owner_id: str, service: dict) -> dict:
        return {"service": service, "health": self._summary(owner_id, service)}

    def list(self, owner_id: str) -> dict:
        owner_id = self._owner(owner_id)
        try:
            return {"ok": True, "services": [self._public(owner_id, row) for row in self.repository.list_services(owner_id)]}
        except Exception as exc:
            raise self._translate(exc) from exc

    def get(self, owner_id: str, service_id: str) -> dict:
        owner_id = self._owner(owner_id)
        try:
            service = self.repository.get_service(owner_id, validate_id(service_id, "service_id"))
            if service is None:
                raise ServiceRepositoryError("service_not_found")
            evidence = self.repository.list_evidence(owner_id, service_id, limit=100)
            return {"ok": True, **self._public(owner_id, service), "evidence": evidence}
        except Exception as exc:
            raise self._translate(exc) from exc

    def ingest(
        self,
        owner_id: str,
        evidence: dict,
        *,
        node_id: str | None = None,
        expected_service_version: int | None = None,
    ) -> dict:
        owner_id = self._owner(owner_id)
        try:
            service_id = validate_id(evidence.get("service_id"), "service_id")
            service = self.repository.get_service(owner_id, service_id)
            if service is None:
                raise ServiceRepositoryError("service_not_found")
            if node_id is not None and service["node_id"] != node_id:
                raise ServiceRepositoryError("reference_forbidden")
            saved = self.repository.record_evidence(
                owner_id,
                evidence,
                expected_service_version=expected_service_version,
            )
            return {"ok": True, "evidence": saved}
        except Exception as exc:
            raise self._translate(exc) from exc

    def node_evidence(self, owner_id: str, node_id: str, evidence: dict) -> dict:
        return self.ingest(owner_id, evidence, node_id=validate_id(node_id, "node_id"))

__all__ = ["ServiceHealthService"]
