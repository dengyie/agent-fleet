"""Bounded image and audio references shared by explicit Native Workflow consumers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .workflow_contract import parse_resolved
from .workflow_contract_types import ArtifactReference, ResolvedContract
from .workflow_errors import WorkflowValidationError
from .workflow_identity import validate_reference

MAX_MEDIA_INPUTS = 4
MAX_MEDIA_ITEM_BYTES = 2 * 1024 * 1024
MAX_MEDIA_INPUT_BYTES = 4 * 1024 * 1024
IMAGE_CONTENT_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
AUDIO_CONTENT_TYPES = frozenset({"audio/wav", "audio/mpeg"})


@dataclass(frozen=True, slots=True)
class WorkflowMediaInput:
    ref_key: str
    reference: ArtifactReference
    content_type: str


@dataclass(frozen=True, slots=True)
class WorkflowAudioInput:
    ref_key: str
    reference: ArtifactReference
    content_type: str


def artifact_media_references(resolved: ResolvedContract) -> tuple[tuple[str, ArtifactReference], ...]:
    if type(resolved) is not ResolvedContract or parse_resolved(resolved.snapshot) != resolved:
        raise WorkflowValidationError("invalid_resolved_snapshot")
    references = []
    for named in resolved.contract.input_refs:
        if type(named.reference) is ArtifactReference:
            references.append((named.ref_key, named.reference))
        else:
            raise WorkflowValidationError("media_input_unsupported")
    if len(references) > 16:
        raise WorkflowValidationError("invalid_input_references")
    return tuple(references)


def media_input_value(expected: tuple[str, ArtifactReference], actual: WorkflowMediaInput) -> dict[str, Any]:
    if type(actual) is not WorkflowMediaInput or (actual.ref_key, actual.reference) != expected:
        raise WorkflowValidationError("media_input_mismatch")
    key, reference = expected
    validate_reference(key)
    if (type(reference) is not ArtifactReference or type(reference.size) is not int
            or reference.size < 0 or reference.size > MAX_MEDIA_ITEM_BYTES):
        raise WorkflowValidationError("media_input_too_large")
    if type(actual.content_type) is not str or actual.content_type not in IMAGE_CONTENT_TYPES:
        raise WorkflowValidationError("media_input_unsupported")
    return {"ref_key": key, "artifact_id": reference.artifact_id, "sha256": reference.sha256,
            "size": reference.size, "content_type": actual.content_type}


def parse_media_inputs(value: object) -> tuple[WorkflowMediaInput, ...]:
    if type(value) is not list or len(value) > MAX_MEDIA_INPUTS:
        raise WorkflowValidationError("invalid_media_inputs")
    inputs = []
    for item in value:
        if (type(item) is not dict or set(item) != {"ref_key", "artifact_id", "sha256", "size", "content_type"}
                or type(item["size"]) is not int):
            raise WorkflowValidationError("invalid_media_inputs")
        inputs.append(WorkflowMediaInput(item["ref_key"],
            ArtifactReference(item["artifact_id"], item["sha256"], item["size"]), item["content_type"]))
    return tuple(inputs)


def audio_input_value(expected: tuple[str, ArtifactReference], actual: WorkflowAudioInput) -> dict[str, Any]:
    if type(actual) is not WorkflowAudioInput or (actual.ref_key, actual.reference) != expected:
        raise WorkflowValidationError("audio_input_mismatch")
    key, reference = expected
    validate_reference(key)
    if (type(reference) is not ArtifactReference or type(reference.size) is not int
            or reference.size < 0 or reference.size > MAX_MEDIA_ITEM_BYTES):
        raise WorkflowValidationError("audio_input_too_large")
    if type(actual.content_type) is not str or actual.content_type not in AUDIO_CONTENT_TYPES:
        raise WorkflowValidationError("audio_input_unsupported")
    return {"ref_key": key, "artifact_id": reference.artifact_id, "sha256": reference.sha256,
            "size": reference.size, "content_type": actual.content_type}


def parse_audio_inputs(value: object) -> tuple[WorkflowAudioInput, ...]:
    if type(value) is not list or len(value) > MAX_MEDIA_INPUTS:
        raise WorkflowValidationError("invalid_audio_inputs")
    inputs = []
    for item in value:
        if (type(item) is not dict or set(item) != {"ref_key", "artifact_id", "sha256", "size", "content_type"}
                or type(item["size"]) is not int):
            raise WorkflowValidationError("invalid_audio_inputs")
        inputs.append(WorkflowAudioInput(item["ref_key"],
            ArtifactReference(item["artifact_id"], item["sha256"], item["size"]), item["content_type"]))
    return tuple(inputs)
