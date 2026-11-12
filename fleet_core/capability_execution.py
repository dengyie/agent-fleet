"""Pinned capability context inside the existing signed command arguments."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from .capability import CapabilityError, TargetBinding, decode, freeze, identifier
from .workflow.contract_types import CatalogRef
from fleet_core.platform_command import PlatformCommand, args_hash

CAPABILITY_ARGUMENT = "_fleet_capability"


@dataclass(frozen=True, slots=True)
class RegisteredCapabilityAction:
    adapter_id: str
    capability: CatalogRef
    action: str

    def __post_init__(self) -> None:
        identifier(self.adapter_id)
        identifier(self.action)
        if type(self.capability) is not CatalogRef:
            raise CapabilityError("invalid_capability_action")
        identifier(self.capability.id)
        if type(self.capability.revision) is not int or not 1 <= self.capability.revision <= 9007199254740991:
            raise CapabilityError("invalid_capability_action")
        if not self.action.startswith(("tool.", "reconcile.")):
            raise CapabilityError("invalid_capability_action")


@dataclass(frozen=True, slots=True)
class CapabilityExecutionContext:
    capability_id: str
    adapter_id: str
    binding: TargetBinding

    def __post_init__(self) -> None:
        identifier(self.capability_id)
        identifier(self.adapter_id)
        if type(self.binding) is not TargetBinding or self.capability_id not in {ref.id for ref in self.binding.capabilities}:
            raise CapabilityError("invalid_capability_context")

    @property
    def snapshot(self) -> bytes:
        return freeze({"schema_version": 1, "capability_id": self.capability_id,
            "adapter_id": self.adapter_id, "binding": decode(self.binding.snapshot)})

    @classmethod
    def from_snapshot(cls, raw: bytes) -> CapabilityExecutionContext:
        value = decode(raw)
        if (set(value) != {"schema_version", "capability_id", "adapter_id", "binding"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1):
            raise CapabilityError("invalid_capability_context")
        return cls(value["capability_id"], value["adapter_id"], TargetBinding.from_snapshot(freeze(value["binding"])))

    @classmethod
    def from_command(cls, command: Mapping[str, Any]) -> CapabilityExecutionContext:
        if not isinstance(command, Mapping):
            raise CapabilityError("invalid_capability_context")
        arguments = command.get("arguments")
        if not isinstance(arguments, Mapping) or CAPABILITY_ARGUMENT not in arguments:
            raise CapabilityError("invalid_capability_context")
        return cls.from_snapshot(freeze(arguments[CAPABILITY_ARGUMENT]))

    def bind(self, command: PlatformCommand) -> PlatformCommand:
        """Trusted Hub preparation; the resulting envelope must be signed again."""
        if (type(command) is not PlatformCommand or command.target_node != self.binding.node_id
                or type(command.run_id) is not str or not command.run_id
                or not command.action.startswith(("tool.", "reconcile."))
                or CAPABILITY_ARGUMENT in command.arguments):
            raise CapabilityError("invalid_capability_command")
        identifier(command.run_id)
        identifier(command.action)
        arguments = {**command.arguments, CAPABILITY_ARGUMENT: decode(self.snapshot)}
        try:
            freeze(arguments)
        except CapabilityError as exc:
            if exc.code == "json_too_large":
                raise CapabilityError("arguments_too_large") from exc
            raise
        return replace(command, arguments=arguments, args_digest=args_hash(arguments), signature=None)
