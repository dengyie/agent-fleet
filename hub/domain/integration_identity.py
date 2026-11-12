"""Immutable, bounded authorization for the three Fleet integration tools."""
from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from itertools import islice

from .workflow_errors import WorkflowValidationError
from .workflow_identity import canonical_json, validate_reference
from .workflow_json import decode_snapshot

SCOPES = frozenset({"workflow.create_from_template", "workflow.get", "workflow.list_artifacts"})
MAX_ALLOWLIST = 128
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = frozenset({"integration_id", "owner_id", "token_digest", "template_ids", "workspace_ids", "scopes",
                     "max_artifacts", "max_budget_tokens", "max_budget_requests"})


class IntegrationIdentityError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def token_digest(token: str) -> str:
    if (type(token) is not str or not 32 <= len(token) <= 512
            or any(not 33 <= ord(char) <= 126 for char in token)):
        raise IntegrationIdentityError("invalid_integration_token")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _reference(value: str) -> None:
    try:
        validate_reference(value)
    except WorkflowValidationError as exc:
        raise IntegrationIdentityError("invalid_integration_identity") from exc


def _references(values: Iterable[str], *, scopes: bool = False) -> frozenset[str]:
    if isinstance(values, (str, bytes, Mapping)):
        raise IntegrationIdentityError("invalid_integration_scope")
    try:
        items = tuple(islice(iter(values), MAX_ALLOWLIST + 1))
    except TypeError as exc:
        raise IntegrationIdentityError("invalid_integration_scope") from exc
    if len(items) > MAX_ALLOWLIST or any(type(item) is not str for item in items):
        raise IntegrationIdentityError("invalid_integration_scope")
    result = frozenset(items)
    if len(result) != len(items) or (not scopes and not result) or (scopes and not result <= SCOPES):
        raise IntegrationIdentityError("invalid_integration_scope")
    for item in result:
        _reference(item)
    return result


@dataclass(frozen=True, slots=True)
class IntegrationIdentity:
    integration_id: str
    owner_id: str
    token_digest: str = field(repr=False)
    template_ids: frozenset[str]
    workspace_ids: frozenset[str]
    scopes: frozenset[str] = SCOPES
    max_artifacts: int = 100
    max_budget_tokens: int = 200000
    max_budget_requests: int = 50
    revoked: bool = False
    _snapshot: bytes = field(init=False, repr=False, compare=False)
    _definition_digest: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _reference(self.integration_id)
        _reference(self.owner_id)
        if type(self.token_digest) is not str or _DIGEST.fullmatch(self.token_digest) is None:
            raise IntegrationIdentityError("invalid_integration_token")
        for name in ("template_ids", "workspace_ids", "scopes"):
            values = getattr(self, name)
            if type(values) is not frozenset:
                raise IntegrationIdentityError("invalid_integration_scope")
            _references(values, scopes=name == "scopes")
        if type(self.max_artifacts) is not int or not 1 <= self.max_artifacts <= 100:
            raise IntegrationIdentityError("invalid_artifact_limit")
        if (type(self.max_budget_tokens) is not int or not 0 <= self.max_budget_tokens <= 2_000_000
                or type(self.max_budget_requests) is not int or not 0 <= self.max_budget_requests <= 1000):
            raise IntegrationIdentityError("invalid_budget_limit")
        if type(self.revoked) is not bool:
            raise IntegrationIdentityError("invalid_integration_identity")
        value = {name: sorted(getattr(self, name)) if name in {"template_ids", "workspace_ids", "scopes"}
                 else getattr(self, name) for name in _FIELDS}
        try:
            snapshot = canonical_json(value)
        except WorkflowValidationError as exc:
            raise IntegrationIdentityError("invalid_integration_identity") from exc
        object.__setattr__(self, "_snapshot", snapshot)
        object.__setattr__(self, "_definition_digest", hashlib.sha256(snapshot).hexdigest())

    @classmethod
    def register(cls, integration_id: str, owner_id: str, token: str, *,
                 template_ids: Iterable[str], workspace_ids: Iterable[str], scopes: Iterable[str] | None = None,
                 max_artifacts: int = 100, max_budget_tokens: int = 200000,
                 max_budget_requests: int = 50) -> IntegrationIdentity:
        return cls(integration_id, owner_id, token_digest(token), _references(template_ids), _references(workspace_ids),
                   SCOPES if scopes is None else _references(scopes, scopes=True), max_artifacts,
                   max_budget_tokens, max_budget_requests)

    @property
    def snapshot(self) -> bytes:
        return self._snapshot

    @property
    def definition_digest(self) -> str:
        return self._definition_digest

    def authenticate(self, token: str) -> bool:
        if self.revoked:
            return False
        try:
            digest = token_digest(token)
        except IntegrationIdentityError:
            return False
        return hmac.compare_digest(self.token_digest, digest)

    def require(self, scope: str) -> None:
        if self.revoked:
            raise IntegrationIdentityError("integration_revoked")
        if scope not in self.scopes:
            raise IntegrationIdentityError("scope_forbidden")

    def authorize_template(self, template_id: str, workspace_id: str) -> None:
        self.require("workflow.create_from_template")
        if template_id not in self.template_ids or workspace_id not in self.workspace_ids:
            raise IntegrationIdentityError("scope_forbidden")

    def namespace_token(self, client_token: str) -> str:
        if type(client_token) is not str or not 1 <= len(client_token) <= 128:
            raise IntegrationIdentityError("invalid_client_token")
        try:
            digest = hashlib.sha256(canonical_json(client_token)).hexdigest()
        except WorkflowValidationError as exc:
            raise IntegrationIdentityError("invalid_client_token") from exc
        return self.token_namespace + digest

    @property
    def token_namespace(self) -> str:
        digest = hashlib.sha256(canonical_json({"integration_id": self.integration_id, "owner_id": self.owner_id})).hexdigest()
        return "mcp_" + digest[:32] + "_"


def parse_integration_identity(raw: bytes) -> IntegrationIdentity:
    try:
        if type(raw) is not bytes:
            raise IntegrationIdentityError("integration_snapshot_invalid")
        value = decode_snapshot(raw)
        if set(value) != _FIELDS or any(type(value[name]) is not list for name in ("template_ids", "workspace_ids", "scopes")):
            raise IntegrationIdentityError("integration_snapshot_invalid")
        identity = IntegrationIdentity(**{**value, "template_ids": _references(value["template_ids"]),
            "workspace_ids": _references(value["workspace_ids"]), "scopes": _references(value["scopes"], scopes=True)})
        if identity.snapshot != raw:
            raise IntegrationIdentityError("integration_snapshot_invalid")
        return identity
    except (WorkflowValidationError, TypeError, IntegrationIdentityError) as exc:
        raise IntegrationIdentityError("integration_snapshot_invalid") from exc


__all__ = ["IntegrationIdentity", "IntegrationIdentityError", "SCOPES", "parse_integration_identity", "token_digest"]
