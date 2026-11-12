"""Shared fixed-test receipt encoding and validation for Hub and Node."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from fleet_core.remote_workflow_test import (
    REMOTE_TEST_RECIPE, RemoteTestError, RemoteTestResult, remote_test_executor_id,
)
from .contract_types import CodeRevisionReference
from .errors import WorkflowValidationError
from .identity import canonical_digest
from .json import decode_snapshot
from .validators import (
    CODING_DELIVERY_TEST_RECIPE, TestRecipe, TestValidation, WorkflowValidatorError,
    revision_payload, test_report_bytes,
)


def encode_test_result(result: TestValidation, revision: CodeRevisionReference, *,
                       recipe: TestRecipe = CODING_DELIVERY_TEST_RECIPE) -> dict[str, Any]:
    try:
        test_report_bytes(result, revision)
        return RemoteTestResult(result.executor_id, result.passed, result.exit_code, result.timed_out, result.report_hash,
            result.raw_report, result.junit, result.environment_snapshot, recipe.argv).payload()
    except WorkflowValidatorError as exc:
        raise RemoteTestError(exc.code) from exc


def decode_test_result(value: Mapping[str, Any], *, revision: CodeRevisionReference,
                       subject_digest: str, node_id: str,
                       recipe: TestRecipe = CODING_DELIVERY_TEST_RECIPE) -> TestValidation:
    wire = RemoteTestResult.parse(value)
    if wire.executor_id != remote_test_executor_id(node_id):
        raise RemoteTestError("executor_identity_mismatch")
    result = TestValidation(canonical_digest(revision_payload(revision)), subject_digest, REMOTE_TEST_RECIPE, 1,
        wire.exit_code, wire.timed_out, wire.passed, wire.raw_report, wire.junit, wire.report_hash,
        wire.executor_id, wire.environment)
    try:
        test_report_bytes(result, revision)
        environment = decode_snapshot(wire.environment)
        # The legacy module path is a persisted wire value from existing Nodes.
        if (environment["backend"] not in {
                "fleet_core.backends.sandbox.SandboxBackend", "tools.platform.backends.sandbox.SandboxBackend"}
                or environment["launcher_sha256"] is None
                or not PurePosixPath(wire.recipe_argv[0]).is_absolute()
                or wire.recipe_argv[1:] != recipe.argv[1:]
                or environment["recipe_argv_sha256"] != canonical_digest(list(wire.recipe_argv))):
            raise RemoteTestError("invalid_remote_test_environment")
        return result
    except (WorkflowValidatorError, WorkflowValidationError) as exc:
        raise RemoteTestError(exc.code) from exc
