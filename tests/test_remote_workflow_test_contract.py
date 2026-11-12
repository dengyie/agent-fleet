"""Strict wire caps and complete report/subject integrity without process I/O."""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace

import pytest

from hub.domain.remote_workflow_test import (RemoteTestError, RemoteTestRequest, RemoteTestResult, remote_test_executor_id)
from hub.domain.workflow_contract_types import ArtifactReference, CodeRevisionReference
from hub.domain.workflow_identity import canonical_digest, canonical_json
from tools.platform.workflow_remote_test import decode_test_result, encode_test_result
from tools.platform.workflow_validators import TestValidation as Validation, revision_payload
from tools.platform.workflow_validators import CODING_DELIVERY_TEST_RECIPE


def revision() -> CodeRevisionReference:
    return CodeRevisionReference("repo", "1" * 40, "2" * 40, "3" * 40, ArtifactReference("bundle", "a" * 64, 10240))


def result(*, passed: bool = True, source: CodeRevisionReference | None = None, subject: str = "d" * 64,
           node: str = "node-a") -> Validation:
    source = source or revision()
    junit = b'<testsuite tests="1"><testcase name="ok"/></testsuite>' if passed else (
        b'<testsuite tests="1" failures="1"><testcase name="bad"><failure/></testcase></testsuite>')
    environment = canonical_json({"scope": "adapter_host", "os": "Linux", "architecture": "x86_64", "python_version": "3.13",
        "backend": "tools.platform.backends.sandbox.SandboxBackend",
        "recipe_argv_sha256": canonical_digest(list(CODING_DELIVERY_TEST_RECIPE.argv)), "launcher_sha256": "c" * 64})
    executor = remote_test_executor_id(node)
    raw = canonical_json({"schema_version": 1, "source_revision": revision_payload(source), "subject_digest": subject,
        "recipe": {"id": "coding-delivery-tests", "revision": 1}, "executor_id": executor,
        "exit_code": 0 if passed else 1, "timed_out": False, "passed": passed, "junit_sha256": hashlib.sha256(junit).hexdigest(),
        "error_code": None if passed else "process_failed", "output_truncated": False, "environment": json.loads(environment)})
    return Validation(canonical_digest(revision_payload(source)), subject, "coding-delivery-tests", 1, 0 if passed else 1,
        False, passed, raw, junit, hashlib.sha256(raw).hexdigest(), executor, environment)


def request() -> RemoteTestRequest:
    return RemoteTestRequest("workflow-a", 1, "e" * 64, "f" * 64, "b" * 64, revision(), "d" * 64,
        {"grant_id": "c" * 64, "artifact_id": "bundle", "sha256": "a" * 64, "size": 10240, "input_alias": "source"})


@pytest.mark.parametrize("passed", [True, False])
def test_complete_result_round_trip_preserves_exact_bytes(passed: bool) -> None:
    original = result(passed=passed)
    wire = encode_test_result(original, revision())
    assert decode_test_result(wire, revision=revision(), subject_digest="d" * 64, node_id="node-a") == original
    assert len(canonical_json({"command_id": "x" * 128, "state": "succeeded", "result": wire})) < 65536
    assert RemoteTestRequest.parse(request().payload()) == request()


@pytest.mark.parametrize("change", [{"schema_version": True}, {"recipe_revision": True}, {"recipe_id": "shell"},
    {"argv": ["sh"]}, {"generation": True}, {"dispatch_token": "x"}, {"binding_digest": []}, {"source_revision": {}},
    {"receive_receipt": {}}, {"subject_digest": "invalid"}])
def test_request_rejects_extra_fields_and_bad_identity(change: dict) -> None:
    with pytest.raises(RemoteTestError):
        RemoteTestRequest.parse(request().payload() | change)


@pytest.mark.parametrize("field,limit", [("raw_report_b64", 8192), ("junit_b64", 32768), ("environment_b64", 4096)])
def test_decoded_limits_fail_closed(field: str, limit: int) -> None:
    wire = encode_test_result(result(), revision())
    wire[field] = base64.b64encode(b"x" * (limit + 1)).decode()
    with pytest.raises(RemoteTestError):
        RemoteTestResult.parse(wire)


@pytest.mark.parametrize("change", [{"schema_version": True}, {"recipe_revision": True}, {"recipe_id": "other"},
    {"passed": 1}, {"exit_code": True}, {"timed_out": 0}, {"report_hash": "0" * 64}, {"raw_report_b64": "%%%"},
    {"junit_b64": ""}, {"environment_b64": None}, {"path": "/tmp/report"},
    {"recipe_argv": None}, {"recipe_argv": []}, {"recipe_argv": ["x"] * 33},
    {"recipe_argv": [True]}, {"recipe_argv": [""]}, {"recipe_argv": ["x" * 4097]},
    {"recipe_argv": ["python\x00"]}])
