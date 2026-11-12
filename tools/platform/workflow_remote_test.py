"""Fixed Node test execution and complete report validation at the Hub boundary."""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from fleet_core.artifact_receive import ArtifactReceiveError
from fleet_core.capability import CapabilityError
from fleet_core.capability_execution import CAPABILITY_ARGUMENT, CapabilityExecutionContext
from fleet_core.platform_command import verify_command
from fleet_core.remote_workflow_test import (
    REMOTE_TEST_ACTION, REMOTE_TEST_RECIPE, RemoteTestError, RemoteTestRequest,
    remote_test_command_id, remote_test_executor_id,
)
from fleet_core.workflow.contract_types import CodeRevisionReference
from fleet_core.workflow.errors import WorkflowValidationError
from fleet_core.workflow import remote_test_codec as _codec
from tools.async_io import run_owned_io
from .artifact_transfer import ArtifactReceiver
from fleet_core.backends.sandbox import SandboxBackend
from .workflow_source import SourceExportError, extract_source_stream
from .workflow_validators import (
    CODING_DELIVERY_TEST_RECIPE, FixedTestRunner, TestValidation, WorkflowValidatorError,
)


def encode_test_result(result: TestValidation, revision: CodeRevisionReference) -> dict[str, Any]:
    return _codec.encode_test_result(result, revision, recipe=CODING_DELIVERY_TEST_RECIPE)


def decode_test_result(value: Mapping[str, Any], *, revision: CodeRevisionReference,
                       subject_digest: str, node_id: str) -> TestValidation:
    return _codec.decode_test_result(value, revision=revision, subject_digest=subject_digest,
        node_id=node_id, recipe=CODING_DELIVERY_TEST_RECIPE)


class NodeRemoteTestExecutor:
    """The administrator supplies the launcher; the command supplies no executable."""

    def __init__(self, receiver: ArtifactReceiver, *, launcher: tuple[str, ...],
                 clock: Callable[[], float], should_cancel: Callable[[], bool]) -> None:
        self.receiver, self.launcher = receiver, tuple(launcher)
        self.clock, self.should_cancel = clock, should_cancel
        self.runner = FixedTestRunner(backend_factory=lambda root: SandboxBackend(root, launcher=self.launcher))

    @property
    def available(self) -> bool:
        return bool(os.name == "posix" and sys.platform.startswith("linux") and self.receiver.available
            and self.launcher and shutil.which(self.launcher[0]))

    def execute(self, command: Mapping[str, Any], *, should_cancel=None) -> dict[str, Any]:
        if not self.available:
            raise RemoteTestError("capability_unavailable")
        try:
            if not verify_command(command, self.receiver.public_key):
                raise RemoteTestError("invalid_signature")
            context = CapabilityExecutionContext.from_command(command)
            arguments = dict(command["arguments"])
            arguments.pop(CAPABILITY_ARGUMENT)
            request = RemoteTestRequest.parse(arguments)
            if (command.get("action"), command.get("retry_class"), command.get("command_id"), command.get("run_id"),
                    command.get("target_node"), command.get("grant_id"), context.capability_id, context.binding.node_id,
                    context.binding.input_digest, hashlib.sha256(context.binding.snapshot).hexdigest()) != (
                    REMOTE_TEST_ACTION, "manual_only", remote_test_command_id(request.dispatch_token), "wf-run-" + request.dispatch_token,
                    self.receiver.node_id, None, "coding.test", self.receiver.node_id, request.input_digest, request.binding_digest):
                raise RemoteTestError("invalid_remote_test_command")
            expiry = command.get("expires_at")
            if type(expiry) not in (int, float) or expiry <= self.clock():
                raise RemoteTestError("remote_test_expired")
            def cancelled() -> bool:
                return self.should_cancel() or (should_cancel is not None and should_cancel()) or self.clock() >= expiry
            return asyncio.run(self._run(request, command["resource_id"], cancelled))
        except (CapabilityError, ArtifactReceiveError, WorkflowValidationError, SourceExportError, WorkflowValidatorError) as exc:
            uncertain = getattr(exc, "uncertain", False) or getattr(exc, "code", "") in {
                "test_execution_failed", "test_cancelled", "test_stop_unconfirmed",
            }
            raise RemoteTestError(exc.code, uncertain=uncertain) from exc

    async def _run(self, request: RemoteTestRequest, workspace: str, cancelled: Callable[[], bool]) -> dict[str, Any]:
        if self.runner.recipes.get(REMOTE_TEST_RECIPE) != CODING_DELIVERY_TEST_RECIPE:
            raise RemoteTestError("invalid_test_recipe")
        if cancelled():
            raise RemoteTestError("remote_test_cancelled")
        with tempfile.TemporaryDirectory(prefix="fleet-remote-fixed-test-") as temporary:
            checkout = Path(temporary) / "source"
            def materialize() -> None:
                with self.receiver.open_received(request.receive_receipt, workspace_id=workspace,
                        workflow_id=request.workflow_id, generation=request.generation, input_digest=request.input_digest) as source:
                    extract_source_stream(source, checkout, expected=request.source_revision, should_cancel=cancelled)
            await run_owned_io(materialize)
            if cancelled():
                raise RemoteTestError("remote_test_cancelled")
            result = await self.runner.run(request.source_revision, request.subject_digest, checkout,
                recipe_id=REMOTE_TEST_RECIPE, executor_id=remote_test_executor_id(self.receiver.node_id), should_cancel=cancelled)
            if cancelled():
                raise RemoteTestError("remote_test_cancelled", uncertain=True)
            return encode_test_result(result, request.source_revision)
