"""Deterministic external execution registrations and private task snapshots."""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass

from .external_agent import (
    ExternalAgentCapabilities,
    ExternalAgentError,
    ExternalTaskRef,
    _id,
)
from .external_request import ExternalAgentRequest, external_input_references
from .workflow_contract import parse_resolved
from .workflow_contract_types import CatalogRef, ResolvedContract
from .workflow_errors import WorkflowValidationError
from .workflow_execution import ExecutionObservation
from .workflow_identity import canonical_json, validate_reference
from .workflow_json import decode_snapshot

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CAPABILITIES = frozenset(ExternalAgentCapabilities.__dataclass_fields__)
_BASE_CAPABILITIES = _CAPABILITIES - {"supported_capabilities"}


@dataclass(frozen=True, slots=True)
class RegisteredExternalTarget:
    owner_id: str
    workspace_id: str
    target_key: str
    adapter_ref: CatalogRef
    policy_ref: CatalogRef
    agent_id: str
    configuration_digest: str
    capabilities: ExternalAgentCapabilities

    def __post_init__(self) -> None:
        for value in (self.owner_id, self.workspace_id, self.target_key):
            validate_reference(value)
        for reference in (self.adapter_ref, self.policy_ref):
            if type(reference) is not CatalogRef or type(reference.revision) is not int or not 1 <= reference.revision <= 9007199254740991:
                raise WorkflowValidationError("invalid_catalog_reference")
            validate_reference(reference.id)
        try:
            _id(self.agent_id)
        except ExternalAgentError as exc:
            raise WorkflowValidationError(exc.code) from exc
        if type(self.configuration_digest) is not str or not _DIGEST.fullmatch(self.configuration_digest):
            raise WorkflowValidationError("invalid_external_configuration")
        if type(self.capabilities) is not ExternalAgentCapabilities:
            raise WorkflowValidationError("invalid_external_capabilities")

    @property
    def snapshot(self) -> bytes:
        target = asdict(self)
        target["adapter_ref"] = asdict(self.adapter_ref)
        target["policy_ref"] = asdict(self.policy_ref)
        target["capabilities"] = self.capabilities.payload()
        return canonical_json({"schema_version": 1, **target})

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.snapshot).hexdigest()

    def validate_resolved(self, resolved: ResolvedContract) -> None:
        if type(resolved) is not ResolvedContract or parse_resolved(resolved.snapshot) != resolved:
            raise WorkflowValidationError("invalid_resolved_snapshot")
        adapter = decode_snapshot(resolved.snapshot)["adapter_ref"]
        if (resolved.owner_id, resolved.workspace_id, resolved.contract.policy_ref, adapter) != (
            self.owner_id, self.workspace_id, self.policy_ref, asdict(self.adapter_ref),
        ):
            raise WorkflowValidationError("external_registration_conflict")
        if not self.capabilities.supports(resolved.contract.capability_requirements):
            raise WorkflowValidationError("external_capability_unavailable")
        external_input_references(resolved)
        if resolved.contract.output_specs and not self.capabilities.output_export_supported:
            raise WorkflowValidationError("external_contract_unsupported")


def parse_external_target(raw: bytes) -> RegisteredExternalTarget:
    value = decode_snapshot(raw, max_bytes=8192)
    if set(value) != {"schema_version", *RegisteredExternalTarget.__dataclass_fields__} or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise WorkflowValidationError("invalid_external_registration")
    for key in ("adapter_ref", "policy_ref"):
        if type(value[key]) is not dict or set(value[key]) != {"id", "revision"}:
            raise WorkflowValidationError("invalid_catalog_reference")
    if type(value["capabilities"]) is not dict or set(value["capabilities"]) not in {_BASE_CAPABILITIES, _CAPABILITIES}:
        raise WorkflowValidationError("invalid_external_capabilities")
    capabilities = dict(value["capabilities"])
    supported = capabilities.get("supported_capabilities", [])
    if type(supported) is not list:
        raise WorkflowValidationError("invalid_external_capabilities")
    try:
        capabilities["supported_capabilities"] = tuple(CatalogRef(**item) for item in supported)
    except (TypeError, ValueError) as exc:
        raise WorkflowValidationError("invalid_external_capabilities") from exc
    try:
        target = RegisteredExternalTarget(value["owner_id"], value["workspace_id"], value["target_key"],
            CatalogRef(**value["adapter_ref"]), CatalogRef(**value["policy_ref"]), value["agent_id"],
            value["configuration_digest"], ExternalAgentCapabilities(**capabilities))
    except ExternalAgentError as exc:
        raise WorkflowValidationError(exc.code) from exc
    if target.snapshot != raw:
        raise WorkflowValidationError("noncanonical_external_registration")
    return target


def task_snapshot(task: ExternalTaskRef, target: RegisteredExternalTarget, token: str) -> bytes:
    if type(task) is not ExternalTaskRef or (task.agent_id, task.dispatch_token) != (target.agent_id, token) or not (task.external_id or task.session_id):
        raise WorkflowValidationError("external_task_mismatch")
    return canonical_json(asdict(task))


def parse_external_task(raw: bytes, target: RegisteredExternalTarget, token: str) -> ExternalTaskRef:
    value = decode_snapshot(raw, max_bytes=2048)
    if set(value) != set(ExternalTaskRef.__dataclass_fields__):
        raise WorkflowValidationError("invalid_external_task")
    try:
        task = ExternalTaskRef(**value)
    except ExternalAgentError as exc:
        raise WorkflowValidationError(exc.code) from exc
    if task_snapshot(task, target, token) != raw:
        raise WorkflowValidationError("invalid_external_task")
    return task


@dataclass(frozen=True, slots=True)
class ExternalDispatchPin:
    target: RegisteredExternalTarget
    resolved: ResolvedContract
    send_started_at: float | None
    task: ExternalTaskRef | None
    accepted: bool = False
    request: ExternalAgentRequest | None = None
    observation: ExecutionObservation | None = None
