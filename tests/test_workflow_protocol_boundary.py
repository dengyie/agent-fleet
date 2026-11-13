"""Hub and Node share one Workflow protocol without depending on each other."""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("legacy,neutral", [
    ("hub.domain.artifact_transfer", "fleet_core.artifact_transfer"),
    ("hub.domain.artifact_receive", "fleet_core.artifact_receive"),
    ("hub.domain.artifact_export", "fleet_core.artifact_export"),
    ("hub.domain.capability", "fleet_core.capability"),
    ("hub.domain.capability_execution", "fleet_core.capability_execution"),
    ("hub.domain.remote_workflow_test", "fleet_core.remote_workflow_test"),
    ("hub.domain.workflow_contract", "fleet_core.workflow.contract"),
    ("hub.domain.workflow_contract_types", "fleet_core.workflow.contract_types"),
    ("hub.domain.workflow_errors", "fleet_core.workflow.errors"),
    ("hub.domain.workflow_identity", "fleet_core.workflow.identity"),
    ("hub.domain.workflow_json", "fleet_core.workflow.json"),
    ("hub.domain.workflow_review", "fleet_core.workflow.review_protocol"),
    ("hub.platform.workflow_source", "fleet_core.workflow.source"),
    ("hub.platform.workflow_validators", "fleet_core.workflow.validators"),
    ("hub.platform.workflow_review", "fleet_core.workflow.review"),
    ("tools.platform.workflow_source", "fleet_core.workflow.source"),
    ("tools.platform.workflow_validators", "fleet_core.workflow.validators"),
    ("tools.platform.workflow_review", "fleet_core.workflow.review"),
    ("hub.platform.async_io", "fleet_core.async_io"),
    ("tools.async_io", "fleet_core.async_io"),
    ("tools.atomic_directory", "fleet_core.atomic_directory"),
])
def test_compatibility_paths_preserve_module_and_type_identity(legacy: str, neutral: str) -> None:
    assert importlib.import_module(legacy) is importlib.import_module(neutral)


def test_artifact_and_delivery_errors_keep_existing_identity() -> None:
    from fleet_core.artifact_contract import ARTIFACT_MAX_BYTES, ArtifactError
    from fleet_core.workflow.errors import WorkflowValidationError
    from fleet_core.workflow.identity import digest_value
    from hub.domain.workflow_delivery import digest_value as legacy_digest
    from hub.domain.workflow_errors import WorkflowValidationError as LegacyValidationError
    from hub.platform.artifacts import ArtifactError as LegacyArtifactError, ArtifactStore

    assert ArtifactError is LegacyArtifactError
    assert WorkflowValidationError is LegacyValidationError
    assert legacy_digest is digest_value
    assert ArtifactStore.MAX_BYTES == ARTIFACT_MAX_BYTES == 10 * 1024 * 1024
    with pytest.raises(LegacyValidationError, match="invalid_delivery_digest"):
        digest_value("bad")


def test_capability_dispatch_intent_preserves_workflow_record_identity() -> None:
    from fleet_core.workflow.dispatch import DispatchIntent
    from hub.domain.workflow_records import DispatchIntent as LegacyDispatchIntent

    assert DispatchIntent is LegacyDispatchIntent


def test_node_workflow_modules_import_with_hub_unavailable() -> None:
    root = Path(__file__).resolve().parents[1]
    probe = """
import importlib, sys
sys.modules['hub'] = None
for name in ('artifact_transfer', 'artifact_receive', 'artifact_export', 'capability', 'capability_execution', 'remote_workflow_test'):
    importlib.import_module('fleet_core.' + name)
for name in ('source', 'validators', 'review'):
    core = importlib.import_module('fleet_core.workflow.' + name)
    facade = importlib.import_module('tools.platform.workflow_' + name)
    assert core is facade
import tools.async_io, tools.atomic_directory
import tools.platform.node_capabilities, tools.platform.workflow_remote_test
import fleet_core.workflow.remote_test_codec
assert sorted(name for name in sys.modules if name == 'hub' or name.startswith('hub.')) == ['hub']
print('ok')
"""
    result = subprocess.run([sys.executable, "-c", probe], cwd=root, capture_output=True,
                            text=True, timeout=30,
                            env={**os.environ, "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