def test_result_rejects_malformed_wire(change: dict) -> None:
    with pytest.raises(RemoteTestError):
        RemoteTestResult.parse(encode_test_result(result(), revision()) | change)


@pytest.mark.parametrize("change", ["revision", "subject", "executor", "junit", "environment", "xml"])
def test_hub_rechecks_every_report_binding(change: str) -> None:
    wire = encode_test_result(result(), revision())
    expected_revision, subject, node = revision(), "d" * 64, "node-a"
    if change == "revision":
        expected_revision = replace(expected_revision, head_commit="4" * 40)
    elif change == "subject":
        subject = "e" * 64
    elif change == "executor":
        node = "node-b"
    elif change == "junit":
        wire["junit_b64"] = base64.b64encode(b'<testsuite><testcase name="other"/></testsuite>').decode()
    elif change == "environment":
        wire["environment_b64"] = base64.b64encode(b"{}").decode()
    else:
        junit = b'<!DOCTYPE x [<!ENTITY a "a">]><testsuite><testcase/></testsuite>'
        raw = json.loads(result().raw_report)
        raw["junit_sha256"] = hashlib.sha256(junit).hexdigest()
        encoded = canonical_json(raw)
        wire.update(raw_report_b64=base64.b64encode(encoded).decode(), report_hash=hashlib.sha256(encoded).hexdigest(),
            junit_b64=base64.b64encode(junit).decode())
    with pytest.raises(RemoteTestError):
        decode_test_result(wire, revision=expected_revision, subject_digest=subject, node_id=node)


@pytest.mark.parametrize("change", [{"backend": "tools.platform.backends.directory.DirectoryBackend"},
    {"launcher_sha256": None}, {"recipe_argv_sha256": "0" * 64}])
def test_remote_report_requires_fixed_recipe_and_explicit_sandbox(change: dict) -> None:
    original = result()
    environment = canonical_json(json.loads(original.environment_snapshot) | change)
    raw = canonical_json(json.loads(original.raw_report) | {"environment": json.loads(environment)})
    wire = encode_test_result(replace(original, environment_snapshot=environment, raw_report=raw,
        report_hash=hashlib.sha256(raw).hexdigest()), revision())
    with pytest.raises(RemoteTestError, match="invalid_remote_test_environment"):
        decode_test_result(wire, revision=revision(), subject_digest="d" * 64, node_id="node-a")


def test_hub_accepts_fixed_recipe_from_a_different_node_python_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools.platform import workflow_remote_test

    original = result()
    node_recipe = replace(CODING_DELIVERY_TEST_RECIPE, argv=(
        "/opt/fleet-node/venv/bin/python", *CODING_DELIVERY_TEST_RECIPE.argv[1:]))
    environment = canonical_json(json.loads(original.environment_snapshot) | {
        "recipe_argv_sha256": canonical_digest(list(node_recipe.argv))})
    raw = canonical_json(json.loads(original.raw_report) | {"environment": json.loads(environment)})
    remote = replace(original, environment_snapshot=environment, raw_report=raw, report_hash=hashlib.sha256(raw).hexdigest())
    with monkeypatch.context() as node:
        node.setattr(workflow_remote_test, "CODING_DELIVERY_TEST_RECIPE", node_recipe)
        wire = encode_test_result(remote, revision())
    assert decode_test_result(wire, revision=revision(), subject_digest="d" * 64, node_id="node-a") == remote


@pytest.mark.parametrize("argv", [("python", *CODING_DELIVERY_TEST_RECIPE.argv[1:]),
    ("/usr/bin/python", "-m", "pytest", "--override-ini=addopts=-x"),
    ("/usr/bin/python", *CODING_DELIVERY_TEST_RECIPE.argv[1:], "extra.py")])
def test_hub_rejects_changed_fixed_arguments_even_with_matching_digest(argv: tuple[str, ...]) -> None:
    original = result()
    environment = canonical_json(json.loads(original.environment_snapshot) | {"recipe_argv_sha256": canonical_digest(list(argv))})
    raw = canonical_json(json.loads(original.raw_report) | {"environment": json.loads(environment)})
    wire = encode_test_result(replace(original, environment_snapshot=environment, raw_report=raw,
        report_hash=hashlib.sha256(raw).hexdigest()), revision()) | {"recipe_argv": list(argv)}
    with pytest.raises(RemoteTestError, match="invalid_remote_test_environment"):
        decode_test_result(wire, revision=revision(), subject_digest="d" * 64, node_id="node-a")
