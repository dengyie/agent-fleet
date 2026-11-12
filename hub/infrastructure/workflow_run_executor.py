"""Registered Run adapter; reuses the existing durable legacy Task outbox."""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Protocol, TypedDict
from hub.domain.task import DomainError, TaskStopObservation

from hub.application.task_service import ApplicationError
from hub.application.workflow_dispatch import WorkflowDispatchError
from hub.domain.task import validate_task_input
from hub.domain.capability import CapabilityStoreError, TargetBinding
from hub.domain.workflow_contract import parse_resolved
from hub.domain.workflow_contract_types import CatalogRef, CodeRevisionReference, ResolvedContract
from hub.domain.workflow_errors import WorkflowPersistenceError, WorkflowValidationError
from hub.domain.workflow_execution import ExecutionBinding, ExecutionObservation, ExecutorCapabilities, HubInputTarget
from hub.domain.workflow_identity import canonical_digest, canonical_json, validate_reference
from hub.domain.workflow_json import decode_snapshot
from hub.domain.workflow_review import ReviewAdapterSpec
from hub.domain.workflow_native_memory import NATIVE_CODE_MODE_MARKER, NATIVE_MEDIA_MODE_MARKER, NATIVE_MEMORY_MARKER, NATIVE_TEXT_MODE_MARKER, NativeWorkflowMemory, build_native_memory
from hub.domain.workflow_media_inputs import WorkflowAudioInput, WorkflowMediaInput, WorkflowVideoInput, artifact_media_references
from hub.domain.workflow_summary import validate_context_selection
from hub.domain.workflow_text_inputs import WorkflowTextInput, text_artifact_references
from hub.domain.remote_workflow_test import REMOTE_TEST_MARKER
from hub.application.workflow_text_inputs import WorkflowTextInputs
from hub.application.workflow_code_inputs import WorkflowCodeInputs
from fleet_core.workflow.code_inputs import CodeRevisionInput, code_revision_inputs
from hub.infrastructure.workflow_repository import WorkflowRepository, _time
from .capability_dispatch import CAPABILITY_RUN_MARKER, CapabilityDispatchRepository, pinned_dispatch
from .workflow_memory_repository import WorkflowMemoryRepository
from .workflow_summary_repository import WorkflowSummaryRepository
from .workflow_native_memory_repository import saved_native_memory
from .workflow_schema import inspect_schema
from hub.platform.async_io import run_owned_io
from platform_schema import MAX_SECRET_REF

_RUN_STATES = {
    "queued": "running", "waiting_task": "waiting_external", "running": "running",
    "waiting_approval": "waiting_approval", "waiting_node": "waiting_node", "paused": "paused",
    "cancelling": "cancelling", "succeeded": "succeeded", "failed": "failed", "unknown": "unknown", "cancelled": "cancelled",
}


def _valid_provider_snapshot(value: Any) -> bool:
    if type(value) is not dict or set(value) not in (
            {"provider", "model", "provider_config", "capabilities"},
            {"provider", "model", "provider_config", "capabilities", "secret_ref"}):
        return False
    if (any(type(value[key]) is not str or not value[key] for key in ("provider", "model"))
            or any(type(value[key]) is not dict for key in ("provider_config", "capabilities"))):
        return False
    secret_ref = value.get("secret_ref")
    return "secret_ref" not in value or secret_ref is None or (
        type(secret_ref) is str and bool(secret_ref.strip()) and len(secret_ref) <= MAX_SECRET_REF
    )


class LegacyTaskRequest(TypedDict):
    machine: str
    agent_type: str
    project: str
    confirm: bool
    instruction: str


@dataclass(frozen=True, slots=True)
class LegacyTaskTarget:
    machine: str
    agent_type: str
    project: str
    confirm: bool = False

    def __post_init__(self) -> None:
        if type(self.confirm) is not bool:
            raise WorkflowValidationError("invalid_legacy_target")
        validate_task_input({"machine": self.machine, "agent_type": self.agent_type, "project": self.project,
                             "instruction": "Validate registered target", "confirm": self.confirm})


