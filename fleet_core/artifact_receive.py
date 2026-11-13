"""Pure command and receipt contract for fixed Node artifact inputs."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import re
import hashlib
from typing import Any, TypedDict

from fleet_core.platform_command import args_hash, verify_command
from fleet_core.workflow.contract_types import ArtifactReference, CodeRevisionReference, ResolvedContract

RECEIVE_ACTION = "artifact.receive"
RECEIVE_SUFFIXES = frozenset({".bin", ".tar", ".zip", ".json", ".txt", ".md", ".png", ".apk"})
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ARTIFACT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_ALIAS = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_FIELDS = frozenset({"artifact_id", "sha256", "size", "input_alias", "suffix", "workflow_id", "step_key", "generation", "ref_key", "input_digest"})


class ArtifactReceiveError(RuntimeError):
    def __init__(self, code: str, *, uncertain: bool = False) -> None:
        self.code, self.uncertain = code, uncertain
        super().__init__(code)


class ReceiveReceipt(TypedDict):
    grant_id: str
    artifact_id: str
    sha256: str
    size: int
    input_alias: str


def validate_alias(alias: str) -> None:
    if type(alias) is not str or _ALIAS.fullmatch(alias) is None:
        raise ArtifactReceiveError("invalid_input_alias")


@dataclass(frozen=True, slots=True)
class ReceiveRequest:
    command_id: str
    grant_id: str
    artifact_id: str
    sha256: str
    size: int
    input_alias: str
    suffix: str
    expires_at: float
    command_digest: str

    @property
    def filename(self) -> str:
        return self.grant_id + self.suffix

    def receipt(self) -> ReceiveReceipt:
        return {"grant_id": self.grant_id, "artifact_id": self.artifact_id,
            "sha256": self.sha256, "size": self.size, "input_alias": self.input_alias}

    @classmethod
    def parse(cls, command: Mapping[str, Any], *, node_id: str, public_key: bytes,
              now: float, max_bytes: int, allow_expired: bool = False) -> ReceiveRequest:
        arguments = command.get("arguments")
        try:
            valid = verify_command(command, public_key)
        except (TypeError, ValueError) as exc:
            raise ArtifactReceiveError("invalid_signature") from exc
        if not valid or command.get("target_node") != node_id:
            raise ArtifactReceiveError("invalid_signature")
        if (command.get("action") != RECEIVE_ACTION or type(arguments) is not dict
                or set(arguments) != _FIELDS or args_hash(arguments) != command.get("args_hash")
                or command.get("retry_class") != "idempotent_with_key"):
            raise ArtifactReceiveError("invalid_receive_command")
        grant_id = command.get("grant_id")
        if (type(grant_id) is not str or _HASH.fullmatch(grant_id) is None
                or command.get("command_id") != "receive-" + grant_id):
            raise ArtifactReceiveError("invalid_receive_grant")
        if any(type(arguments[key]) is not str or _ID.fullmatch(arguments[key]) is None
               for key in ("workflow_id", "step_key", "ref_key")):
            raise ArtifactReceiveError("invalid_receive_command")
        if type(arguments["artifact_id"]) is not str or _ARTIFACT_ID.fullmatch(arguments["artifact_id"]) is None:
            raise ArtifactReceiveError("invalid_receive_command")
        validate_alias(arguments["input_alias"])
        if type(arguments["suffix"]) is not str or arguments["suffix"] not in RECEIVE_SUFFIXES:
            raise ArtifactReceiveError("invalid_input_suffix")
        if any(type(arguments[key]) is not str or _HASH.fullmatch(arguments[key]) is None
               for key in ("sha256", "input_digest")):
            raise ArtifactReceiveError("invalid_receive_command")
        if type(arguments["generation"]) is not int or not 1 <= arguments["generation"] <= 9007199254740991:
            raise ArtifactReceiveError("invalid_receive_command")
        if type(arguments["size"]) is not int or not 0 <= arguments["size"] <= max_bytes:
            raise ArtifactReceiveError("artifact_too_large")
        expiry = command.get("expires_at")
        if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= 0:
            raise ArtifactReceiveError("invalid_receive_command")
        if not allow_expired and expiry <= now:
            raise ArtifactReceiveError("receive_expired")
        return cls(command["command_id"], grant_id, arguments["artifact_id"], arguments["sha256"],
            arguments["size"], arguments["input_alias"], arguments["suffix"], float(expiry), args_hash({
                key: command.get(key) for key in ("arguments", "grant_id", "target_node", "resource_id", "expires_at")}))


def validate_receipt(request: ReceiveRequest, receipt: Any) -> ReceiveReceipt:
    expected = request.receipt()
    if type(receipt) is not dict or receipt != expected or type(receipt.get("size")) is not int:
        raise ArtifactReceiveError("invalid_receive_receipt")
    return expected


@dataclass(frozen=True, slots=True)
class RegisteredNodeInputs:
    owner_id: str
    workspace_id: str
    node_id: str
    aliases: tuple[tuple[str, str, str], ...]

    def __post_init__(self) -> None:
        if not self.aliases or len(self.aliases) > 64 or len({item[0] for item in self.aliases}) != len(self.aliases):
            raise ArtifactReceiveError("invalid_input_registration")
        for _ref_key, alias, suffix in self.aliases:
            validate_alias(alias)
            if suffix not in RECEIVE_SUFFIXES:
                raise ArtifactReceiveError("invalid_input_suffix")


def receive_grant_id(owner: str, workflow: str, step: str, generation: int, node: str, ref_key: str) -> str:
    return hashlib.sha256("\0".join(("artifact-input-v1", owner, workflow, step, str(generation), node, ref_key)).encode("utf-8")).hexdigest()


def artifact_inputs(resolved: ResolvedContract) -> tuple[tuple[str, ArtifactReference], ...]:
    inputs: list[tuple[str, ArtifactReference]] = []
    for named in resolved.contract.input_refs + resolved.contract.context_refs:
        reference = named.reference.source_bundle if type(named.reference) is CodeRevisionReference else named.reference
        if type(reference) is ArtifactReference:
            inputs.append((named.ref_key, reference))
    return tuple(inputs)
