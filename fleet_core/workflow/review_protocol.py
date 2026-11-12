"""Trusted review identity and immutable command-adapter registration."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contract_types import CatalogRef
from .identity import digest_value
from .errors import WorkflowValidationError
from .identity import validate_reference


@dataclass(frozen=True, slots=True)
class ReviewIdentity:
    executor_id: str
    kind: str
    model_id: str | None = None

    def __post_init__(self) -> None:
        validate_reference(self.executor_id)
        if self.kind not in ("model", "service", "human"):
            raise WorkflowValidationError("invalid_executor_identity")
        if self.kind == "model":
            validate_reference(self.model_id)
        elif self.model_id is not None:
            raise WorkflowValidationError("invalid_executor_identity")

    def payload(self) -> dict[str, str | None]:
        return {"id": self.executor_id, "kind": self.kind, "model_id": self.model_id}


@dataclass(frozen=True, slots=True)
class ReviewAdapterSpec:
    adapter_ref: CatalogRef
    executor: ReviewIdentity
    configuration_digest: str

    def __post_init__(self) -> None:
        ref = self.adapter_ref
        if type(ref) is not CatalogRef or type(ref.revision) is not int or not 1 <= ref.revision <= 9007199254740991:
            raise WorkflowValidationError("invalid_review_adapter")
        validate_reference(ref.id)
        if type(self.executor) is not ReviewIdentity:
            raise WorkflowValidationError("invalid_executor_identity")
        digest_value(self.configuration_digest)

    def payload(self) -> dict[str, Any]:
        return {"adapter_id": self.adapter_ref.id, "revision": self.adapter_ref.revision,
                "executor": self.executor.payload(), "configuration_digest": self.configuration_digest}

    @classmethod
    def parse(cls, value: Any) -> ReviewAdapterSpec:
        if type(value) is not dict or set(value) != {"adapter_id", "revision", "executor", "configuration_digest"}:
            raise WorkflowValidationError("invalid_review_adapter")
        executor = value["executor"]
        if type(executor) is not dict or set(executor) != {"id", "kind", "model_id"}:
            raise WorkflowValidationError("invalid_executor_identity")
        return cls(CatalogRef(value["adapter_id"], value["revision"]),
                   ReviewIdentity(executor["id"], executor["kind"], executor["model_id"]), value["configuration_digest"])