@dataclass(frozen=True, slots=True)
class RegisteredRunTarget:
    owner_id: str
    workspace_id: str
    target_key: str
    adapter_ref: CatalogRef
    policy_ref: CatalogRef
    conversation_id: str
    config_snapshot: bytes
    legacy_task: LegacyTaskTarget | None = None
    test_recipe: CatalogRef | None = None
    source_repository_id: str | None = None
    review_adapter: ReviewAdapterSpec | None = None
    inline_text_inputs: bool = False
    inline_code_inputs: bool = False
    remote_test: bool = False
    inline_media_inputs: bool = False

    def __post_init__(self) -> None:
        for value in (self.owner_id, self.workspace_id, self.target_key, self.conversation_id):
            validate_reference(value)
        for ref in (self.adapter_ref, self.policy_ref):
            if type(ref) is not CatalogRef or type(ref.revision) is not int or not 1 <= ref.revision <= 9007199254740991:
                raise WorkflowValidationError("invalid_catalog_reference")
            validate_reference(ref.id)
        config = decode_snapshot(self.config_snapshot)
        if (type(self.inline_text_inputs) is not bool or type(self.inline_code_inputs) is not bool
                or type(self.inline_media_inputs) is not bool
                or type(self.remote_test) is not bool):
            raise WorkflowValidationError("invalid_text_input_support")
        if {"workflow_dispatch", "workflow_test", "workflow_source", "workflow_review", REMOTE_TEST_MARKER, NATIVE_MEMORY_MARKER, NATIVE_TEXT_MODE_MARKER, "workflow_code_mode", NATIVE_MEDIA_MODE_MARKER, CAPABILITY_RUN_MARKER} & config.keys() or any(
            config.get(key, expected) != expected for key, expected in (("owner_id", self.owner_id), ("workspace_id", self.workspace_id))
        ):
            raise WorkflowValidationError("invalid_run_configuration")
        if config.get("tool_policy") not in (None, "acceptance_read_only"):
            raise WorkflowValidationError("unsupported_run_policy")
        if self.legacy_task is not None and type(self.legacy_task) is not LegacyTaskTarget:
            raise WorkflowValidationError("invalid_legacy_target")
        if self.source_repository_id is not None:
            validate_reference(self.source_repository_id)
            if self.legacy_task is None or self.test_recipe is not None:
                raise WorkflowValidationError("invalid_source_target")
        if self.test_recipe is not None:
            if type(self.test_recipe) is not CatalogRef or type(self.test_recipe.revision) is not int or self.test_recipe != CatalogRef("coding-delivery-tests", 1):
                raise WorkflowValidationError("invalid_test_recipe")
            allowed = {"owner_id", "workspace_id", "execution_node_id"} if self.remote_test else {"owner_id", "workspace_id"}
            if self.legacy_task is not None or config.keys() - allowed:
                raise WorkflowValidationError("invalid_test_run_configuration")
        if self.remote_test and (self.test_recipe is None or self.target_key in {"local", "hub", "host"}
                or config.get("execution_node_id") != self.target_key):
            raise WorkflowValidationError("invalid_remote_test_configuration")
        if self.review_adapter is not None and (type(self.review_adapter) is not ReviewAdapterSpec or
                self.legacy_task is not None or self.test_recipe is not None or self.source_repository_id is not None or
                config.keys() - {"owner_id", "workspace_id"} or self.review_adapter.adapter_ref != self.adapter_ref):
            raise WorkflowValidationError("invalid_review_run_configuration")
        provider = config.get("provider_snapshot")
        if self.inline_text_inputs and (
                self.legacy_task is not None or self.test_recipe is not None or self.review_adapter is not None
                or self.source_repository_id is not None or "memory_context" in config
                or config.get("execution_node_id") not in (None, "", "local", "hub", "host")
                or not _valid_provider_snapshot(provider)):
            raise WorkflowValidationError("unsupported_text_input_target")
        if self.inline_code_inputs and (
                self.legacy_task is not None or self.test_recipe is not None or self.review_adapter is not None
                or self.source_repository_id is not None or self.remote_test or "memory_context" in config
                or config.get("execution_node_id") not in (None, "", "local", "hub", "host")
                or not _valid_provider_snapshot(provider)):
            raise WorkflowValidationError("unsupported_code_input_target")
        if self.inline_media_inputs and (
                self.legacy_task is not None or self.test_recipe is not None or self.review_adapter is not None
                or self.source_repository_id is not None or self.remote_test or "memory_context" in config
                or config.get("execution_node_id") not in (None, "", "local", "hub", "host")
                or not _valid_provider_snapshot(provider)
                or not any(provider["capabilities"].get(name) is True for name in ("image_input", "audio_input", "video_input"))):
            raise WorkflowValidationError("unsupported_media_input_target")

    def hub_input_target(self) -> HubInputTarget | None:
        if self.legacy_task is not None or self.remote_test:
            return None
        if self.test_recipe is not None:
            kind = "validation"
        elif self.review_adapter is not None:
            kind = "coding_task"
        elif self.inline_text_inputs or self.inline_code_inputs or self.inline_media_inputs:
            kind = "native_run"
        else:
            return None
        return HubInputTarget(self.owner_id, self.workspace_id, self.target_key, self.adapter_ref, self.policy_ref, kind)


class LegacyExecutionControl(Protocol):
    def request_cancel(self, owner_id: str, run_id: str) -> TaskStopObservation: ...

    def inspect_stop(self, owner_id: str, run_id: str) -> TaskStopObservation | None: ...


