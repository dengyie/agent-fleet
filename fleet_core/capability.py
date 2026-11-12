"""Bounded capability values and pinned schemas; no framework, I/O or clock."""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .workflow.contract_types import CapabilityRequirement, CatalogRef
from .workflow.errors import WorkflowPersistenceError, WorkflowValidationError
from .workflow.identity import canonical_json, validate_reference
from .workflow.json import decode_snapshot
from .workflow.dispatch import DispatchIntent

MAX_REVISION = 9007199254740991
MAX_RESOURCE_BYTES = 1 << 50
MAX_DEFINITIONS = 4096
MAX_CANDIDATES = 256
MAX_OFFER_CAPABILITIES = 128
MAX_SOFTWARE = 64


class CapabilityError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CapabilityStoreError(WorkflowPersistenceError):
    """Persistence boundary error, retaining the underlying exception as cause."""


def identifier(value: str) -> str:
    try:
        validate_reference(value)
    except WorkflowValidationError as exc:
        raise CapabilityError("invalid_identifier") from exc
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise CapabilityError("invalid_identifier")
    return value


def integer(value: int, *, maximum: int = MAX_REVISION, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise CapabilityError("invalid_integer")
    return value


def timestamp(value: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 253402300799:
        raise CapabilityError("invalid_time")
    return float(value)


def digest(value: str) -> str:
    if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise CapabilityError("invalid_digest")
    return value


def freeze(value: object) -> bytes:
    try:
        return canonical_json(value)
    except WorkflowValidationError as exc:
        raise CapabilityError(exc.code) from exc


def decode(raw: bytes, *, max_bytes: int = 65536) -> dict[str, Any]:
    try:
        return decode_snapshot(raw, max_bytes=max_bytes)
    except WorkflowValidationError as exc:
        raise CapabilityError(exc.code) from exc


def _fields(value: object, names: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != names:
        raise CapabilityError("invalid_fields")
    return value


def _strings(values: tuple[str, ...], *, maximum: int = 128) -> None:
    if type(values) is not tuple or len(values) > maximum:
        raise CapabilityError("invalid_list")
    for value in values:
        identifier(value)
    if len(set(values)) != len(values):
        raise CapabilityError("invalid_list")


def _references(values: tuple[CatalogRef, ...], *, maximum: int = MAX_OFFER_CAPABILITIES) -> None:
    if type(values) is not tuple or len(values) > maximum:
        raise CapabilityError("invalid_references")
    for value in values:
        if type(value) is not CatalogRef:
            raise CapabilityError("invalid_reference")
        identifier(value.id)
        integer(value.revision, minimum=1)
    if len(set(values)) != len(values):
        raise CapabilityError("duplicate_reference")


def _ref_payload(values: tuple[CatalogRef, ...]) -> list[dict[str, str | int]]:
    return [{"id": ref.id, "revision": ref.revision} for ref in sorted(values, key=lambda ref: (ref.id, ref.revision))]


def _parse_refs(raw: object) -> tuple[CatalogRef, ...]:
    if type(raw) is not list or len(raw) > MAX_OFFER_CAPABILITIES:
        raise CapabilityError("invalid_references")
    values = tuple(CatalogRef(**_fields(item, {"id", "revision"})) for item in raw)
    _references(values)
    return tuple(sorted(values, key=lambda ref: (ref.id, ref.revision)))


def _software(values: tuple[tuple[str, str], ...]) -> None:
    if type(values) is not tuple or len(values) > MAX_SOFTWARE:
        raise CapabilityError("invalid_software")
    for item in values:
        if type(item) is not tuple or len(item) != 2:
            raise CapabilityError("invalid_software")
        identifier(item[0])
        identifier(item[1])
    if len(dict(values)) != len(values):
        raise CapabilityError("duplicate_software")


def _schema(raw: bytes) -> bytes:
    value = decode(raw, max_bytes=32768)
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 16:
            raise CapabilityError("schema_too_deep")
        if type(item) is list:
            pending.extend((child, depth + 1) for child in item)
            continue
        if type(item) is not dict:
            continue
        for key, child in item.items():
            if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (type(child) is not str or not child.startswith("#")):
                raise CapabilityError("external_schema_reference")
            pending.append((child, depth + 1))
    return freeze(value)


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    capability_id: str
    revision: int
    adapter_id: str
    input_schema_ref: str
    output_schema_ref: str
    input_schema: bytes
    output_schema: bytes
    risk: str
    retry_class: str
    allowed_owner_roles: tuple[str, ...]
    max_active_seconds: int
    result_limit_bytes: int
    _snapshot: bytes = field(init=False, repr=False, compare=False)
    _digest: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for value in (self.capability_id, self.adapter_id, self.input_schema_ref, self.output_schema_ref, self.risk, self.retry_class):
            identifier(value)
        integer(self.revision, minimum=1)
        integer(self.max_active_seconds, minimum=1, maximum=86400)
        integer(self.result_limit_bytes, minimum=1, maximum=65536)
        _strings(self.allowed_owner_roles, maximum=16)
        if not self.allowed_owner_roles or self.retry_class not in {"read_only", "idempotent_with_key", "reconcile_before_retry", "manual_only"}:
            raise CapabilityError("invalid_capability_policy")
        object.__setattr__(self, "input_schema", _schema(self.input_schema))
        object.__setattr__(self, "output_schema", _schema(self.output_schema))
        snapshot = freeze(self.payload())
        object.__setattr__(self, "_snapshot", snapshot)
        object.__setattr__(self, "_digest", hashlib.sha256(snapshot).hexdigest())

    def payload(self) -> dict[str, Any]:
        return {"capability_id": self.capability_id, "revision": self.revision, "adapter_id": self.adapter_id,
            "input_schema_ref": self.input_schema_ref, "output_schema_ref": self.output_schema_ref,
            "input_schema": decode(self.input_schema), "output_schema": decode(self.output_schema),
            "input_schema_hash": hashlib.sha256(self.input_schema).hexdigest(),
            "output_schema_hash": hashlib.sha256(self.output_schema).hexdigest(),
            "risk": self.risk, "retry_class": self.retry_class, "allowed_owner_roles": sorted(self.allowed_owner_roles),
            "max_active_seconds": self.max_active_seconds, "result_limit_bytes": self.result_limit_bytes}

    @property
    def snapshot(self) -> bytes:
        return self._snapshot

    @property
    def digest(self) -> str:
        return self._digest

    @classmethod
    def from_snapshot(cls, raw: bytes) -> CapabilityDefinition:
        value = decode(raw)
        _fields(value, {"capability_id", "revision", "adapter_id", "input_schema_ref", "output_schema_ref",
            "input_schema", "output_schema", "input_schema_hash", "output_schema_hash", "risk", "retry_class",
            "allowed_owner_roles", "max_active_seconds", "result_limit_bytes"})
        if type(value["allowed_owner_roles"]) is not list:
            raise CapabilityError("invalid_list")
        result = cls(value["capability_id"], value["revision"], value["adapter_id"], value["input_schema_ref"], value["output_schema_ref"],
            freeze(value["input_schema"]), freeze(value["output_schema"]), value["risk"], value["retry_class"],
            tuple(value["allowed_owner_roles"]), value["max_active_seconds"], value["result_limit_bytes"])
        if result.payload() != value:
            raise CapabilityError("definition_corrupt")
        return result


@dataclass(frozen=True, slots=True)
class NodeOffer:
    offer_revision: int
    capabilities: tuple[CatalogRef, ...]
    local_allowed_capabilities: tuple[CatalogRef, ...]
    os: str
    architecture: str
    software: tuple[tuple[str, str], ...]
    slots: int
    memory_bytes: int
    disk_bytes: int
    observed_at: str
    health: str
    received_at: float
    boot_id: str

    def __post_init__(self) -> None:
        integer(self.offer_revision, minimum=1)
        _references(self.capabilities)
        _references(self.local_allowed_capabilities)
        identifier(self.architecture)
        identifier(self.boot_id)
        _software(self.software)
        integer(self.slots, maximum=1024)
        integer(self.memory_bytes, maximum=MAX_RESOURCE_BYTES)
        integer(self.disk_bytes, maximum=MAX_RESOURCE_BYTES)
        timestamp(self.received_at)
        identifier(self.os)
        identifier(self.health)
        if self.os not in {"linux", "darwin", "windows"} or self.health not in {"healthy", "degraded", "unhealthy"}:
            raise CapabilityError("invalid_offer_state")
        if type(self.observed_at) is not str or len(self.observed_at) > 32 or not self.observed_at.endswith("Z"):
            raise CapabilityError("invalid_utc_time")
        try:
            parsed = datetime.fromisoformat(self.observed_at[:-1] + "+00:00")
        except ValueError as exc:
            raise CapabilityError("invalid_utc_time") from exc
        if "T" not in self.observed_at or parsed.tzinfo != timezone.utc:
            raise CapabilityError("invalid_utc_time")
        freeze(self.payload())

    def payload(self) -> dict[str, Any]:
        return {"schema_version": 1, "offer_revision": self.offer_revision, "capabilities": _ref_payload(self.capabilities),
            "local_allowed_capabilities": _ref_payload(self.local_allowed_capabilities), "os": self.os,
            "architecture": self.architecture, "software": dict(self.software), "slots": self.slots,
            "memory_bytes": self.memory_bytes, "disk_bytes": self.disk_bytes, "observed_at": self.observed_at, "health": self.health}

    @property
    def config_digest(self) -> str:
        value = self.payload()
        for key in ("memory_bytes", "disk_bytes", "observed_at", "health"):
            value.pop(key)
        return hashlib.sha256(freeze(value)).hexdigest()


def parse_offer(raw: bytes, *, received_at: float, boot_id: str) -> NodeOffer:
    value = decode(raw)
    _fields(value, {"schema_version", "offer_revision", "capabilities", "local_allowed_capabilities", "os",
        "architecture", "software", "slots", "memory_bytes", "disk_bytes", "observed_at", "health"})
    if type(value["schema_version"]) is not int or value["schema_version"] != 1 or type(value["software"]) is not dict:
        raise CapabilityError("invalid_offer_schema")
    if len(value["software"]) > MAX_SOFTWARE:
        raise CapabilityError("invalid_software")
    return NodeOffer(value["offer_revision"], _parse_refs(value["capabilities"]), _parse_refs(value["local_allowed_capabilities"]),
        value["os"], value["architecture"], tuple(sorted(value["software"].items())), value["slots"], value["memory_bytes"],
        value["disk_bytes"], value["observed_at"], value["health"], timestamp(received_at), boot_id)


@dataclass(frozen=True, slots=True)
class NodePolicy:
    owner_id: str
    node_id: str
    workspaces: tuple[str, ...]
    templates: tuple[CatalogRef, ...]
    capabilities: tuple[CatalogRef, ...]
    priority: int = 0
    slot_limit: int = 1
    input_aliases: tuple[str, ...] = ()
    residency_aliases: tuple[str, ...] = ()
    allowed_risks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        identifier(self.owner_id)
        identifier(self.node_id)
        _strings(self.workspaces)
        _strings(self.input_aliases)
        _strings(self.residency_aliases)
        _strings(self.allowed_risks, maximum=16)
        _references(self.templates)
        _references(self.capabilities)
        integer(self.priority, minimum=-1000000, maximum=1000000)
        integer(self.slot_limit, minimum=1, maximum=1024)

    @property
    def snapshot(self) -> bytes:
        return freeze({"owner_id": self.owner_id, "node_id": self.node_id, "workspaces": sorted(self.workspaces),
            "templates": _ref_payload(self.templates), "capabilities": _ref_payload(self.capabilities), "priority": self.priority,
            "slot_limit": self.slot_limit, "input_aliases": sorted(self.input_aliases),
            "residency_aliases": sorted(self.residency_aliases), "allowed_risks": sorted(self.allowed_risks)})

    @classmethod
    def from_snapshot(cls, raw: bytes) -> NodePolicy:
        value = decode(raw)
        _fields(value, {"owner_id", "node_id", "workspaces", "templates", "capabilities", "priority", "slot_limit",
            "input_aliases", "residency_aliases", "allowed_risks"})
        for key in ("workspaces", "input_aliases", "residency_aliases", "allowed_risks"):
            if type(value[key]) is not list:
                raise CapabilityError("invalid_list")
            value[key] = tuple(value[key])
        value["templates"] = _parse_refs(value["templates"])
        value["capabilities"] = _parse_refs(value["capabilities"])
        return cls(**value)


@dataclass(frozen=True, slots=True)
class SelectionRequirements:
    owner_id: str
    workspace_id: str
    template: CatalogRef
    owner_role: str
    adapter_id: str
    capabilities: tuple[CapabilityRequirement, ...]
    input_digest: str
    operating_systems: tuple[str, ...] = ()
    architectures: tuple[str, ...] = ()
    software: tuple[tuple[str, str], ...] = ()
    input_aliases: tuple[str, ...] = ()
    residency_aliases: tuple[str, ...] = ()
    memory_bytes: int = 0
    disk_bytes: int = 0
    max_active_seconds: int = 60
    result_limit_bytes: int = 65536

    def __post_init__(self) -> None:
        for value in (self.owner_id, self.workspace_id, self.owner_role, self.adapter_id):
            identifier(value)
        _references((self.template,))
        digest(self.input_digest)
        for values in (self.operating_systems, self.architectures, self.input_aliases, self.residency_aliases):
            _strings(values)
        _software(self.software)
        integer(self.memory_bytes, maximum=MAX_RESOURCE_BYTES)
        integer(self.disk_bytes, maximum=MAX_RESOURCE_BYTES)
        integer(self.max_active_seconds, minimum=1, maximum=86400)
        integer(self.result_limit_bytes, minimum=1, maximum=65536)
        if type(self.capabilities) is not tuple or not 1 <= len(self.capabilities) <= 16:
            raise CapabilityError("invalid_requirements")
        seen: set[str] = set()
        for ref in self.capabilities:
            if type(ref) is not CapabilityRequirement or type(ref.revisions) is not tuple or not 1 <= len(ref.revisions) <= 16:
                raise CapabilityError("invalid_requirements")
            identifier(ref.id)
            for revision in ref.revisions:
                integer(revision, minimum=1)
            if ref.id in seen or len(set(ref.revisions)) != len(ref.revisions):
                raise CapabilityError("invalid_requirements")
            seen.add(ref.id)

    @property
    def snapshot(self) -> bytes:
        return freeze({"owner_id": self.owner_id, "workspace_id": self.workspace_id,
            "template": {"id": self.template.id, "revision": self.template.revision}, "owner_role": self.owner_role,
            "adapter_id": self.adapter_id, "capabilities": [{"id": ref.id, "revisions": sorted(ref.revisions)} for ref in sorted(self.capabilities, key=lambda ref: ref.id)],
            "input_digest": self.input_digest, "operating_systems": sorted(self.operating_systems),
            "architectures": sorted(self.architectures), "software": dict(self.software), "input_aliases": sorted(self.input_aliases),
            "residency_aliases": sorted(self.residency_aliases), "memory_bytes": self.memory_bytes, "disk_bytes": self.disk_bytes,
            "max_active_seconds": self.max_active_seconds, "result_limit_bytes": self.result_limit_bytes})

    @classmethod
    def from_snapshot(cls, raw: bytes) -> SelectionRequirements:
        value = _fields(decode(raw), {"owner_id", "workspace_id", "template", "owner_role", "adapter_id",
            "capabilities", "input_digest", "operating_systems", "architectures", "software", "input_aliases",
            "residency_aliases", "memory_bytes", "disk_bytes", "max_active_seconds", "result_limit_bytes"})
        template = CatalogRef(**_fields(value.pop("template"), {"id", "revision"}))
        capabilities = value.pop("capabilities")
        software = value.pop("software")
        if type(capabilities) is not list or not 1 <= len(capabilities) <= 16 or type(software) is not dict:
            raise CapabilityError("invalid_requirements")
        required: list[CapabilityRequirement] = []
        for item in capabilities:
            item = _fields(item, {"id", "revisions"})
            if type(item["revisions"]) is not list:
                raise CapabilityError("invalid_requirements")
            required.append(CapabilityRequirement(item["id"], tuple(item["revisions"])))
        for name in ("operating_systems", "architectures", "input_aliases", "residency_aliases"):
            if type(value[name]) is not list:
                raise CapabilityError("invalid_requirements")
            value[name] = tuple(value[name])
        return cls(**value, template=template, capabilities=tuple(required), software=tuple(software.items()))


@dataclass(frozen=True, slots=True)
class AuthorizedNode:
    policy: NodePolicy
    offer: NodeOffer | None
    enabled: bool
    credential_enabled: bool
    workspace_enabled: bool
    held_slots: int = 0
    held_memory_bytes: int = 0
    held_disk_bytes: int = 0
    resources_known: bool = True

    def __post_init__(self) -> None:
        if type(self.policy) is not NodePolicy or (self.offer is not None and type(self.offer) is not NodeOffer):
            raise CapabilityError("invalid_candidate")
        if any(type(value) is not bool for value in (self.enabled, self.credential_enabled, self.workspace_enabled, self.resources_known)):
            raise CapabilityError("invalid_candidate")
        integer(self.held_slots, maximum=MAX_REVISION)
        integer(self.held_memory_bytes, maximum=MAX_RESOURCE_BYTES * 1024)
        integer(self.held_disk_bytes, maximum=MAX_RESOURCE_BYTES * 1024)


@dataclass(frozen=True, slots=True)
class BoundCapability:
    id: str
    revision: int
    definition_digest: str


@dataclass(frozen=True, slots=True)
class TargetBinding:
    node_id: str
    offer_revision: int
    offer_digest: str
    policy_digest: str
    capabilities: tuple[BoundCapability, ...]
    input_digest: str
    requirements_digest: str
    memory_bytes: int
    disk_bytes: int

    def __post_init__(self) -> None:
        identifier(self.node_id)
        integer(self.offer_revision, minimum=1)
        for value in (self.offer_digest, self.policy_digest, self.input_digest, self.requirements_digest):
            digest(value)
        integer(self.memory_bytes, maximum=MAX_RESOURCE_BYTES)
        integer(self.disk_bytes, maximum=MAX_RESOURCE_BYTES)
        if type(self.capabilities) is not tuple or not 1 <= len(self.capabilities) <= 16:
            raise CapabilityError("invalid_binding")
        seen: set[str] = set()
        for ref in self.capabilities:
            if type(ref) is not BoundCapability:
                raise CapabilityError("invalid_binding")
            identifier(ref.id)
            integer(ref.revision, minimum=1)
            digest(ref.definition_digest)
            if ref.id in seen:
                raise CapabilityError("invalid_binding")
            seen.add(ref.id)

    @property
    def snapshot(self) -> bytes:
        return freeze({"node_id": self.node_id, "offer_revision": self.offer_revision, "offer_digest": self.offer_digest,
            "policy_digest": self.policy_digest, "capabilities": [{"id": ref.id, "revision": ref.revision,
                "definition_digest": ref.definition_digest} for ref in self.capabilities], "input_digest": self.input_digest,
            "requirements_digest": self.requirements_digest, "memory_bytes": self.memory_bytes, "disk_bytes": self.disk_bytes})

    @classmethod
    def from_snapshot(cls, raw: bytes) -> TargetBinding:
        value = decode(raw)
        _fields(value, {"node_id", "offer_revision", "offer_digest", "policy_digest", "capabilities", "input_digest",
            "requirements_digest", "memory_bytes", "disk_bytes"})
        if type(value["capabilities"]) is not list or not 1 <= len(value["capabilities"]) <= 16:
            raise CapabilityError("invalid_binding")
        value["capabilities"] = tuple(BoundCapability(**_fields(item, {"id", "revision", "definition_digest"})) for item in value["capabilities"])
        return cls(**value)


@dataclass(frozen=True, slots=True)
class CandidateExclusion:
    node_id: str | None
    reason_code: str


@dataclass(frozen=True, slots=True)
class SelectionResult:
    binding: TargetBinding | None
    exclusions: tuple[CandidateExclusion, ...] = ()
    excluded_count: int = 0

    @property
    def state(self) -> str:
        return "selected" if self.binding is not None else "waiting_node"


@dataclass(frozen=True, slots=True)
class CapabilityAdmission:
    selection: SelectionResult
    intent: DispatchIntent | None
