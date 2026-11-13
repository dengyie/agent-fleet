"""Pure signed request and bounded receipt values for fixed Node exports."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
from typing import Any, TypedDict

from .artifact_transfer import ArtifactTransferError, ID_RE, SHA256_RE
from fleet_core.platform_command import args_hash, verify_command

EXPORT_ACTION = "artifact.export"
ENVELOPE_FIELDS = ("command_id", "target_node", "action", "resource_id", "arguments",
    "retry_class", "expires_at", "args_hash", "run_id", "grant_id", "signature")


class ArtifactExportError(ArtifactTransferError):
    def __init__(self, code: str, *, uncertain: bool = False) -> None:
        super().__init__(code)
        self.uncertain = uncertain


class ExportArtifactRef(TypedDict):
    artifact_id: str
    sha256: str
    size: int


class ExportReceipt(TypedDict):
    output_key: str
    artifact: ExportArtifactRef


def _identity(value: Any) -> bool:
    return (type(value) is str and 0 < len(value) <= 128
        and all(0x20 <= ord(char) != 0x7f for char in value))


@dataclass(frozen=True, slots=True)
class ExportRequest:
    command_id: str
    workspace_id: str
    output_key: str
    run_id: str
    expires_at: float
    command_digest: str

    @classmethod
    def parse(cls, command: Mapping[str, Any], *, node_id: str, public_key: bytes,
              now: float, allow_expired: bool = False) -> ExportRequest:
        try:
            signed = verify_command(command, public_key)
        except (TypeError, ValueError) as exc:
            raise ArtifactExportError("invalid_signature") from exc
        if not signed or command.get("target_node") != node_id:
            raise ArtifactExportError("invalid_signature")
        arguments = command.get("arguments")
        if (command.get("action") != EXPORT_ACTION or type(arguments) is not dict
                or set(arguments) != {"output_key"} or args_hash(arguments) != command.get("args_hash")
                or any(not _identity(command.get(key)) for key in ("command_id", "resource_id", "run_id"))
                or not _identity(arguments["output_key"])):
            raise ArtifactExportError("invalid_export_command")
        expiry = command.get("expires_at")
        if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= 0:
            raise ArtifactExportError("invalid_export_command")
        if not allow_expired and expiry <= now:
            raise ArtifactExportError("export_expired")
        return cls(command["command_id"], command["resource_id"], arguments["output_key"],
            command["run_id"], float(expiry), args_hash({key: command.get(key) for key in ENVELOPE_FIELDS}))


def validate_export_receipt(request: ExportRequest, receipt: Any, *, artifact_id: str,
                            size: int, sha256: str) -> ExportReceipt:
    expected: ExportReceipt = {"output_key": request.output_key,
        "artifact": {"artifact_id": artifact_id, "size": size, "sha256": sha256}}
    if (type(artifact_id) is not str or ID_RE.fullmatch(artifact_id) is None
            or type(size) is not int or not 0 <= size <= 256 * 1024 * 1024
            or type(sha256) is not str or SHA256_RE.fullmatch(sha256) is None
            or type(receipt) is not dict or receipt != expected or type(receipt.get("artifact")) is not dict
            or type(receipt["artifact"].get("size")) is not int):
        raise ArtifactExportError("invalid_export_receipt")
    return expected
