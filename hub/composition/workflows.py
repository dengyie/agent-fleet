"""Explicit Workflow assembly on the current split composition root."""
from __future__ import annotations

import secrets
import sqlite3
import time
from contextlib import closing
from functools import partial
from pathlib import Path

from hub.infrastructure.workflow_repository import WorkflowRepository, WorkflowRepositoryError
from hub.infrastructure.workflow_schema import WorkflowSchemaError, inspect_schema


def assert_workflow_visibility(path: Path, *, enabled: bool) -> None:
    """Keep unresolved executions visible even when new dispatch is disabled."""
    if enabled or not path.exists():
        return
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            if inspect_schema(connection) is None:
                return
            from hub.infrastructure.workflow_external_repository import ExternalWorkflowRepository
            from hub.infrastructure.workflow_summary_repository import WorkflowSummaryRepository

            ExternalWorkflowRepository.validate_snapshot(connection)
            WorkflowSummaryRepository.validate_snapshot(connection)
            unresolved = connection.execute(
                "SELECT 1 FROM workflow_dispatches d JOIN workflow_steps s "
                "USING(owner_id,workspace_id,workflow_id,step_key,generation) "
                "WHERE d.stage IN ('sending','bound','unknown') "
                "AND s.state NOT IN ('succeeded','failed','cancelled') LIMIT 1",
            ).fetchone()
            if unresolved is not None:
                raise WorkflowRepositoryError("workflow_domain_in_use")
    except (sqlite3.Error, WorkflowSchemaError) as exc:
        raise WorkflowRepositoryError("workflow_visibility_unavailable") from exc


