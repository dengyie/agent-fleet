"""Platform domain values and default selection rules."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from platform_schema import validate_id, validate_owner_id


@dataclass(frozen=True)
class RunConfigSnapshot:
    owner_id: str
    model_profile_id: str | None
    workspace_id: str | None
    execution_node_id: str | None
    source: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "owner_id": self.owner_id,
            "model_profile_id": self.model_profile_id,
            "workspace_id": self.workspace_id,
            "execution_node_id": self.execution_node_id,
            "source": dict(self.source),
        }


def _choice(overrides: Mapping[str, Any], conversation: Mapping[str, Any], workspace: Mapping[str, Any], owner: Mapping[str, Any], key: str):
    for source_name, source in (("request", overrides), ("conversation", conversation), ("workspace", workspace), ("owner", owner)):
        value = source.get(key) if isinstance(source, Mapping) else None
        if value not in (None, ""):
            return value, source_name
    return None, "unset"


def resolve_run_config(owner_id: str, *, conversation: Mapping[str, Any] | None = None, workspace: Mapping[str, Any] | None = None, owner: Mapping[str, Any] | None = None, overrides: Mapping[str, Any] | None = None) -> RunConfigSnapshot:
    owner_id = validate_owner_id(owner_id)
    conversation = conversation or {}
    workspace = workspace or {}
    owner = owner or {}
    overrides = overrides or {}
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for key in ("model_profile_id", "workspace_id", "execution_node_id"):
        value, source = _choice(overrides, conversation, workspace, owner, key)
        if value is not None:
            values[key] = validate_id(value, key)
        else:
            values[key] = None
        sources[key] = source
    return RunConfigSnapshot(owner_id=owner_id, source=sources, **values)


__all__ = ["RunConfigSnapshot", "resolve_run_config"]
