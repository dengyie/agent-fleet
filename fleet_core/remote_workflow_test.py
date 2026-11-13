"""Bounded remote fixed-test values; no execution, storage, clock or framework."""
from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from fleet_core.artifact_receive import ArtifactReceiveError, ReceiveReceipt, validate_alias
from fleet_core.capability import CapabilityError, digest, integer
from fleet_core.workflow.contract import parse_input_references, reference_values
from fleet_core.workflow.contract_types import CodeRevisionReference, NamedReference
from fleet_core.workflow.errors import WorkflowValidationError
from fleet_core.workflow.identity import canonical_json, validate_reference

REMOTE_TEST_ACTION = "tool.workflow.test"
REMOTE_TEST_MARKER = "workflow_remote_test"
REMOTE_TEST_RECIPE = "coding-delivery-tests"
REMOTE_TEST_REVISION = 1
REPORT_LIMIT = 8 * 1024
JUNIT_LIMIT = 32 * 1024
ENVIRONMENT_LIMIT = 4 * 1024
_REQUEST_FIELDS = frozenset({"schema_version", "recipe_id", "recipe_revision", "workflow_id", "generation",
    "dispatch_token", "input_digest", "binding_digest", "source_revision", "subject_digest", "receive_receipt"})
_RESULT_FIELDS = frozenset({"schema_version", "recipe_id", "recipe_revision", "executor_id", "passed", "exit_code",
    "timed_out", "report_hash", "raw_report_b64", "junit_b64", "environment_b64", "recipe_argv"})


class RemoteTestError(RuntimeError):
    def __init__(self, code: str, *, uncertain: bool = False) -> None:
        self.code, self.uncertain = code, uncertain
        super().__init__(code)


def remote_test_command_id(token: str) -> str:
    try:
        digest(token)
    except CapabilityError as exc:
        raise RemoteTestError("invalid_remote_test_request") from exc
    return "workflow-test-" + token


def remote_test_executor_id(node_id: str) -> str:
    validate_reference(node_id)
    return "remote-test-" + hashlib.sha256(node_id.encode("utf-8")).hexdigest()


def validate_remote_test_config(config: object, *, node_id: str | None = None,
                                binding_digest: str | None = None) -> None:
    fields = {"owner_id", "workspace_id", "execution_node_id", "workflow_dispatch", "workflow_test",
        REMOTE_TEST_MARKER, "workflow_capability"}
    if type(config) is not dict or set(config) != fields:
        raise RemoteTestError("invalid_remote_test_configuration")
    try:
        for key in ("owner_id", "workspace_id", "execution_node_id"):
            validate_reference(config[key])
        if config["execution_node_id"] in {"local", "hub", "host"} or (
                node_id is not None and config["execution_node_id"] != node_id):
            raise RemoteTestError("invalid_remote_test_configuration")
        if (type(config[REMOTE_TEST_MARKER]) is not dict or config[REMOTE_TEST_MARKER] != {"revision": 1}
                or type(config[REMOTE_TEST_MARKER].get("revision")) is not int
                or type(config["workflow_test"]) is not dict or config["workflow_test"] != {"recipe_id": REMOTE_TEST_RECIPE, "revision": 1}
                or type(config["workflow_test"].get("revision")) is not int):
            raise RemoteTestError("invalid_remote_test_configuration")
        marker, capability = config["workflow_dispatch"], config["workflow_capability"]
        if type(marker) is not dict or set(marker) != {"token", "payload_hash", "config_digest"} or (
                type(capability) is not dict or set(capability) != {"binding_digest"}):
            raise RemoteTestError("invalid_remote_test_configuration")
        for value in (*marker.values(), capability["binding_digest"]):
            digest(value)
        if binding_digest is not None and capability["binding_digest"] != binding_digest:
            raise RemoteTestError("invalid_remote_test_configuration")
    except (WorkflowValidationError, CapabilityError, TypeError) as exc:
        raise RemoteTestError("invalid_remote_test_configuration") from exc


