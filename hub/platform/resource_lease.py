"""In-process workspace write lease with explicit epochs."""
from __future__ import annotations

import threading
import time


class ResourceLeaseError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ResourceLeaseManager:
    def __init__(self, *, clock=time.time):
        self.clock = clock
        self._lock = threading.Lock()
        self._leases: dict[str, dict] = {}

    def acquire(self, resource_id: str, owner_id: str, *, ttl_s: float = 60.0) -> dict:
        now = float(self.clock())
        with self._lock:
            current = self._leases.get(resource_id)
            if current and current["expires_at"] > now and current["owner_id"] != owner_id:
                raise ResourceLeaseError("workspace_busy")
            epoch = int(current["epoch"] + 1) if current else 1
            lease = {"resource_id": resource_id, "owner_id": owner_id, "epoch": epoch, "expires_at": now + max(1.0, float(ttl_s))}
            self._leases[resource_id] = lease
            return dict(lease)

    def release(self, resource_id: str, owner_id: str, epoch: int) -> None:
        with self._lock:
            current = self._leases.get(resource_id)
            if not current or current["owner_id"] != owner_id or current["epoch"] != epoch:
                raise ResourceLeaseError("lease_mismatch")
            self._leases.pop(resource_id, None)

    def renew(self, resource_id: str, owner_id: str, epoch: int, *, ttl_s: float = 60.0) -> dict:
        now = float(self.clock())
        with self._lock:
            current = self._leases.get(resource_id)
            if (not current or current["owner_id"] != owner_id
                    or current["epoch"] != epoch
                    or current["expires_at"] <= now):
                raise ResourceLeaseError("lease_mismatch")
            current = dict(current)
            current["expires_at"] = now + max(1.0, float(ttl_s))
            self._leases[resource_id] = current
            return dict(current)

    def validate(self, resource_id: str, owner_id: str, epoch: int) -> bool:
        current = self._leases.get(resource_id)
        return bool(current and current["owner_id"] == owner_id and current["epoch"] == epoch and current["expires_at"] > float(self.clock()))


__all__ = ["ResourceLeaseError", "ResourceLeaseManager"]
