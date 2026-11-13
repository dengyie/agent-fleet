"""Framework-free external Agent contract and evidence states."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from .contract_types import CapabilityRequirement, CatalogRef
from .errors import WorkflowValidationError
from .identity import validate_reference

if TYPE_CHECKING:
    from .external_outputs import ExternalOutputSet

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class ExternalAgentError(RuntimeError):
    def __init__(self, code: str, *, uncertain: bool = False, cause: BaseException | None = None):
        self.code = code
        self.uncertain = bool(uncertain)
        super().__init__(code)
        if cause is not None:
            self.__cause__ = cause


def _id(value: object, code: str = "invalid_external_identity") -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ExternalAgentError(code)
    return value


@dataclass(frozen=True, slots=True)
class ExternalAgentCapabilities:
    submit_idempotent: bool
    lookup_by_token: bool
    status_evidence: bool
    cancel_supported: bool
    cancel_confirmable: bool
    output_export_supported: bool
    supported_capabilities: tuple[CatalogRef, ...] = ()

    def __post_init__(self) -> None:
        for value in (self.submit_idempotent, self.lookup_by_token, self.status_evidence,
                      self.cancel_supported, self.cancel_confirmable, self.output_export_supported):
            if type(value) is not bool:
                raise ExternalAgentError("invalid_capabilities")
        if self.cancel_confirmable and not self.cancel_supported:
            raise ExternalAgentError("invalid_capabilities")
        if type(self.supported_capabilities) is not tuple or len(self.supported_capabilities) > 16:
            raise ExternalAgentError("invalid_capabilities")
        for reference in self.supported_capabilities:
            if type(reference) is not CatalogRef or type(reference.revision) is not int or not 1 <= reference.revision <= 9007199254740991:
                raise ExternalAgentError("invalid_capabilities")
            try:
                validate_reference(reference.id)
            except WorkflowValidationError as exc:
                raise ExternalAgentError("invalid_capabilities") from exc
        if len(set(self.supported_capabilities)) != len(self.supported_capabilities):
            raise ExternalAgentError("invalid_capabilities")
        # Capability declarations are a set. Keep equality consistent with the
        # canonical snapshot used by admission, dispatch and restart recovery.
        object.__setattr__(self, "supported_capabilities", tuple(sorted(
            self.supported_capabilities, key=lambda item: (item.id, item.revision))))

    def supports(self, requirements: tuple[CapabilityRequirement, ...]) -> bool:
        """Return whether this explicitly registered adapter can honor a contract."""
        offered = set(self.supported_capabilities)
        return all(any(CatalogRef(item.id, revision) in offered for revision in item.revisions)
                   for item in requirements)

    def payload(self) -> dict[str, Any]:
        payload = {
            "submit_idempotent": self.submit_idempotent,
            "lookup_by_token": self.lookup_by_token,
            "status_evidence": self.status_evidence,
            "cancel_supported": self.cancel_supported,
            "cancel_confirmable": self.cancel_confirmable,
            "output_export_supported": self.output_export_supported,
        }
        if self.supported_capabilities:
            payload["supported_capabilities"] = [
                {"id": item.id, "revision": item.revision}
                for item in sorted(self.supported_capabilities, key=lambda value: (value.id, value.revision))
            ]
        return payload


@dataclass(frozen=True, slots=True)
class ExternalTaskRef:
    agent_id: str
    dispatch_token: str
    external_id: str | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        _id(self.agent_id)
        _id(self.dispatch_token)
        if self.external_id is not None:
            _id(self.external_id)
        if self.session_id is not None:
            _id(self.session_id)


@dataclass(frozen=True, slots=True)
class ExternalAgentStatus:
    state: str
    evidence: str
    external_id: str | None = None
    text: str = ""
    error_code: str | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        if self.state not in {"dispatching", "waiting_external", "running", "waiting_input",
                              "succeeded", "failed", "cancelled", "cancelling", "unknown"}:
            raise ExternalAgentError("invalid_external_state")
        if self.evidence not in {"none", "transport", "accepted", "agent_reported", "confirmed", "unknown"}:
            raise ExternalAgentError("invalid_external_evidence")
        if self.external_id is not None:
            _id(self.external_id)
        if self.session_id is not None:
            _id(self.session_id)
        if len(self.text) > 8192:
            raise ExternalAgentError("external_result_too_large")


@dataclass(frozen=True, slots=True)
class CancelReceipt:
    state: str
    requested: bool
    confirmed: bool
    external_id: str | None = None
    error_code: str | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        if self.state not in {"cancelling", "cancelled", "unknown", "unsupported"}:
            raise ExternalAgentError("invalid_cancel_state")
        if type(self.requested) is not bool or type(self.confirmed) is not bool:
            raise ExternalAgentError("invalid_cancel_receipt")
        if (self.confirmed != (self.state == "cancelled") or (self.confirmed and not self.requested)
                or (self.state == "unsupported" and self.requested)
                or (self.state == "cancelling" and not self.requested)):
            raise ExternalAgentError("invalid_cancel_receipt")
        if self.external_id is not None:
            _id(self.external_id)
        if self.session_id is not None:
            _id(self.session_id)


class ExternalAgentAdapter(Protocol):
    agent_id: str
    capabilities: ExternalAgentCapabilities

    @property
    def configuration_digest(self) -> str | None: ...

    def lookup(self, dispatch_token: str, resolved_contract: Mapping[str, Any]) -> ExternalTaskRef | None: ...
    def submit(self, dispatch_token: str, resolved_contract: Mapping[str, Any]) -> ExternalTaskRef: ...
    def inspect(self, task: ExternalTaskRef) -> ExternalAgentStatus: ...
    def request_cancel(self, task: ExternalTaskRef) -> CancelReceipt: ...
    def export_outputs(self, task: ExternalTaskRef, output_specs: tuple[Mapping[str, Any], ...]) -> ExternalOutputSet: ...


__all__ = [
    "CancelReceipt",
    "ExternalAgentAdapter",
    "ExternalAgentCapabilities",
    "ExternalAgentError",
    "ExternalAgentStatus",
    "ExternalTaskRef",
]