@dataclass(frozen=True, slots=True)
class RemoteTestRequest:
    workflow_id: str
    generation: int
    dispatch_token: str
    input_digest: str
    binding_digest: str
    source_revision: CodeRevisionReference
    subject_digest: str
    receive_receipt: ReceiveReceipt

    def __post_init__(self) -> None:
        try:
            validate_reference(self.workflow_id)
            integer(self.generation, minimum=1)
            for value in (self.dispatch_token, self.input_digest, self.binding_digest, self.subject_digest):
                digest(value)
            if type(self.source_revision) is not CodeRevisionReference:
                raise RemoteTestError("invalid_remote_test_request")
            reference_values((NamedReference("source", self.source_revision),))
            receipt, artifact = self.receive_receipt, self.source_revision.source_bundle
            if type(receipt) is not dict or set(receipt) != {"grant_id", "artifact_id", "sha256", "size", "input_alias"}:
                raise RemoteTestError("invalid_remote_test_receipt")
            digest(receipt["grant_id"])
            validate_alias(receipt["input_alias"])
            if type(receipt["size"]) is not int or (receipt["artifact_id"], receipt["sha256"], receipt["size"]) != (
                    artifact.artifact_id, artifact.sha256, artifact.size):
                raise RemoteTestError("invalid_remote_test_receipt")
            object.__setattr__(self, "receive_receipt", dict(receipt))
        except (WorkflowValidationError, CapabilityError, ArtifactReceiveError) as exc:
            raise RemoteTestError("invalid_remote_test_request") from exc

    def payload(self) -> dict[str, Any]:
        return {"schema_version": 1, "recipe_id": REMOTE_TEST_RECIPE, "recipe_revision": REMOTE_TEST_REVISION,
            "workflow_id": self.workflow_id, "generation": self.generation, "dispatch_token": self.dispatch_token,
            "input_digest": self.input_digest, "binding_digest": self.binding_digest,
            "source_revision": reference_values((NamedReference("source", self.source_revision),))[0],
            "subject_digest": self.subject_digest, "receive_receipt": dict(self.receive_receipt)}

    @classmethod
    def parse(cls, value: object) -> RemoteTestRequest:
        if type(value) is not dict or set(value) != _REQUEST_FIELDS or type(value["schema_version"]) is not int or (
                value["schema_version"], value["recipe_id"], value["recipe_revision"]) != (1, REMOTE_TEST_RECIPE, REMOTE_TEST_REVISION
                ) or type(value["recipe_revision"]) is not int:
            raise RemoteTestError("invalid_remote_test_request")
        try:
            references = parse_input_references([value["source_revision"]])
            if references[0].ref_key != "source":
                raise RemoteTestError("invalid_remote_test_request")
            return cls(**{key: value[key] for key in _REQUEST_FIELDS - {"schema_version", "recipe_id", "recipe_revision", "source_revision"}},
                source_revision=references[0].reference)
        except WorkflowValidationError as exc:
            raise RemoteTestError("invalid_remote_test_request") from exc


def _decode_blob(value: object, limit: int) -> bytes:
    if type(value) is not str or len(value) > 4 * ((limit + 2) // 3):
        raise RemoteTestError("remote_test_result_too_large")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RemoteTestError("invalid_remote_test_result") from exc
    if not raw or len(raw) > limit or base64.b64encode(raw).decode("ascii") != value:
        raise RemoteTestError("invalid_remote_test_result")
    return raw


@dataclass(frozen=True, slots=True)
class RemoteTestResult:
    executor_id: str
    passed: bool
    exit_code: int | None
    timed_out: bool
    report_hash: str
    raw_report: bytes
    junit: bytes
    environment: bytes
    recipe_argv: tuple[str, ...]

    def __post_init__(self) -> None:
        try:
            validate_reference(self.executor_id)
            digest(self.report_hash)
            if (type(self.recipe_argv) is not tuple or not 1 <= len(self.recipe_argv) <= 32
                    or any(type(arg) is not str or not arg or len(arg) > 4096 or '\x00' in arg for arg in self.recipe_argv)):
                raise RemoteTestError("invalid_remote_test_result")
            if type(self.passed) is not bool or type(self.timed_out) is not bool or (
                    self.exit_code is not None and (type(self.exit_code) is not int or not -(1 << 31) <= self.exit_code < (1 << 31))):
                raise RemoteTestError("invalid_remote_test_result")
            for raw, limit in ((self.raw_report, REPORT_LIMIT), (self.junit, JUNIT_LIMIT), (self.environment, ENVIRONMENT_LIMIT)):
                if type(raw) is not bytes or not 1 <= len(raw) <= limit:
                    raise RemoteTestError("remote_test_result_too_large")
            if hashlib.sha256(self.raw_report).hexdigest() != self.report_hash:
                raise RemoteTestError("invalid_remote_test_result")
        except (WorkflowValidationError, CapabilityError) as exc:
            raise RemoteTestError("invalid_remote_test_result") from exc

    def payload(self) -> dict[str, Any]:
        result = {"schema_version": 1, "recipe_id": REMOTE_TEST_RECIPE, "recipe_revision": REMOTE_TEST_REVISION,
            "executor_id": self.executor_id, "passed": self.passed, "exit_code": self.exit_code, "timed_out": self.timed_out,
            "report_hash": self.report_hash, "raw_report_b64": base64.b64encode(self.raw_report).decode("ascii"),
            "junit_b64": base64.b64encode(self.junit).decode("ascii"), "environment_b64": base64.b64encode(self.environment).decode("ascii"),
            "recipe_argv": list(self.recipe_argv)}
        try:
            # Reserve the complete NodeToolExecutor envelope, including the longest command id.
            canonical_json({"command_id": "x" * 128, "state": "succeeded", "result": result})
        except WorkflowValidationError as exc:
            raise RemoteTestError("remote_test_result_too_large") from exc
        return result

    @classmethod
    def parse(cls, value: Mapping[str, Any]) -> RemoteTestResult:
        if type(value) is not dict or set(value) != _RESULT_FIELDS or type(value["schema_version"]) is not int or (
                value["schema_version"], value["recipe_id"], value["recipe_revision"]) != (1, REMOTE_TEST_RECIPE, REMOTE_TEST_REVISION
                ) or type(value["recipe_revision"]) is not int:
            raise RemoteTestError("invalid_remote_test_result")
        if type(value["recipe_argv"]) is not list or not 1 <= len(value["recipe_argv"]) <= 32:
            raise RemoteTestError("invalid_remote_test_result")
        result = cls(value["executor_id"], value["passed"], value["exit_code"], value["timed_out"], value["report_hash"],
            _decode_blob(value["raw_report_b64"], REPORT_LIMIT), _decode_blob(value["junit_b64"], JUNIT_LIMIT),
            _decode_blob(value["environment_b64"], ENVIRONMENT_LIMIT), tuple(value["recipe_argv"]))
        result.payload()
        return result
