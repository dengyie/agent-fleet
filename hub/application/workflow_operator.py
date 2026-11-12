"""Operator workflow use cases and explicit public DTOs."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Literal, ParamSpec, Protocol, TypeVar, TypedDict

from hub.domain.workflow_control import WorkflowControlReceipt, WorkflowControlRequest
from hub.domain.workflow_contract_types import ArtifactReference, CodeRevisionReference, WorkflowDefinition
from hub.domain.workflow_errors import WorkflowPersistenceError, WorkflowValidationError
from hub.domain.workflow_operator import WorkflowAcceptanceRequest, WorkflowCreateRequest
from .workflow_control import WorkflowControlError, WorkflowControlService
from .workflow_templates import WorkflowTemplateCatalog
from hub.domain.integration_identity import IntegrationIdentity
from hub.platform.async_io import run_owned_io


class WorkflowSummaryDTO(TypedDict):
    workflow_id: str
    workspace_id: str
    template_id: str
    template_revision: int
    execution_state: str
    validation_state: str
    acceptance_state: str
    revision: int
    reason_code: str | None
    created_at: str
    updated_at: str


class WorkflowPageDTO(TypedDict):
    workflows: list[WorkflowSummaryDTO]
    next_cursor: str | None


class WorkflowArtifactDTO(TypedDict):
    artifact_id: str
    sha256: str
    size: int


class WorkflowOutputDTO(WorkflowArtifactDTO):
    output_key: str


class WorkflowReportDTO(WorkflowArtifactDTO):
    content_digest: str


class WorkflowEvidenceDTO(TypedDict):
    evidence_key: str
    validator_ref: str
    subject_digest: str
    source: str
    result: str
    report: WorkflowReportDTO


class WorkflowStepDTO(TypedDict):
    step_key: str
    generation: int
    kind: str
    execution_state: str
    validation_state: str
    subject_digest: str | None
    reason_code: str | None
    outputs: list[WorkflowOutputDTO]
    evidence: list[WorkflowEvidenceDTO]


class WorkflowBudgetDTO(TypedDict):
    step_key: str
    reserved_tokens: int | str
    reserved_requests: int | str
    charged_tokens: int | str | None
    charged_requests: int | str | None
    settlement: str
    cost: str | None
    cost_state: str


class WorkflowDetailDTO(WorkflowSummaryDTO):
    delivery_state: str
    delivery_digest: str
    steps: list[WorkflowStepDTO]
    budget: list[WorkflowBudgetDTO]
    cancel_requested: bool
    pause_requested: bool
    admission_mode: str


class WorkflowEventDTO(TypedDict):
    sequence: int
    kind: str
    payload: dict[str, object]
    created_at: str


class WorkflowEventsDTO(TypedDict):
    events: list[WorkflowEventDTO]
    next_cursor: int | None


class WorkflowAcceptanceDTO(TypedDict):
    action: str
    revision: int
    acceptance_state: str
    delivery_digest: str


class WorkflowTemplateStepDTO(TypedDict):
    step_key: str
    kind: str


class WorkflowArtifactInputDTO(WorkflowArtifactDTO):
    ref_key: str
    kind: Literal["artifact"]


class WorkflowCodeRevisionInputDTO(TypedDict):
    ref_key: str
    kind: Literal["code_revision"]
    repository_id: str
    base_commit: str
    head_commit: str
    tree_id: str
    source_bundle: WorkflowArtifactDTO


WorkflowInputDTO = WorkflowArtifactInputDTO | WorkflowCodeRevisionInputDTO


class WorkflowTemplateDTO(TypedDict):
    template_id: str
    revision: int
    workspace_id: str
    policy_revision: int
    steps: list[WorkflowTemplateStepDTO]
    input_refs: list[WorkflowInputDTO]


class WorkflowTemplatesDTO(TypedDict):
    templates: list[WorkflowTemplateDTO]


class WorkflowOperationDTO(TypedDict):
    action: str
    revision: int
    execution_state: str
    generations: list[dict[str, str | int]]


class WorkflowOperatorStore(Protocol):
    def create(self, request: WorkflowCreateRequest, definition: WorkflowDefinition, *, workflow_id: str, now: float,
               integration: IntegrationIdentity | None = None) -> WorkflowSummaryDTO: ...

    def list(self, owner: str, *, limit: int, cursor: str | None) -> WorkflowPageDTO: ...

    def detail(self, owner: str, workflow: str) -> WorkflowDetailDTO: ...

    def integration_detail(self, identity: IntegrationIdentity, workflow: str, *, scope: str) -> WorkflowDetailDTO: ...

    def events(self, owner: str, workflow: str, *, after: int, limit: int) -> WorkflowEventsDTO: ...

    def acceptance(self, request: WorkflowAcceptanceRequest, *, now: float) -> WorkflowAcceptanceDTO: ...


P = ParamSpec("P")
T = TypeVar("T")


class WorkflowOperatorError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def utc_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def operation_dto(receipt: WorkflowControlReceipt) -> WorkflowOperationDTO:
    return {"action": receipt.action, "revision": receipt.revision, "execution_state": receipt.execution_state,
            "generations": [{"step_key": key, "generation": generation} for key, generation in receipt.generations]}


def _template_inputs(definition: WorkflowDefinition) -> list[WorkflowInputDTO]:
    inputs: list[WorkflowInputDTO] = []
    for named in definition.steps[0].contract.input_refs:
        ref = named.reference
        if type(ref) is ArtifactReference:
            inputs.append({"ref_key": named.ref_key, "kind": "artifact", "artifact_id": ref.artifact_id, "sha256": ref.sha256, "size": ref.size})
        elif type(ref) is CodeRevisionReference:
            inputs.append({"ref_key": named.ref_key, "kind": "code_revision", "repository_id": ref.repository_id,
                "base_commit": ref.base_commit, "head_commit": ref.head_commit, "tree_id": ref.tree_id,
                "source_bundle": {"artifact_id": ref.source_bundle.artifact_id, "sha256": ref.source_bundle.sha256, "size": ref.source_bundle.size}})
        else:
            raise WorkflowValidationError("invalid_template_input")
    return inputs


class WorkflowOperatorService:
    def __init__(self, repository: WorkflowOperatorStore, controls: WorkflowControlService, catalog: WorkflowTemplateCatalog,
                 *, clock: Callable[[], float], identity: Callable[[], str]) -> None:
        self.repository = repository
        self.controls = controls
        self.catalog = catalog
        self.clock = clock
        self.identity = identity

    def templates(self, owner: str) -> WorkflowTemplatesDTO:
        return {"templates": [{"template_id": item.definition.template_id, "revision": item.definition.revision,
                               "workspace_id": item.workspace_id, "policy_revision": item.definition.steps[0].contract.policy_ref.revision,
                               "input_refs": _template_inputs(item.definition),
                               "steps": [{"step_key": step.step_key, "kind": step.kind} for step in item.definition.steps]}
                              for item in self.catalog.available(owner)]}

    async def create(self, request: WorkflowCreateRequest, *, integration: IntegrationIdentity | None = None) -> WorkflowSummaryDTO:
        try:
            if integration is not None:
                integration.authorize_template(request.template_id, request.workspace_id)
                if request.owner_id != integration.owner_id or not request.client_token.startswith(integration.token_namespace):
                    raise WorkflowOperatorError("scope_forbidden")
            definition = self.catalog.instantiate(request)
            if integration is not None and (
                sum(step.contract.budget.max_tokens for step in definition.steps) > integration.max_budget_tokens
                or sum(step.contract.budget.max_model_requests for step in definition.steps) > integration.max_budget_requests
            ):
                raise WorkflowOperatorError("budget_forbidden")
            return await run_owned_io(self.repository.create, request, definition, workflow_id=self.identity(), now=self.clock(),
                                      integration=integration)
        except (WorkflowPersistenceError, WorkflowValidationError) as exc:
            raise WorkflowOperatorError(exc.code) from exc

    async def list(self, owner: str, *, limit: int, cursor: str | None) -> WorkflowPageDTO:
        return await self._read(self.repository.list, owner, limit=limit, cursor=cursor)

    async def detail(self, owner: str, workflow: str) -> WorkflowDetailDTO:
        return await self._read(self.repository.detail, owner, workflow)

    async def integration_detail(self, identity: IntegrationIdentity, workflow: str, *, scope: str) -> WorkflowDetailDTO:
        identity.require(scope)
        return await self._read(self.repository.integration_detail, identity, workflow, scope=scope)

    async def events(self, owner: str, workflow: str, *, after: int, limit: int) -> WorkflowEventsDTO:
        return await self._read(self.repository.events, owner, workflow, after=after, limit=limit)

    @staticmethod
    async def _read(operation: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return await run_owned_io(operation, *args, **kwargs)
        except (WorkflowPersistenceError, WorkflowValidationError) as exc:
            raise WorkflowOperatorError(exc.code) from exc

    async def control(self, request: WorkflowControlRequest) -> WorkflowOperationDTO:
        try:
            return operation_dto(await self.controls.execute(request))
        except WorkflowControlError as exc:
            raise WorkflowOperatorError(exc.code) from exc

    async def acceptance(self, request: WorkflowAcceptanceRequest) -> WorkflowAcceptanceDTO:
        return await self._read(self.repository.acceptance, request, now=self.clock())