class RunStepExecutor:
    capabilities = ExecutorCapabilities(idempotent=True, lookup=True, cancel=True)

    def __init__(self, repository: WorkflowRepository, target: RegisteredRunTarget, *, clock: Callable[[], float],
                 legacy_control: LegacyExecutionControl | None = None,
                 capability_dispatch: CapabilityDispatchRepository | None = None,
                 text_inputs: WorkflowTextInputs | None = None,
                 code_inputs: WorkflowCodeInputs | None = None,
                 media_inputs=None) -> None:
        self.repository = repository
        self.target = target
        self.clock = clock
        self.legacy_control = legacy_control
        self.capability_dispatch = capability_dispatch
        self.text_inputs = text_inputs
        self.code_inputs = code_inputs
        self.media_inputs = media_inputs

    async def create_or_lookup(self, token: str, payload_hash: str, resolved: ResolvedContract) -> ExecutionBinding:
        return await run_owned_io(self._create, token, payload_hash, resolved)

    async def lookup(self, token: str, payload_hash: str, resolved: ResolvedContract) -> ExecutionBinding | None:
        return await run_owned_io(self._lookup, token, payload_hash, resolved)

    async def inspect(self, binding: ExecutionBinding) -> ExecutionObservation:
        return await run_owned_io(self._inspect, binding)

    async def request_cancel(self, binding: ExecutionBinding) -> ExecutionObservation:
        return await run_owned_io(self._cancel, binding)

    def _cancel(self, binding: ExecutionBinding) -> ExecutionObservation:
        from hub.infrastructure.platform_db import PlatformRepository, PlatformRepositoryError

        observation = self._inspect(binding)
        if observation.state in ("succeeded", "failed", "cancelled"):
            return observation
        try:
            PlatformRepository(self.repository.db_path).cancel_run(binding.owner_id, binding.run_id, now=self.clock())
            if self.target.legacy_task is not None and self.legacy_control is not None:
                stopped = self.legacy_control.request_cancel(binding.owner_id, binding.run_id)
                if stopped.stopped:
                    state = stopped.state if stopped.state in ("succeeded", "failed") else "cancelled"
                    return ExecutionObservation(binding, state, reason_code="legacy_stop_confirmed",
                                                task_id=stopped.task_id, attempt_id=stopped.attempt_id)
        except (PlatformRepositoryError, ApplicationError, DomainError) as exc:
            raise WorkflowDispatchError("executor_unavailable") from exc
        return self._inspect(binding)

    def _request(self, resolved: ResolvedContract, *, capability_binding: TargetBinding | None = None,
                 memory: NativeWorkflowMemory | None = None,
                 target: RegisteredRunTarget | None = None) -> tuple[bytes, LegacyTaskRequest | None]:
        target = self.target if target is None else target
        resolved_codes = code_revision_inputs(resolved)
        has_complete_inputs = memory is not None or bool(resolved.contract.context_refs or
            (target.inline_text_inputs and resolved.contract.input_refs) or (target.inline_code_inputs and resolved_codes)
            or (target.inline_media_inputs and resolved.contract.input_refs))
        config = decode_snapshot(target.config_snapshot)
        config.update(owner_id=target.owner_id, workspace_id=target.workspace_id)
        text_refs = tuple(item for item in resolved.contract.input_refs if type(item.reference) is not CodeRevisionReference)
        if target.inline_text_inputs and text_refs:
            config[NATIVE_TEXT_MODE_MARKER] = {"revision": 1}
        if target.inline_code_inputs and resolved_codes:
            config[NATIVE_CODE_MODE_MARKER] = {"revision": 1}
        if target.inline_media_inputs and resolved.contract.input_refs:
            config[NATIVE_MEDIA_MODE_MARKER] = {"revision": 1}
        if memory is not None:
            config[NATIVE_MEMORY_MARKER] = decode_snapshot(memory.snapshot)
        if capability_binding is not None:
            self._capability_target(capability_binding, target=target)
            config[CAPABILITY_RUN_MARKER] = {"binding_digest": hashlib.sha256(capability_binding.snapshot).hexdigest()}
        if target.test_recipe is not None:
            config["workflow_test"] = {"recipe_id": target.test_recipe.id, "revision": target.test_recipe.revision}
        if target.remote_test:
            config[REMOTE_TEST_MARKER] = {"revision": 1}
        if target.source_repository_id is not None:
            config["workflow_source"] = {"repository_id": target.source_repository_id, "revision": 1}
        if target.review_adapter is not None:
            config["workflow_review"] = target.review_adapter.payload()
        request: LegacyTaskRequest | None = None
        if target.legacy_task is not None:
            legacy = target.legacy_task
            request = {"machine": legacy.machine, "agent_type": legacy.agent_type, "project": legacy.project,
                       "confirm": legacy.confirm, "instruction": resolved.snapshot.decode("utf-8")}
        try:
            digest = canonical_digest({
                "target_key": target.target_key, "adapter_ref": {"id": target.adapter_ref.id, "revision": target.adapter_ref.revision},
                "conversation_id": target.conversation_id, "config": config, "legacy_request": request,
            })
            config["workflow_dispatch"] = {"payload_hash": resolved.digest, "config_digest": digest}
            encoded = canonical_json(config)
        except WorkflowValidationError as exc:
            if has_complete_inputs and exc.code == "json_too_large":
                raise WorkflowDispatchError("context_too_large", uncertain=False) from exc
            raise
        if has_complete_inputs and len(encoded) + len(',"token":"' + "0" * 64 + '"') > 65536:
            raise WorkflowDispatchError("context_too_large", uncertain=False)
        return encoded, request

    def _capability_target(self, binding: TargetBinding, *, target: RegisteredRunTarget | None = None) -> None:
        target = self.target if target is None else target
        config = decode_snapshot(target.config_snapshot)
        if (target.legacy_task is not None or (target.test_recipe is not None and not target.remote_test) or target.review_adapter is not None
                or (target.target_key, config.get("execution_node_id")) != (binding.node_id, binding.node_id)):
            raise WorkflowDispatchError("unsupported_capability_target", uncertain=False)

    def _validate(self, token: str, payload_hash: str, resolved: ResolvedContract, *, target: RegisteredRunTarget | None = None) -> None:
        target = self.target if target is None else target
        if type(resolved) is not ResolvedContract or parse_resolved(resolved.snapshot) != resolved or resolved.digest != payload_hash:
            raise WorkflowDispatchError("dispatch_conflict", uncertain=False)
        codes = code_revision_inputs(resolved)
        try:
            media_refs = artifact_media_references(resolved) if target.inline_media_inputs else ()
        except WorkflowValidationError as exc:
            raise WorkflowDispatchError(exc.code, uncertain=False) from exc
        native_input_target = (target.legacy_task is None and target.test_recipe is None
            and target.review_adapter is None)
        if codes and native_input_target and not target.inline_code_inputs:
            raise WorkflowDispatchError("artifact_input_unsupported", uncertain=False)
        non_code_refs = tuple(item for item in resolved.contract.input_refs if type(item.reference) is not CodeRevisionReference)
        if target.inline_code_inputs and codes and non_code_refs and not target.inline_text_inputs:
            raise WorkflowDispatchError("artifact_input_unsupported", uncertain=False)
        if target.inline_media_inputs and resolved.contract.input_refs and (
                not media_refs or target.inline_text_inputs or target.inline_code_inputs or resolved.contract.context_refs):
            raise WorkflowDispatchError("media_input_unsupported", uncertain=False)
        if resolved.contract.context_refs or (target.inline_text_inputs and non_code_refs) or (target.inline_code_inputs and codes) or (target.inline_media_inputs and media_refs):
            config = decode_snapshot(target.config_snapshot)
            if (target.legacy_task is not None or target.test_recipe is not None
                    or target.review_adapter is not None or target.source_repository_id is not None
                    or config.get("execution_node_id") not in (None, "", "local", "hub", "host")
                    or "memory_context" in config or type(config.get("provider_snapshot")) is not dict
                    or resolved.contract.capability_requirements):
                raise WorkflowDispatchError("context_resolution_required", uncertain=False)
            try:
                validate_context_selection(tuple(named.reference for named in resolved.contract.context_refs))
            except WorkflowValidationError as exc:
                raise WorkflowDispatchError("context_resolution_required", uncertain=False) from exc
            if target.inline_text_inputs and non_code_refs:
                try:
                    text_artifact_references(non_code_refs)
                except WorkflowValidationError as exc:
                    raise WorkflowDispatchError(exc.code, uncertain=False) from exc
            if target.inline_media_inputs and media_refs:
                from hub.domain.workflow_media_inputs import (
                    MAX_MEDIA_INPUT_BYTES,
                    MAX_MEDIA_INPUTS,
                    MAX_MEDIA_ITEM_BYTES,
                )
                if (len(media_refs) > MAX_MEDIA_INPUTS or sum(reference.size for _, reference in media_refs) > MAX_MEDIA_INPUT_BYTES
                        or any(reference.size > MAX_MEDIA_ITEM_BYTES for _, reference in media_refs)):
                    raise WorkflowDispatchError("media_inputs_too_large", uncertain=False)
                provider = config.get("provider_snapshot")
                if type(provider) is not dict or not any(provider.get("capabilities", {}).get(name) is True
                        for name in ("image_input", "audio_input", "video_input")):
                    raise WorkflowDispatchError("media_input_unsupported", uncertain=False)
            if codes:
                try:
                    self._validate_code_refs(resolved, target)
                except WorkflowValidationError as exc:
                    raise WorkflowDispatchError(exc.code, uncertain=False) from exc
        raw = decode_snapshot(resolved.snapshot)
        if (resolved.owner_id, resolved.workspace_id, resolved.contract.policy_ref) != (
            target.owner_id, target.workspace_id, target.policy_ref,
        ) or raw["adapter_ref"] != {"id": target.adapter_ref.id, "revision": target.adapter_ref.revision}:
            raise WorkflowDispatchError("dispatch_conflict", uncertain=False)
        # Binding validates the exact stable digest without assuming a Workflow ID is a Run ID.
        self._binding(token, payload_hash, target=target)
        if target.source_repository_id is not None:
            from hub.application.workflow_templates import coding_delivery_definition

            if resolved.step_key != "develop" or resolved.contract != coding_delivery_definition().steps[0].contract:
                raise WorkflowDispatchError("invalid_source_contract", uncertain=False)
        if target.test_recipe is not None:
            from hub.application.workflow_templates import validate_coding_test_contract

            try:
                validate_coding_test_contract(resolved)
            except WorkflowValidationError as exc:
                raise WorkflowDispatchError(exc.code, uncertain=False) from exc
        if target.review_adapter is not None:
            from hub.application.workflow_templates import validate_coding_validation_contract

            try:
                validate_coding_validation_contract(resolved, "review")
            except WorkflowValidationError as exc:
                raise WorkflowDispatchError(exc.code, uncertain=False) from exc

    def _binding(self, token: str, payload_hash: str, *, target: RegisteredRunTarget | None = None) -> ExecutionBinding:
        target = self.target if target is None else target
        run_id = "wf-run-" + token
        return ExecutionBinding(target.owner_id, target.workspace_id, target.target_key,
                                token, payload_hash, "run", run_id, run_id)

    def _existing(self, connection: sqlite3.Connection, token: str, payload_hash: str, resolved: ResolvedContract, *,
                  target: RegisteredRunTarget | None = None) -> ExecutionBinding | None:
        target = self.target if target is None else target
        binding = self._binding(token, payload_hash, target=target)
        row = connection.execute(
            "SELECT owner_id,conversation_id,config_snapshot,trigger_message_id FROM runs WHERE run_id=?", (binding.run_id,),
        ).fetchone()
        if row is None:
            return None
        pinned = pinned_dispatch(connection, token)
        codes = code_revision_inputs(resolved)
        text_required = target.inline_text_inputs and any(type(item.reference) is not CodeRevisionReference for item in resolved.contract.input_refs)
        code_required = target.inline_code_inputs and bool(codes)
        media_required = target.inline_media_inputs and bool(resolved.contract.input_refs)
        memory = saved_native_memory(connection, token, resolved=resolved) if resolved.contract.context_refs or text_required or code_required or media_required else None
        if text_required and (memory is None or not memory.texts):
            raise WorkflowDispatchError("dispatch_conflict", uncertain=False)
        if code_required and (memory is None or memory.codes != codes):
            raise WorkflowDispatchError("dispatch_conflict", uncertain=False)
        if media_required and (memory is None or not (memory.uses_media or memory.uses_audio or memory.uses_video)):
            raise WorkflowDispatchError("dispatch_conflict", uncertain=False)
        config, request = self._request(resolved, capability_binding=pinned.binding if pinned is not None else None, memory=memory, target=target)
        expected = decode_snapshot(config)
        expected["workflow_dispatch"]["token"] = token
        message = connection.execute("SELECT content,client_token,owner_id FROM messages WHERE message_id=?", (row["trigger_message_id"],)).fetchone()
        if (row["owner_id"], row["conversation_id"], decode_snapshot(row["config_snapshot"].encode())) != (
            target.owner_id, target.conversation_id, expected,
        ) or message is None or (message["content"], message["client_token"], message["owner_id"]) != (
            resolved.snapshot.decode(), token, target.owner_id,
        ):
            raise WorkflowDispatchError("dispatch_conflict", uncertain=False)
        legacy = connection.execute("SELECT request FROM legacy_task_links WHERE owner_id=? AND run_id=?",
                                    (target.owner_id, binding.run_id)).fetchone()
        if (request is None) != (legacy is None) or (legacy is not None and decode_snapshot(legacy[0].encode()) != request):
            raise WorkflowDispatchError("dispatch_conflict", uncertain=False)
        return binding

    @staticmethod
    def _validate_code_refs(resolved: ResolvedContract, target: RegisteredRunTarget) -> tuple[CodeRevisionInput, ...]:
        if (not target.inline_code_inputs or target.legacy_task is not None or target.test_recipe is not None
                or target.review_adapter is not None or target.source_repository_id is not None
                or resolved.contract.capability_requirements):
            raise WorkflowValidationError("unsupported_code_input_target")
        return code_revision_inputs(resolved)

    def _lookup(self, token: str, payload_hash: str, resolved: ResolvedContract) -> ExecutionBinding | None:
        target = self.target
        self._validate(token, payload_hash, resolved, target=target)
        try:
            with self.repository.transaction(write=False) as connection:
                return self._existing(connection, token, payload_hash, resolved, target=target)
        except WorkflowValidationError as exc:
            raise WorkflowDispatchError(exc.code, uncertain=False) from exc
        except WorkflowPersistenceError as exc:
            raise WorkflowDispatchError("executor_unavailable") from exc

    def _create(self, token: str, payload_hash: str, resolved: ResolvedContract) -> ExecutionBinding:
        target = self.target
        self._validate(token, payload_hash, resolved, target=target)
        config, request = self._request(resolved, target=target)
        if self.target != target:
            raise WorkflowDispatchError("dispatch_conflict", uncertain=False)
        if request is not None and len(request["instruction"]) > 2000:
            raise WorkflowDispatchError("context_too_large", uncertain=False)
        if len(resolved.snapshot) > 32768:
            raise WorkflowDispatchError("context_too_large", uncertain=False)
        try:
            texts: tuple[WorkflowTextInput, ...] = ()
            code_refs = code_revision_inputs(resolved)
            code_required = target.inline_code_inputs and bool(code_refs)
            text_refs = tuple(item for item in resolved.contract.input_refs if type(item.reference) is not CodeRevisionReference)
            text_required = target.inline_text_inputs and bool(text_refs)
            media_required = target.inline_media_inputs and bool(resolved.contract.input_refs)
            media: tuple = ()
            audio_mode = False
            video_mode = False
            if code_required or text_required or media_required:
                with self.repository.transaction(write=False) as connection:
                    existing = self._existing(connection, token, payload_hash, resolved, target=target)
                if existing is not None:
                    return existing
            if text_required:
                reader = self.text_inputs
                if reader is None:
                    raise WorkflowDispatchError("artifact_input_unavailable", uncertain=False)
                texts = reader.collect(resolved, references=text_refs)
                reader.authorize(resolved, texts, references=text_refs)
                if self.target != target or self.text_inputs is not reader:
                    raise WorkflowDispatchError("dispatch_conflict", uncertain=False)
            if code_required:
                reader = self.code_inputs
                if reader is None:
                    raise WorkflowDispatchError("code_input_unavailable", uncertain=False)
                reader.authorize(resolved, code_refs)
                if self.target != target or self.code_inputs is not reader:
                    raise WorkflowDispatchError("dispatch_conflict", uncertain=False)
            if media_required:
                reader = self.media_inputs
                if reader is None:
                    raise WorkflowDispatchError("media_input_unavailable", uncertain=False)
                provider = decode_snapshot(target.config_snapshot).get("provider_snapshot", {})
                capabilities = provider.get("capabilities", {}) if type(provider) is dict else {}
                media = reader.collect(resolved, capabilities=capabilities)
                reader.authorize(resolved, media, capabilities=capabilities)
                if self.target != target or self.media_inputs is not reader:
                    raise WorkflowDispatchError("dispatch_conflict", uncertain=False)
                audio_mode = bool(media) and all(type(item) is WorkflowAudioInput for item in media)
                image_mode = bool(media) and all(type(item) is WorkflowMediaInput for item in media)
                video_mode = bool(media) and all(type(item) is WorkflowVideoInput for item in media)
                if not (audio_mode or image_mode or video_mode):
                    raise WorkflowDispatchError("media_input_unsupported", uncertain=False)
            with self.repository.transaction() as connection:
                now = _time(self.clock())
                if self.target != target:
                    raise WorkflowDispatchError("dispatch_conflict", uncertain=False)
                existing = self._existing(connection, token, payload_hash, resolved, target=target)
                if existing is not None:
                    return existing
                intent = connection.execute(
                    "SELECT d.owner_id,d.workspace_id,d.target_key,d.payload_hash,d.stage,d.binding_run_id,"
                    "s.kind,s.is_current,s.active_deadline,r.cancel_requested,r.pause_requested,m.admission_mode "
                    "FROM workflow_dispatches d JOIN workflow_steps s USING(owner_id,workspace_id,workflow_id,step_key,generation) "
                    "JOIN workflow_runs r USING(owner_id,workspace_id,workflow_id) CROSS JOIN workflow_schema_meta m "
                    "WHERE d.token=? AND m.singleton=1", (token,),
                ).fetchone()
                if intent is None or (intent["owner_id"], intent["workspace_id"], intent["target_key"], intent["payload_hash"]) != (
                    resolved.owner_id, resolved.workspace_id, target.target_key, payload_hash,
                ):
                    raise WorkflowDispatchError("dispatch_conflict", uncertain=False)
                if intent["stage"] != "sending" or intent["is_current"] != 1 or intent["admission_mode"] != "open" or intent["cancel_requested"] or intent["pause_requested"]:
                    raise WorkflowDispatchError("admission_closed", uncertain=False)
                if (text_required or code_required or media_required) and (intent["active_deadline"] is None or intent["active_deadline"] <= now):
                    raise WorkflowDispatchError("admission_closed", uncertain=False)
                pinned = pinned_dispatch(connection, token)
                if target.remote_test and pinned is None:
                    raise WorkflowDispatchError("capability_unavailable", uncertain=False)
                if pinned is not None:
                    self._capability_target(pinned.binding, target=target)
                    if self.capability_dispatch is None:
                        raise WorkflowDispatchError("capability_unavailable", uncertain=False)
                    self.capability_dispatch.validate_dispatch(connection, token, now=now)
                    config, request = self._request(resolved, capability_binding=pinned.binding, target=target)
                if target.remote_test:
                    connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=max(minimum_reader_version,10) WHERE singleton=1")
                expected_kind = "coding_task" if request is not None else "native_run"
                if target.test_recipe is not None:
                    expected_kind = "validation"
                if target.review_adapter is not None:
                    expected_kind = "coding_task"
                if intent["kind"] != expected_kind:
                    raise WorkflowDispatchError("unsupported_executor_kind", uncertain=False)
                if resolved.contract.context_refs or texts or code_required or media_required:
                    inspect_schema(connection)
                    items = WorkflowMemoryRepository(self.repository).read_selected(resolved.owner_id,
                        tuple(named.reference for named in resolved.contract.context_refs if named.reference.kind == "memory"), connection=connection)
                    summary_refs = tuple(named for named in resolved.contract.context_refs if named.reference.kind == "summary")
                    summary_items = WorkflowSummaryRepository(self.repository).read_selected(resolved.owner_id, resolved.workspace_id,
                        tuple(named.reference for named in summary_refs), connection=connection)
                    memory = build_native_memory(resolved, items, texts=texts,
                        summaries=tuple((named.ref_key, item) for named, item in zip(summary_refs, summary_items)),
                        codes=code_refs if code_required else (), media=media if not (audio_mode or video_mode) else (),
                        media_mode=media_required and not (audio_mode or video_mode),
                        audio=media if audio_mode else (), audio_mode=audio_mode,
                        video=media if video_mode else (), video_mode=video_mode)
                    config, request = self._request(resolved, memory=memory, target=target)
                    reader_version = 15 if video_mode else (14 if audio_mode else (13 if media_required else 12 if code_required else 11 if summary_items else 9 if texts else 7))
                    connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=max(minimum_reader_version,?) WHERE singleton=1",
                                       (reader_version,))
                conversation = connection.execute(
                    "SELECT 1 FROM conversations c JOIN workspaces w USING(owner_id,workspace_id) "
                    "WHERE c.owner_id=? AND c.conversation_id=? AND c.workspace_id=? AND c.archived_at IS NULL AND w.enabled=1",
                    (resolved.owner_id, target.conversation_id, resolved.workspace_id),
                ).fetchone()
                if conversation is None:
                    raise WorkflowDispatchError("reference_forbidden", uncertain=False)
                frozen = decode_snapshot(config)
                if request is None and target.test_recipe is None and target.review_adapter is None:
                    self._check_native_config(connection, frozen, target=target)
                binding = self._binding(token, payload_hash, target=target)
                if intent["binding_run_id"] is not None:
                    raise WorkflowDispatchError("execution_missing")
                frozen["workflow_dispatch"]["token"] = token
                message_id = "wf-msg-" + token
                connection.execute(
                    "INSERT INTO messages(message_id,conversation_id,owner_id,role,content_ref,content,client_token,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (message_id, target.conversation_id, resolved.owner_id, "user", "inline", resolved.snapshot.decode(), token, str(now)),
                )
                connection.execute(
                    "INSERT INTO runs(run_id,conversation_id,owner_id,trigger_message_id,state,config_snapshot,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (binding.run_id, target.conversation_id, resolved.owner_id, message_id,
                     "waiting_task" if request is not None else "queued", canonical_json(frozen).decode(), now, now),
                )
                if request is not None:
                    encoded_request = canonical_json(request)
                    connection.execute(
                        "INSERT INTO legacy_task_links(owner_id,run_id,request,request_hash,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (resolved.owner_id, binding.run_id, encoded_request.decode(), hashlib.sha256(encoded_request).hexdigest(), "pending", now, now),
                    )
                    connection.execute("INSERT INTO run_events VALUES(?,1,'legacy_task_queued',?,?)",
                                       (binding.run_id, '{"bridge_state":"pending","state":"waiting_task"}', now))
                connection.execute("UPDATE workflow_dispatches SET binding_run_id=?,updated_at=? WHERE token=?",
                                   (binding.run_id, now, token))
                connection.execute(
                    "UPDATE workflow_budget_reservations SET child_request_id=? WHERE (owner_id,workflow_id,step_key,generation)="
                    "(SELECT owner_id,workflow_id,step_key,generation FROM workflow_dispatches WHERE token=?)", (binding.run_id, token),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO workflow_budget_checks(owner_id,workflow_id,step_key,generation) "
                    "SELECT owner_id,workflow_id,step_key,generation FROM workflow_dispatches WHERE token=?", (token,),
                )
                if self.target != target:
                    raise WorkflowDispatchError("dispatch_conflict", uncertain=False)
                return binding
        except CapabilityStoreError as exc:
            raise WorkflowDispatchError(exc.code, uncertain=False) from exc
        except WorkflowValidationError as exc:
            raise WorkflowDispatchError(exc.code, uncertain=False) from exc
        except WorkflowPersistenceError as exc:
            raise WorkflowDispatchError("executor_unavailable") from exc

    def _check_native_config(self, connection: sqlite3.Connection, frozen: dict[str, Any], *,
                             target: RegisteredRunTarget | None = None) -> None:
        target = self.target if target is None else target
        provider = frozen.get("provider_snapshot")
        if not _valid_provider_snapshot(provider):
            raise WorkflowDispatchError("invalid_run_configuration", uncertain=False)
        profile = frozen.get("model_profile_id")
        model = connection.execute("SELECT enabled FROM model_profiles WHERE owner_id=? AND profile_id=?",
                                   (target.owner_id, profile)).fetchone()
        if model is None or model[0] != 1:
            raise WorkflowDispatchError("reference_forbidden", uncertain=False)
        node = frozen.get("execution_node_id")
        if node not in (None, "", "local", "hub", "host") and connection.execute("SELECT 1 FROM nodes WHERE owner_id=? AND node_id=? AND enabled=1",
                                                   (target.owner_id, node)).fetchone() is None:
            raise WorkflowDispatchError("reference_forbidden", uncertain=False)

    def _inspect(self, binding: ExecutionBinding) -> ExecutionObservation:
        if type(binding) is not ExecutionBinding or binding != self._binding(binding.token, binding.payload_hash):
            raise WorkflowDispatchError("dispatch_conflict", uncertain=False)
        try:
            with self.repository.transaction(write=False) as connection:
                row = connection.execute(
                    "SELECT r.state,r.config_snapshot,l.task_id,l.attempt_id,l.task_state,s.resolved_snapshot FROM runs r "
                    "LEFT JOIN legacy_task_links l USING(owner_id,run_id) "
                    "JOIN workflow_dispatches d ON d.owner_id=r.owner_id AND d.binding_run_id=r.run_id "
                    "JOIN workflow_steps s ON s.owner_id=d.owner_id AND s.workflow_id=d.workflow_id AND s.step_key=d.step_key AND s.generation=d.generation "
                    "WHERE r.owner_id=? AND r.run_id=? AND d.token=?", (binding.owner_id, binding.run_id, binding.token),
                ).fetchone()
                if row is None:
                    raise WorkflowDispatchError("execution_missing")
                marker = decode_snapshot(row["config_snapshot"].encode()).get("workflow_dispatch")
                if type(marker) is not dict or marker.get("token") != binding.token or marker.get("payload_hash") != binding.payload_hash:
                    raise WorkflowDispatchError("dispatch_conflict")
                resolved = parse_resolved(bytes(row["resolved_snapshot"]))
                self._validate(binding.token, binding.payload_hash, resolved)
                self._existing(connection, binding.token, binding.payload_hash, resolved)
                state = _RUN_STATES.get(row["state"])
                if state is None:
                    raise WorkflowDispatchError("invalid_execution_state")
                if self.target.legacy_task is not None and (state == "cancelled" or row["task_state"] in ("cancelled", "expired")):
                    # Legacy cancellation fences receipts; it does not confirm runner exit.
                    observation = ExecutionObservation(binding, "unknown", reason_code="legacy_stop_unconfirmed",
                                                       task_id=row["task_id"], attempt_id=row["attempt_id"])
                else:
                    observation = ExecutionObservation(binding, state, task_id=row["task_id"], attempt_id=row["attempt_id"])
            if self.target.legacy_task is not None and self.legacy_control is not None:
                stopped = self.legacy_control.inspect_stop(binding.owner_id, binding.run_id)
                if stopped is not None and stopped.stopped:
                    state = stopped.state if stopped.state in ("succeeded", "failed") else "cancelled"
                    return ExecutionObservation(binding, state, reason_code="legacy_stop_confirmed",
                                                task_id=stopped.task_id, attempt_id=stopped.attempt_id)
            return observation
        except WorkflowValidationError as exc:
            raise WorkflowDispatchError(exc.code, uncertain=False) from exc
        except (WorkflowPersistenceError, ApplicationError, DomainError) as exc:
            raise WorkflowDispatchError("executor_unavailable") from exc
