"""Immutable, bounded code-package inputs for Native Workflow Runs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contract import reference_values
from .contract_types import CodeRevisionReference, NamedReference, ResolvedContract
from .errors import WorkflowValidationError
from .identity import canonical_json, validate_reference

MAX_CODE_REFERENCES = 16
MAX_CODE_BYTES = 10 * 1024 * 1024
MAX_CODE_FILES = 4096
MAX_CODE_LIST_PAGE = 128
MAX_CODE_READ_BYTES = 8192


@dataclass(frozen=True, slots=True)
class CodeRevisionInput:
    ref_key: str
    reference: CodeRevisionReference

    def __post_init__(self) -> None:
        validate_reference(self.ref_key)
        if type(self.reference) is not CodeRevisionReference:
            raise WorkflowValidationError("invalid_code_input")


def code_revision_inputs(resolved: ResolvedContract) -> tuple[CodeRevisionInput, ...]:
    if type(resolved) is not ResolvedContract:
        raise WorkflowValidationError("invalid_code_input")
    found = tuple(CodeRevisionInput(item.ref_key, item.reference)
                  for item in resolved.contract.input_refs
                  if type(item.reference) is CodeRevisionReference)
    if not found:
        return ()
    if len(found) > MAX_CODE_REFERENCES:
        raise WorkflowValidationError("too_many_code_inputs")
    keys: set[str] = set()
    bundles: dict[str, tuple[str, int]] = {}
    counted: set[str] = set()
    total = 0
    for item in found:
        reference = item.reference
        if item.ref_key in keys:
            raise WorkflowValidationError("invalid_code_input")
        keys.add(item.ref_key)
        bundle = reference.source_bundle
        identity = (bundle.sha256, bundle.size)
        previous = bundles.setdefault(bundle.artifact_id, identity)
        if previous != identity:
            raise WorkflowValidationError("invalid_code_input")
        if bundle.artifact_id not in counted:
            total += bundle.size
            counted.add(bundle.artifact_id)
    if total > MAX_CODE_BYTES:
        raise WorkflowValidationError("code_inputs_too_large")
    reference_values(tuple(NamedReference(item.ref_key, item.reference) for item in found))
    return found


def code_input_value(item: CodeRevisionInput) -> dict[str, Any]:
    reference = item.reference
    bundle = reference.source_bundle
    return {"ref_key": item.ref_key, "repository_id": reference.repository_id,
            "base_commit": reference.base_commit, "head_commit": reference.head_commit,
            "tree_id": reference.tree_id, "artifact_id": bundle.artifact_id,
            "sha256": bundle.sha256, "size": bundle.size}


def code_input_snapshot(items: tuple[CodeRevisionInput, ...]) -> bytes:
    if type(items) is not tuple or not items or len(items) > MAX_CODE_REFERENCES:
        raise WorkflowValidationError("invalid_code_input")
    values = [code_input_value(item) for item in items]
    if len({item["ref_key"] for item in values}) != len(values):
        raise WorkflowValidationError("invalid_code_input")
    return canonical_json(values)
