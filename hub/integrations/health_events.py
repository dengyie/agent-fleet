"""Map external monitor snapshots into platform health evidence."""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Mapping

from hub.integrations.komari import KomariError, KomariNodeSnapshot
from hub.integrations.http_probe import (
    HttpProbeError,
    HttpProbeSnapshot,
    probe_evidence,
)
from hub.infrastructure.incident_repository import IncidentRepositoryError

_HTTP_ERROR_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


def incident_fingerprint(
    service_id: str,
    rule_id: str,
    source: str = "komari",
    *,
    version: int | None = None,
) -> str:
    values = [source, rule_id, service_id]
    if version is not None:
        if type(version) is not int or not 1 <= version <= 1_000_000:
            raise ValueError("invalid service version")
        values.append(version)
    value = json.dumps(values, ensure_ascii=True, separators=(",", ":"))
    return "ifp_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:48]


class IncidentService:
    """Synchronize read-only evidence and incidents for registered services."""

    def __init__(self, health_service, incident_repository, *, node_mapping: Mapping[str, str] | None = None, recovery_required: int = 2, clock=time.time):
        self.health_service = health_service
        self.repository = incident_repository
        self.node_mapping = {str(key): str(value) for key, value in (node_mapping or {}).items() if isinstance(key, str) and isinstance(value, str) and value}
        self.recovery_required = max(1, min(10, int(recovery_required)))
        self.clock = clock
        self._last_observation_at = 0.0

    def _observation_time(self, candidate: float | None = None) -> float:
        value = float(self.clock() if candidate is None else candidate)
        if value <= self._last_observation_at:
            value = self._last_observation_at + 0.000001
        self._last_observation_at = value
        return value

    def _sync_one(self, owner_id: str, service: Mapping[str, Any], snapshot: KomariNodeSnapshot | None, *, source_error: str | None = None) -> dict[str, Any]:
        service_id = service["service_id"]
        rule_id = "komari_node_reachability"
        if source_error:
            state, failure_class = "unknown", "source_unavailable"
            detail = {"collector_status": source_error}
        elif snapshot is None:
            state, failure_class = "unknown", "source_unavailable"
            detail = {"collector_status": "node_missing"}
        elif snapshot.online is False:
            state, failure_class = "unhealthy", "health_unhealthy"
            detail = dict(snapshot.detail) | {"external_node_id": snapshot.external_id}
        elif snapshot.online is True:
            state, failure_class = "healthy", None
            detail = dict(snapshot.detail) | {"external_node_id": snapshot.external_id}
        else:
            state, failure_class = "unknown", "source_unavailable"
            detail = dict(snapshot.detail) | {"external_node_id": snapshot.external_id}
        evidence_seed = json.dumps(
            [
                service_id,
                self.node_mapping.get(service.get("node_id", "")),
                snapshot.observed_at if snapshot else self.clock(),
                state,
                source_error or (snapshot.external_id if snapshot else "node_missing"),
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        evidence_id = "komari_" + hashlib.sha256(evidence_seed.encode()).hexdigest()[:40]
        observed_at = self._observation_time(snapshot.observed_at if snapshot else None)
        evidence = {
            "service_id": service_id, "dimension": "host_reachability",
            "source": "komari", "state": state,
            "observed_at": observed_at,
            "ttl_s": 300.0, "evidence_id": evidence_id, "detail": detail,
        }
        saved = self.health_service.ingest(owner_id, evidence)["evidence"]
        fingerprint = incident_fingerprint(service_id, rule_id)
        if failure_class is not None:
            incident = self.repository.open_or_update(owner_id, {
                "service_id": service_id, "failure_class": failure_class,
                "fingerprint": fingerprint, "evidence_id": saved["evidence_id"],
            }, observed_at=saved["observed_at"], source="komari", state=state, detail=detail, rule_id=rule_id, recovery_required=self.recovery_required)
        else:
            incident = self.repository.recover(owner_id, fingerprint=fingerprint, observed_at=saved["observed_at"], evidence_id=saved["evidence_id"], source="komari", state=state, detail=detail, recovery_required=self.recovery_required)
        return {"service_id": service_id, "evidence": saved, "incident": incident}

    def sync(self, owner_id: str, services: list[Mapping[str, Any]], snapshots: list[KomariNodeSnapshot], *, source_error: str | None = None) -> dict[str, Any]:
        by_external = {snapshot.external_id: snapshot for snapshot in snapshots}
        result = []
        for service in services[:1000]:
            external_id = self.node_mapping.get(service.get("node_id"))
            snapshot = by_external.get(external_id) if external_id else None
            result.append(self._sync_one(owner_id, service, snapshot, source_error=source_error))
        return {"ok": True, "results": result, "source": "komari", "source_error": source_error}

    def sync_from_client(self, owner_id: str, services: list[Mapping[str, Any]], client) -> dict[str, Any]:
        try:
            snapshots = client.fetch_nodes(observed_at=float(self.clock()))
        except KomariError as exc:
            return self.sync(owner_id, services, [], source_error=exc.code)
        return self.sync(owner_id, services, snapshots)

    @staticmethod
    def _http_failure_evidence(
        service_id: str,
        error_code: str,
        *,
        observed_at: float,
        ttl_s: float,
        service_version: int | None = None,
    ) -> dict[str, Any]:
        safe_code = (
            error_code[:120]
            if isinstance(error_code, str)
            and _HTTP_ERROR_CODE_RE.fullmatch(error_code[:120])
            else "source_unavailable"
        )
        if service_version is not None and (
            type(service_version) is not int or not 1 <= service_version <= 1_000_000
        ):
            raise HttpProbeError("invalid_evidence")
        seed_values = [
            service_id, "http_probe_v1", "http_probe_application_health", safe_code, observed_at,
        ]
        if service_version is not None:
            seed_values.append(service_version)
        seed = json.dumps(
            seed_values,
            ensure_ascii=True, separators=(",", ":"),
        )
        detail = {"collector_status": safe_code}
        if service_version is not None:
            detail["service_version"] = service_version
        return {
            "service_id": service_id,
            "dimension": "application_health",
            "source": "http_probe_v1",
            "state": "unknown",
            "observed_at": float(observed_at),
            "ttl_s": float(ttl_s),
            "evidence_id": "http_probe_" + hashlib.sha256(seed.encode()).hexdigest()[:40],
            "detail": detail,
        }

    def _current_http_probe_version(
        self,
        owner_id: str,
        service: Mapping[str, Any],
        expected_service_version: int | None,
    ) -> tuple[bool, int | None]:
        if expected_service_version is None:
            return True, None
        if type(expected_service_version) is not int or not 1 <= expected_service_version <= 1_000_000:
            raise HttpProbeError("invalid_evidence")
        current = self.health_service.repository.get_service(
            owner_id, service["service_id"],
        )
        current_version = None if current is None else int(current["version"])
        return bool(
            current is not None
            and current.get("enabled", True)
            and current_version == expected_service_version
        ), current_version

    def _sync_http_observation(
        self,
        owner_id: str,
        service: Mapping[str, Any],
        evidence: Mapping[str, Any],
        expected_service_version: int | None = None,
    ) -> dict[str, Any]:
        service_id = service["service_id"]
        rule_id = "http_probe_application_health"
        source = "http_probe_v1"
        state = evidence["state"]
        matches, current_version = self._current_http_probe_version(
            owner_id, service, expected_service_version,
        )
        if not matches:
            return {
                "ok": False, "stale_policy": True,
                "service_id": service_id, "service_version": current_version,
            }
        try:
            saved = self.health_service.ingest(
                owner_id,
                dict(evidence),
                expected_service_version=expected_service_version,
            )["evidence"]
        except Exception as exc:
            if getattr(exc, "code", None) == "service_version_conflict":
                current = self.health_service.repository.get_service(
                    owner_id, service_id,
                )
                return {
                    "ok": False, "stale_policy": True,
                    "service_id": service_id,
                    "service_version": None if current is None else int(current["version"]),
                }
            raise
        # The repository performs an atomic version check. Re-read before the
        # Incident write as well so an endpoint rotation between evidence and
        # Incident persistence cannot recover or update the new policy.
        matches, current_version = self._current_http_probe_version(
            owner_id, service, expected_service_version,
        )
        if not matches:
            self._discard_stale_evidence(
                owner_id, service_id, saved["evidence_id"],
                expected_service_version,
            )
            return {
                "ok": False, "stale_policy": True,
                "service_id": service_id, "service_version": current_version,
            }
        try:
            if state == "healthy":
                incident = self.repository.recover(
                    owner_id,
                    fingerprint=incident_fingerprint(
                        service_id, rule_id, source, version=expected_service_version,
                    ),
                    observed_at=saved["observed_at"],
                    evidence_id=saved["evidence_id"],
                    source=source,
                    state=state,
                    detail=saved.get("detail") or {},
                    recovery_required=self.recovery_required,
                    service_id=service_id,
                    expected_service_version=expected_service_version,
                )
            else:
                failure_class = (
                    "source_unavailable" if state == "unknown"
                    else "health_degraded" if state == "degraded"
                    else "health_unhealthy"
                )
                incident = self.repository.open_or_update(
                    owner_id,
                    {
                        "service_id": service_id,
                        "failure_class": failure_class,
                        "fingerprint": incident_fingerprint(
                            service_id, rule_id, source, version=expected_service_version,
                        ),
                        "evidence_id": saved["evidence_id"],
                    },
                    observed_at=saved["observed_at"],
                    source=source,
                    state=state,
                    detail=saved.get("detail") or {},
                    rule_id=rule_id,
                    recovery_required=self.recovery_required,
                    expected_service_version=expected_service_version,
                )
        except IncidentRepositoryError as exc:
            if exc.code == "service_version_conflict":
                current = self.health_service.repository.get_service(
                    owner_id, service_id,
                )
                self._discard_stale_evidence(
                    owner_id, service_id, saved["evidence_id"],
                    expected_service_version,
                )
                return {
                    "ok": False, "stale_policy": True,
                    "service_id": service_id,
                    "service_version": None if current is None else int(current["version"]),
                }
            raise
        return {"service_id": service_id, "evidence": saved, "incident": incident}

    def _discard_stale_evidence(
        self, owner_id: str, service_id: str, evidence_id: Any,
        expected_service_version: int | None,
    ) -> None:
        if expected_service_version is None or not isinstance(evidence_id, str):
            return
        discard = getattr(
            self.health_service.repository, "discard_versioned_evidence", None,
        )
        if discard is None:
            return
        try:
            discard(
                owner_id, service_id, evidence_id,
                expected_service_version=expected_service_version,
            )
        except Exception:
            # The version fence and health aggregation still prevent stale
            # state from affecting the current policy if cleanup is unavailable.
            return

    def sync_http_probe(
        self,
        owner_id: str,
        service: Mapping[str, Any],
        client,
        *,
        observed_at: float | None = None,
        ttl_s: float = 180.0,
        expected_probe_id: str | None = None,
        expected_service_version: int | None = None,
    ) -> dict[str, Any]:
        """Record one stored-policy HTTP observation for one owner/service."""
        timestamp = self._observation_time(observed_at)
        try:
            snapshot = client.fetch()
            if not isinstance(snapshot, HttpProbeSnapshot):
                raise HttpProbeError("invalid_response")
            if expected_probe_id is not None and snapshot.probe_id != expected_probe_id:
                raise HttpProbeError("probe_id_mismatch")
            evidence = probe_evidence(
                snapshot, service["service_id"],
                observed_at=timestamp, ttl_s=ttl_s,
                service_version=expected_service_version,
            )
        except HttpProbeError as exc:
            evidence = self._http_failure_evidence(
                service["service_id"], getattr(exc, "code", "source_unavailable"),
                observed_at=timestamp, ttl_s=ttl_s,
                service_version=expected_service_version,
            )
        except Exception:
            evidence = self._http_failure_evidence(
                service["service_id"], "source_unavailable",
                observed_at=timestamp, ttl_s=ttl_s,
                service_version=expected_service_version,
            )
        return self._sync_http_observation(
            owner_id, service, evidence,
            expected_service_version=expected_service_version,
        )

    def record_http_probe_failure(
        self,
        owner_id: str,
        service: Mapping[str, Any],
        error_code: str,
        *,
        observed_at: float | None = None,
        ttl_s: float = 180.0,
        expected_service_version: int | None = None,
    ) -> dict[str, Any]:
        timestamp = self._observation_time(observed_at)
        evidence = self._http_failure_evidence(
            service["service_id"], error_code,
            observed_at=timestamp, ttl_s=ttl_s,
            service_version=expected_service_version,
        )
        return self._sync_http_observation(
            owner_id, service, evidence,
            expected_service_version=expected_service_version,
        )


__all__ = ["IncidentService", "incident_fingerprint"]