def wire_workflows(app, config, dependencies) -> None:
    """Build durable coordination and HTTP use cases without starting threads."""
    from hub.application.workflow_budget import WorkflowBudgetService
    from hub.application.workflow_coding_delivery import WorkflowCodingDeliveryService
    from hub.application.workflow_control import WorkflowControlService
    from hub.application.workflow_coordinator import WorkflowCoordinator
    from hub.application.workflow_operator import WorkflowOperatorService
    from hub.application.workflow_review_execution import WorkflowReviewExecutionService
    from hub.application.workflow_templates import WorkflowTemplateCatalog
    from hub.application.workflow_text_inputs import WorkflowTextInputs
    from hub.application.workflow_media_inputs import WorkflowMediaInputs
    from hub.application.workflow_test_execution import WorkflowTestExecutionService
    from fleet_core.backends.sandbox import SandboxBackend
    from hub.platform.workflow_validators import FixedTestRunner
    from hub.infrastructure.workflow_budget import WorkflowBudgetRepository
    from hub.infrastructure.workflow_control_repository import WorkflowControlRepository
    from hub.infrastructure.workflow_delivery_repository import WorkflowDeliveryRepository
    from hub.infrastructure.workflow_input_resolver import WorkflowArtifactInputResolver
    from hub.infrastructure.workflow_inputs import WorkflowArtifactInputAuthorizer
    from hub.infrastructure.workflow_memory_repository import WorkflowMemoryRepository
    from hub.infrastructure.workflow_native_memory_repository import WorkflowNativeMemoryRepository
    from hub.infrastructure.workflow_operator_repository import WorkflowOperatorRepository
    from hub.infrastructure.workflow_run_executor import RunStepExecutor

    fleet = app.extensions["fleet"]
    services = fleet["services"]
    repository = dependencies.get("workflows") or WorkflowRepository(config.platform_db)
    repository.init()
    clock = dependencies.get("workflow_clock", time.time)
    if config.platform_workflow_registration_file is not None:
        from hub.composition.workflow_registration import load_workflow_registration

        loaded = load_workflow_registration(config, fleet, repository, clock=clock)
        if dependencies.keys() & loaded.keys():
            raise ValueError("workflow_registration_conflict")
        dependencies = {**dependencies, **loaded}
    if config.platform_muse_registration_file is not None:
        from hub.composition.muse_registration import load_muse_registration, merge_muse_registration

        dependencies = merge_muse_registration(dependencies,
            load_muse_registration(config.platform_muse_registration_file, fleet["platform_repository"]))
    from hub.application.workflow_summary import WorkflowSummaryService
    from hub.infrastructure.workflow_summary_repository import WorkflowSummaryRepository

    summaries = WorkflowSummaryRepository(repository)
    services["workflow_summaries"] = WorkflowSummaryService(summaries, clock=clock)
    if config.platform_fleet_mcp_enabled:
        from hub.application.integration_identity_admin import IntegrationIdentityAdminService
        from hub.infrastructure.integration_identity_repository import IntegrationIdentityRepository

        identities = IntegrationIdentityRepository(repository)
        identities.init()
        for identity in dependencies.get("integration_identities", ()):
            identities.register(identity, now=clock())
        fleet["repositories"]["integration_identities"] = identities
        services["integration_identity_admin"] = IntegrationIdentityAdminService(identities, clock=clock)
    artifacts = services["platform_artifacts"]
    authorizer = WorkflowArtifactInputAuthorizer(repository, artifacts)
    controls = WorkflowControlService(WorkflowControlRepository(repository), clock=clock, input_authorizer=authorizer)
    catalog = dependencies.get("workflow_templates", WorkflowTemplateCatalog())
    services["workflows"] = WorkflowOperatorService(
        WorkflowOperatorRepository(repository, authorizer), controls, catalog, clock=clock,
        identity=dependencies.get("workflow_identity", lambda: "wf-" + secrets.token_urlsafe(18)),
    )
    budget = WorkflowBudgetService(WorkflowBudgetRepository(repository), clock=clock)
    services["workflow_budget"] = budget
    delivery_repository = WorkflowDeliveryRepository(repository, dependencies.get("task_repository"))
    delivery = WorkflowCodingDeliveryService(
        delivery_repository, artifacts, clock=clock,
        task_repository=dependencies.get("task_repository"),
    )
    services["workflow_coding_delivery"] = delivery
    services["runner"].workflow_delivery = delivery
    services["workflow_test_executor"] = WorkflowTestExecutionService(
        delivery_repository, delivery, clock=clock,
        runner=FixedTestRunner(backend_factory=partial(SandboxBackend, launcher=config.platform_sandbox_launcher)),
    )
    services["workflow_review_executor"] = WorkflowReviewExecutionService(
        delivery_repository, delivery, clock=clock,
        adapters=dependencies.get("workflow_review_adapters", ()),
    )
    text_inputs = WorkflowTextInputs(artifacts)
    media_inputs = WorkflowMediaInputs(artifacts)
    from hub.application.workflow_code_inputs import WorkflowCodeInputs
    code_inputs = WorkflowCodeInputs(artifacts)
    worker = services.get("platform_worker")
    if worker is not None:
        worker.workflow_test_executor = services["workflow_test_executor"]
        worker.workflow_review_executor = services["workflow_review_executor"]
        worker.workflow_memory_guard = WorkflowNativeMemoryRepository(repository, text_inputs=text_inputs,
            code_inputs=code_inputs, media_inputs=media_inputs, clock=clock)
        worker.workflow_code_inputs = code_inputs
    hub_inputs = []
    for owner, workspace, target, executor in dependencies.get("workflow_executors", ()):
        if not isinstance(executor, RunStepExecutor):
            continue
        binding = executor.target
        if (owner, workspace, target) != (binding.owner_id, binding.workspace_id, binding.target_key):
            raise WorkflowRepositoryError("invalid_hub_input_target")
        registration = binding.hub_input_target()
        if registration is not None:
            hub_inputs.append(registration)
        if executor.text_inputs is None:
            executor.text_inputs = text_inputs
        if executor.code_inputs is None:
            executor.code_inputs = code_inputs
        if executor.media_inputs is None:
            executor.media_inputs = media_inputs
    hub_keys = {(item.owner_id, item.workspace_id, item.target_key) for item in hub_inputs}
    if any((item.owner_id, item.workspace_id, item.node_id) in hub_keys
           for item in dependencies.get("workflow_node_inputs", ())):
        raise WorkflowRepositoryError("input_location_conflict")
    repository.configure_hub_input_targets(tuple(hub_inputs))
    from hub.application.workflow_external_execution import ExternalStepExecutor
    from hub.application.workflow_external_inputs import WorkflowExternalInputs
    from hub.application.workflow_external_outputs import WorkflowExternalOutputs
    from hub.infrastructure.workflow_external_repository import ExternalWorkflowRepository

    memories = WorkflowMemoryRepository(repository)
    external_store = ExternalWorkflowRepository(repository)
    if config.platform_external_agents_enabled:
        external_store.init()
    external_executors = tuple((target.owner_id, target.workspace_id, target.target_key,
        ExternalStepExecutor(external_store, target, adapter, clock=clock, enabled=config.platform_external_agents_enabled,
            inputs=WorkflowExternalInputs(artifacts, memories=memories, summaries=summaries), outputs=WorkflowExternalOutputs(artifacts)))
        for target, adapter in dependencies.get("workflow_external_agents", ()))
    services["workflow_coordinator"] = WorkflowCoordinator(
        repository, worker_id="workflow-" + secrets.token_hex(8), clock=clock,
        registrations=dependencies.get("workflow_steps", ()),
        executors=tuple(dependencies.get("workflow_executors", ())) + external_executors,
        dispatch_enabled=config.platform_workflow_dispatch_enabled, budget_service=budget,
        input_resolver=WorkflowArtifactInputResolver(repository, artifacts, memories=memories, summaries=summaries),
        external_agents_enabled=config.platform_external_agents_enabled,
    )
    fleet["repositories"]["workflows"] = repository
    if config.platform_capability_routing_enabled:
        from hub.infrastructure.capability_repository import CapabilityRepository
        from hub.infrastructure.capability_dispatch import CapabilityDispatchRepository

        capabilities = dependencies.get("capabilities") or CapabilityRepository(repository,
            boot_id="capability-" + secrets.token_hex(16))
        capabilities.init()
        for definition in dependencies.get("workflow_capability_definitions", ()):
            capabilities.register_definition(definition, now=clock())
        for policy in dependencies.get("workflow_capability_nodes", ()):
            capabilities.register_node(policy, now=clock())
        fleet["repositories"]["capabilities"] = capabilities
        fleet["capability_clock"] = clock
        preparation = CapabilityDispatchRepository(capabilities,
            actions=dependencies.get("workflow_capability_actions", ()))
        services["capability_dispatch"] = preparation
        services["platform_delivery"].capability_dispatch = preparation
        services["platform_delivery"].repository.capability_guard = preparation.guard_command
        for dispatcher in services["workflow_coordinator"].dispatchers.values():
            if isinstance(dispatcher.executor, RunStepExecutor) and dispatcher.executor.capability_dispatch is None:
                dispatcher.executor.capability_dispatch = preparation
    if getattr(config, "platform_artifact_transfer_enabled", False):
        from hub.application.artifact_transfer_service import ArtifactTransferService
        from hub.infrastructure.artifact_transfer_repository import ArtifactTransferRepository
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        from hub.domain.artifact_transfer import TransferLimits

        signing_key = services["platform_delivery"].signing_key
        if not signing_key:
            raise ValueError("artifact transfer requires command signing")
        public_key = Ed25519PrivateKey.from_private_bytes(signing_key).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        transfer_repository = dependencies.get("artifact_transfers") or ArtifactTransferRepository(
            config.platform_db, command_public_key=public_key, clock=clock)
        transfer_repository.init()
        services["artifact_transfers"] = ArtifactTransferService(
            transfer_repository, services["platform_artifacts"], clock=clock,
            identity=dependencies.get("artifact_transfer_identity", lambda: secrets.token_urlsafe(24)),
            limits=TransferLimits(default_size=getattr(config, "platform_artifact_transfer_max_bytes", 64 * 1024 * 1024)),
        )
        services["artifact_transfers"].recover()
        fleet["repositories"]["artifact_transfers"] = transfer_repository
        services["platform_delivery"].repository.artifact_export_guard = transfer_repository.receipt_guard
        from hub.application.artifact_input_service import ArtifactInputService
        from hub.infrastructure.artifact_input_repository import ArtifactInputRepository
        input_repository = ArtifactInputRepository(repository, services["platform_delivery"].repository,
            signing_key=signing_key, public_key=public_key, limits=services["artifact_transfers"].limits)
        services["platform_delivery"].repository.artifact_receive_guard = input_repository.receipt_guard
        services["artifact_inputs"] = ArtifactInputService(input_repository, services["platform_artifacts"],
            targets=dependencies.get("workflow_node_inputs", ()), clock=clock)
        services["workflow_coordinator"].artifact_input_service = services["artifact_inputs"]
    if config.platform_capability_routing_enabled and services["platform_delivery"].signing_key:
        from hub.infrastructure.capability_workflow import CapabilityWorkflowAdmission

        services["capability_admission"] = CapabilityWorkflowAdmission(capabilities,
            targets=tuple(dispatcher.executor.target for key, dispatcher in services["workflow_coordinator"].dispatchers.items()
                if isinstance(dispatcher.executor, RunStepExecutor) and key == (
                    dispatcher.executor.target.owner_id, dispatcher.executor.target.workspace_id, dispatcher.executor.target.target_key)),
            inputs=services.get("artifact_inputs"))
        services["workflow_coordinator"].capability_admission = services["capability_admission"]
        if services.get("artifact_inputs") is not None and config.platform_remote_execution_enabled:
            from hub.application.remote_workflow_test_execution import RemoteWorkflowTestExecutionService

            services["workflow_remote_test_executor"] = RemoteWorkflowTestExecutionService(delivery_repository,
                delivery, commands=services["platform_delivery"], clock=clock)
            if worker is not None:
                worker.workflow_remote_test_executor = services["workflow_remote_test_executor"]
