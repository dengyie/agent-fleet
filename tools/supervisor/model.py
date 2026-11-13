"""tools/supervisor/model.py — 受管会话 manifest 与不透明 ID（Task 8 scope).

``ManagedSessionManifest`` 是 Supervisor 为每个受管会话维护的本地权威记录：

- 不透明 ``session_id`` / ``attempt_id`` / ``process_group_id`` 绑定；
- agent family（``agent``）、机器（``machine_id``）；
- process-group capability 与 capability manifest（由 Task 2 probe 产生）；
- 有界状态机状态（``launching`` -> ``running`` -> ``paused`` /
  ``quarantined`` / ``terminated``）。

持久化与恢复只写/读 allowlisted 字段：永远不持久化 command、argv、env、
cwd、pid、path 或 secret。公开 DTO 也不含这些字段。
"""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Any, Mapping

# Process-group capability levels (fixed enum).
GROUP_CAPABILITY_CGROUP = "process_group_and_cgroup"
GROUP_CAPABILITY_PGROUP = "process_group_only"

# State machine for a managed session:
#   launching -> running -> paused / quarantined -> terminated
LAUNCHING = "launching"
RUNNING = "running"
PAUSED = "paused"
QUARANTINED = "quarantined"
TERMINATED = "terminated"

TERMINAL_STATES = frozenset({TERMINATED})

_MAX_OPAQUE = 128
_OPAQUE_REJECT = ("/", "\\", "token", "secret", "private", "key", "password")


def new_opaque_id(prefix: str) -> str:
    """Generate a short opaque local id (``<prefix>_<hex>``)."""
    if not prefix or len(prefix) > 32:
        raise ValueError("prefix must be 1..32 chars")
    return f"{prefix}_{secrets.token_hex(8)}"


def validate_opaque(value: Any, name: str) -> str:
    """Strict bounded opaque id validation: no path/secret shape."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty opaque id")
    if len(value) > _MAX_OPAQUE:
        raise ValueError(f"{name} is too long")
    lowered = value.lower()
    if any(m in lowered for m in _OPAQUE_REJECT):
        raise ValueError(f"{name} must not encode a path or secret")
    return value


def sanitize_opaque(value: Any, name: str) -> str | None:
    """Return a valid opaque id or None (never raises)."""
    try:
        return validate_opaque(value, name)
    except (ValueError, TypeError):
        return None


# Fields that are never written to the durable manifest nor exposed publicly.
_PRIVACY_KEYS = frozenset({
    "pid", "process_id", "cwd", "path", "env", "environment", "argv",
    "command", "cmd", "command_line", "token", "secret", "credential",
    "password", "api_key", "access_token", "authorization", "raw",
    "raw_output", "collector_output", "collector", "proc_path",
})

# Public keys a manifest is *allowed* to expose (bounded allowlist).
_PUBLIC_KEYS = frozenset({
    "session_id", "attempt_id", "process_group_id", "machine_id", "agent",
    "state", "group_capability", "platform", "capability", "capability_manifest",
    "managed", "capture_quality", "control_capability", "created_at",
    "updated_at", "started_at", "reason",
})


@dataclass
class ManagedSessionManifest:
    """Durable, bounded per-session authority record.

    The spawned command line, environment, cwd and pid are deliberately NOT
    stored on this durable record, so a lost manifest never leaks operational
    secrets and a recovered Supervisor never auto-restarts the process.
    Native-resume identity lives only on the in-memory ``_ManagedEntry``.
    """

    session_id: str
    machine_id: str
    process_group_id: str
    agent: str = "unknown"
    attempt_id: str | None = None
    state: str = RUNNING
    group_capability: str = GROUP_CAPABILITY_PGROUP
    platform: str = ""
    capability_manifest: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    reason: str = ""  # bounded last-transition code

    def as_dict(self) -> dict[str, Any]:
        """Public/bounded serialization (no privates)."""
        data: dict[str, Any] = {
            "session_id": self.session_id,
            "machine_id": self.machine_id,
            "process_group_id": self.process_group_id,
            "agent": self.agent,
            "attempt_id": self.attempt_id,
            "state": self.state,
            "group_capability": self.group_capability,
            "platform": self.platform,
            "managed": True,
            "control_capability": "available",
        }
        if isinstance(self.capability_manifest, Mapping):
            data["capability_manifest"] = dict(
                list(self.capability_manifest.items())[:64])
        for k in ("created_at", "updated_at", "started_at", "reason"):
            v = getattr(self, k, None)
            if v:
                data[k] = v
        return data

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, ensure_ascii=True)

    @classmethod
    def from_json(cls, raw: str | Mapping[str, Any]) -> "ManagedSessionManifest":
        """Deserialize a bounded durable manifest (never trusts privates)."""
        if isinstance(raw, str):
            raw = json.loads(raw)
        if not isinstance(raw, Mapping):
            raise ValueError("manifest must be a mapping")
        d = dict(raw)
        clean = {k: v for k, v in d.items() if k not in _PRIVACY_KEYS}
        return cls(
            session_id=(sanitize_opaque(clean.get("session_id"), "session_id")
                        or ""),
            machine_id=(sanitize_opaque(clean.get("machine_id"), "machine_id")
                        or ""),
            process_group_id=(sanitize_opaque(
                clean.get("process_group_id"), "process_group_id") or ""),
            agent=str(clean.get("agent") or "unknown")[:32],
            attempt_id=sanitize_opaque(clean.get("attempt_id"), "attempt_id"),
            state=str(clean.get("state") or RUNNING)[:32],
            group_capability=str(clean.get("group_capability")
                                 or GROUP_CAPABILITY_PGROUP)[:48],
            platform=str(clean.get("platform") or "")[:16],
            capability_manifest=dict(clean.get("capability_manifest") or {}),
            created_at=str(clean.get("created_at") or "")[:64],
            updated_at=str(clean.get("updated_at") or "")[:64],
        )


def manifest_public(manifest: Mapping[str, Any], *, managed: bool = True) -> dict[str, Any]:
    """Allowlisted public row for a managed session (never leaks privates)."""
    out: dict[str, Any] = {}
    for key in _PUBLIC_KEYS:
        if key in manifest:
            value = manifest[key]
            if key == "capability_manifest":
                if isinstance(value, Mapping):
                    out[key] = dict(value)
                continue
            if isinstance(value, (str, int, bool)) or value is None:
                out[key] = value
    out["managed"] = bool(managed and manifest.get("process_group_id"))
    out["control_capability"] = "available" if out["managed"] else "unavailable"
    return out


__all__ = [
    "GROUP_CAPABILITY_CGROUP",
    "GROUP_CAPABILITY_PGROUP",
    "ManagedSessionManifest",
    "manifest_public",
    "new_opaque_id",
    "sanitize_opaque",
    "validate_opaque",
]
