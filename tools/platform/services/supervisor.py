from __future__ import annotations

import time
from typing import Any, Callable

from .base import command_evidence


class SupervisorCollector:
    source = "supervisor"

    def collect(self, service_id: str, alias: str, *, timeout_s: float = 5.0, ttl_s: float = 180.0, runner: Callable[..., Any] | None = None, now: Callable[[], float] = time.time) -> dict[str, Any]:
        return command_evidence(
            service_id=service_id, source=self.source, alias=alias,
            argv=("supervisorctl", "status", alias),
            timeout_s=timeout_s, ttl_s=ttl_s, runner=runner, now=now,
        )


__all__ = ["SupervisorCollector"]
