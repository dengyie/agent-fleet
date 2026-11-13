"""In-process workspace execution leases with capacity and unique epochs.

Durable scheduler slots own cross-process admission. These leases fence each
admitted execution's tool calls without imposing a second exclusive lock.
"""
from __future__ import annotations

import threading
import time


class ResourceLeaseError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ResourceLeaseManager:
    def __init__(self, *, clock=time.time, capacity: int = 1):
        self.clock = clock
        self._lock = threading.Lock()
        self._leases: dict[tuple[str, str], dict] = {}
        self._epoch = 0
        self.set_capacity(capacity)

    def set_capacity(self, capacity: int) -> None:
        if type(capacity) is not int or not 1 <= capacity <= 64:
            raise ValueError("invalid_workspace_capacity")
        with self._lock:
            counts: dict[str, int] = {}
            now = float(self.clock())
            for lease in self._leases.values():
                if lease["expires_at"] > now:
                    resource = lease["resource_id"]
                    counts[resource] = counts.get(resource, 0) + 1
            if any(count > capacity for count in counts.values()):
                raise ResourceLeaseError("workspace_busy")
            self.capacity = capacity

    def acquire(self, resource_id: str, owner_id: str, *, ttl_s: float = 60.0) -> dict:
        now = float(self.clock())
        with self._lock:
            for key, current in list(self._leases.items()):
                if current["expires_at"] <= now:
                    del self._leases[key]
            key = (resource_id, owner_id)
            occupied = sum(lease["resource_id"] == resource_id
                           for lease in self._leases.values())
            if key not in self._leases and occupied >= self.capacity:
                raise ResourceLeaseError("workspace_busy")
            self._epoch += 1
            lease = {"resource_id": resource_id, "owner_id": owner_id, "epoch": self._epoch, "expires_at": now + max(1.0, float(ttl_s))}
            self._leases[key] = lease
            return dict(lease)

    def release(self, resource_id: str, owner_id: str, epoch: int) -> None:
        with self._lock:
            key = (resource_id, owner_id)
            current = self._leases.get(key)
            if not current or current["owner_id"] != owner_id or current["epoch"] != epoch:
                raise ResourceLeaseError("lease_mismatch")
            self._leases.pop(key, None)

    def renew(self, resource_id: str, owner_id: str, epoch: int, *, ttl_s: float = 60.0) -> dict:
        now = float(self.clock())
        with self._lock:
            key = (resource_id, owner_id)
            current = self._leases.get(key)
            if (not current or current["owner_id"] != owner_id
                    or current["epoch"] != epoch
                    or current["expires_at"] <= now):
                raise ResourceLeaseError("lease_mismatch")
            current = dict(current)
            current["expires_at"] = now + max(1.0, float(ttl_s))
            self._leases[key] = current
            return dict(current)

    def validate(self, resource_id: str, owner_id: str, epoch: int) -> bool:
        with self._lock:
            current = self._leases.get((resource_id, owner_id))
            return bool(current and current["epoch"] == epoch
                        and current["expires_at"] > float(self.clock()))


__all__ = ["ResourceLeaseError", "ResourceLeaseManager"]
