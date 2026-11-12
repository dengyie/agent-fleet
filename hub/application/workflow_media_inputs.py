"""Scoped, bounded reads for Native Workflow media ArtifactRefs."""
from __future__ import annotations

import base64
import hashlib
from typing import Any, BinaryIO, Mapping, Protocol

from hub.domain.workflow_contract_types import ArtifactReference, ResolvedContract
from hub.domain.workflow_errors import WorkflowValidationError
from hub.domain.workflow_media_inputs import (
    AUDIO_CONTENT_TYPES,
    IMAGE_CONTENT_TYPES,
    VIDEO_CONTENT_TYPES,
    MAX_MEDIA_INPUT_BYTES,
    MAX_MEDIA_INPUTS,
    MAX_MEDIA_ITEM_BYTES,
    WorkflowAudioInput,
    WorkflowMediaInput,
    WorkflowVideoInput,
    audio_input_value,
    artifact_media_references,
    media_input_value,
    video_input_value,
)


class WorkflowMediaArtifacts(Protocol):
    def get(self, owner_id: str, workspace_id: str, artifact_id: str) -> dict[str, Any] | None: ...

    def open_verified(self, owner_id: str, workspace_id: str, artifact_id: str) -> BinaryIO: ...


class WorkflowMediaInputs:
    def __init__(self, artifacts: WorkflowMediaArtifacts) -> None:
        self.artifacts = artifacts

    def _metadata(self, resolved: ResolvedContract, reference: ArtifactReference) -> str:
        metadata = self.artifacts.get(resolved.owner_id, resolved.workspace_id, reference.artifact_id)
        if (type(metadata) is not dict or type(metadata.get("size")) is not int or
                tuple(metadata.get(key) for key in ("owner_id", "workspace_id", "artifact_id", "sha256", "size")) !=
                (resolved.owner_id, resolved.workspace_id, reference.artifact_id, reference.sha256, reference.size)):
            raise WorkflowValidationError("media_input_forbidden")
        content_type = metadata.get("content_type")
        if type(content_type) is not str or content_type not in IMAGE_CONTENT_TYPES | AUDIO_CONTENT_TYPES | VIDEO_CONTENT_TYPES:
            raise WorkflowValidationError("media_input_unsupported")
        if reference.size > MAX_MEDIA_ITEM_BYTES:
            raise WorkflowValidationError("media_input_too_large")
        return content_type

    @staticmethod
    def _read(resolved: ResolvedContract, item: WorkflowMediaInput | WorkflowAudioInput | WorkflowVideoInput, artifacts: WorkflowMediaArtifacts) -> bytes:
        reference = item.reference
        digest = hashlib.sha256()
        output = bytearray()
        with artifacts.open_verified(resolved.owner_id, resolved.workspace_id, reference.artifact_id) as source:
            while True:
                block = source.read(min(65536, reference.size - len(output) + 1))
                if type(block) is not bytes or len(block) > reference.size - len(output) + 1:
                    raise WorkflowValidationError("media_input_mismatch")
                if not block:
                    break
                output.extend(block)
                digest.update(block)
                if len(output) > reference.size:
                    raise WorkflowValidationError("media_input_mismatch")
        if len(output) != reference.size or digest.hexdigest() != reference.sha256:
            raise WorkflowValidationError("media_input_mismatch")
        return bytes(output)

    def collect(self, resolved: ResolvedContract, *,
                capabilities: Mapping[str, Any] | None = None) -> tuple[WorkflowMediaInput | WorkflowAudioInput | WorkflowVideoInput, ...]:
        references = artifact_media_references(resolved)
        if len(references) > MAX_MEDIA_INPUTS or sum(reference.size for _key, reference in references) > MAX_MEDIA_INPUT_BYTES:
            raise WorkflowValidationError("media_inputs_too_large")
        items = []
        cache: dict[str, tuple[ArtifactReference, str]] = {}
        enabled = {"image_input": True} if capabilities is None else capabilities
        if not isinstance(enabled, Mapping):
            raise WorkflowValidationError("media_input_unsupported")
        for key, reference in references:
            content_type = self._metadata(resolved, reference)
            if content_type in IMAGE_CONTENT_TYPES:
                if enabled.get("image_input") is not True:
                    raise WorkflowValidationError("media_input_unsupported")
                media = WorkflowMediaInput(key, reference, content_type)
                media_input_value((key, reference), media)
            elif content_type in AUDIO_CONTENT_TYPES:
                if enabled.get("audio_input") is not True:
                    raise WorkflowValidationError("audio_input_unsupported")
                media = WorkflowAudioInput(key, reference, content_type)
                audio_input_value((key, reference), media)
            else:
                if enabled.get("video_input") is not True:
                    raise WorkflowValidationError("video_input_unsupported")
                media = WorkflowVideoInput(key, reference, content_type)
                video_input_value((key, reference), media)
            identity = (reference, content_type)
            if cache.setdefault(reference.artifact_id, identity) != identity:
                raise WorkflowValidationError("media_input_mismatch")
            if reference.artifact_id not in {item.reference.artifact_id for item in items}:
                self._read(resolved, media, self.artifacts)
            items.append(media)
        return tuple(items)

    def authorize(self, resolved: ResolvedContract,
                  inputs: tuple[WorkflowMediaInput | WorkflowAudioInput | WorkflowVideoInput, ...], *,
                  capabilities: Mapping[str, Any] | None = None) -> None:
        expected = artifact_media_references(resolved)
        if type(inputs) is not tuple or len(inputs) != len(expected):
            raise WorkflowValidationError("media_input_mismatch")
        total = 0
        checked: dict[str, tuple[ArtifactReference, str]] = {}
        enabled = {"image_input": True} if capabilities is None else capabilities
        if not isinstance(enabled, Mapping):
            raise WorkflowValidationError("media_input_unsupported")
        for named, actual in zip(expected, inputs):
            if type(actual) is WorkflowMediaInput:
                value = media_input_value(named, actual)
                capability = "image_input"
            elif type(actual) is WorkflowAudioInput:
                value = audio_input_value(named, actual)
                capability = "audio_input"
            elif type(actual) is WorkflowVideoInput:
                value = video_input_value(named, actual)
                capability = "video_input"
            else:
                raise WorkflowValidationError("media_input_mismatch")
            if enabled.get(capability) is not True:
                raise WorkflowValidationError("media_input_unsupported")
            total += actual.reference.size
            identity = (actual.reference, actual.content_type)
            previous = checked.setdefault(actual.reference.artifact_id, identity)
            if previous != identity or value["content_type"] != self._metadata(resolved, actual.reference):
                raise WorkflowValidationError("media_input_mismatch")
        if len(inputs) > MAX_MEDIA_INPUTS or total > MAX_MEDIA_INPUT_BYTES:
            raise WorkflowValidationError("media_inputs_too_large")

    def load(self, resolved: ResolvedContract, inputs: tuple[WorkflowMediaInput, ...], *,
             capabilities: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], ...]:
        if any(type(item) is not WorkflowMediaInput for item in inputs):
            raise WorkflowValidationError("media_input_mismatch")
        self.authorize(resolved, inputs, capabilities=capabilities)
        cache: dict[str, tuple[tuple[ArtifactReference, str], str]] = {}
        parts = []
        for item in inputs:
            identity = (item.reference, item.content_type)
            previous = cache.get(item.reference.artifact_id)
            if previous is None:
                data = self._read(resolved, item, self.artifacts)
                encoded = base64.b64encode(data).decode("ascii")
                cache[item.reference.artifact_id] = (identity, encoded)
            elif previous[0] != identity:
                raise WorkflowValidationError("media_input_mismatch")
            else:
                encoded = previous[1]
            parts.append({"type": "image_url", "image_url": {"url": f"data:{item.content_type};base64,{encoded}"}})
        return tuple(parts)

    def load_audio(self, resolved: ResolvedContract, inputs: tuple[WorkflowAudioInput, ...], *,
                   capabilities: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], ...]:
        if any(type(item) is not WorkflowAudioInput for item in inputs):
            raise WorkflowValidationError("audio_input_mismatch")
        self.authorize(resolved, inputs, capabilities=capabilities)
        cache: dict[str, tuple[tuple[ArtifactReference, str], str]] = {}
        parts = []
        for item in inputs:
            identity = (item.reference, item.content_type)
            previous = cache.get(item.reference.artifact_id)
            if previous is None:
                data = self._read(resolved, item, self.artifacts)
                encoded = base64.b64encode(data).decode("ascii")
                cache[item.reference.artifact_id] = (identity, encoded)
            elif previous[0] != identity:
                raise WorkflowValidationError("audio_input_mismatch")
            else:
                encoded = previous[1]
            audio_format = "wav" if item.content_type == "audio/wav" else "mp3"
            parts.append({"type": "input_audio", "input_audio": {"data": encoded, "format": audio_format}})
        return tuple(parts)

    def load_video(self, resolved: ResolvedContract, inputs: tuple[WorkflowVideoInput, ...], *,
                   capabilities: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], ...]:
        if any(type(item) is not WorkflowVideoInput for item in inputs):
            raise WorkflowValidationError("video_input_mismatch")
        self.authorize(resolved, inputs, capabilities=capabilities)
        cache: dict[str, tuple[tuple[ArtifactReference, str], str]] = {}
        parts = []
        for item in inputs:
            identity = (item.reference, item.content_type)
            previous = cache.get(item.reference.artifact_id)
            if previous is None:
                data = self._read(resolved, item, self.artifacts)
                encoded = base64.b64encode(data).decode("ascii")
                cache[item.reference.artifact_id] = (identity, encoded)
            elif previous[0] != identity:
                raise WorkflowValidationError("video_input_mismatch")
            else:
                encoded = previous[1]
            parts.append({"type": "video_url", "video_url": {"url": f"data:{item.content_type};base64,{encoded}"}})
        return tuple(parts)
