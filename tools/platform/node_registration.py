"""Translate operator-owned fixed-test configuration into original Node services."""
from __future__ import annotations

from pathlib import Path

from fleet_core.capability import CapabilityDefinition
from fleet_core.remote_workflow_test import REMOTE_TEST_ACTION
from fleet_core.workflow.contract_types import CatalogRef
from fleet_core.workflow.identity import canonical_json
from .artifact_transfer import InputDirectory
from .node_capabilities import LocalCapability, NodeCapabilityConfig


def workflow_test_settings(value: object) -> dict:
    if (type(value) is not dict or set(value) != {
            "enabled", "offer_revision", "capability", "input_directory", "sandbox_launcher"}
            or value["enabled"] is not True):
        raise ValueError("invalid_workflow_test_registration")
    capability = CapabilityDefinition.from_snapshot(canonical_json(value["capability"]))
    if (capability.capability_id != "coding.test" or capability.revision != 1
            or capability.adapter_id != "fixed-tests" or capability.risk != "workspace_read"
            or capability.retry_class != "manual_only" or "operator" not in capability.allowed_owner_roles
            or capability.max_active_seconds < 3600 or capability.result_limit_bytes != 65536):
        raise ValueError("invalid_fixed_test_capability")
    directory = value["input_directory"]
    if type(directory) is not str or not directory or "\x00" in directory:
        raise ValueError("invalid_workflow_input_directory")
    root = Path(directory).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("workflow_input_directory_required")
    launcher = value["sandbox_launcher"]
    if (type(launcher) is not list or not 1 <= len(launcher) <= 64
            or any(type(arg) is not str or not arg or len(arg) > 4096 or "\x00" in arg for arg in launcher)
            or not Path(launcher[0]).is_absolute()):
        raise ValueError("absolute_workflow_test_launcher_required")
    reference = CatalogRef(capability.capability_id, capability.revision)
    return {
        "workflow_test_enabled": True,
        "sandbox_launcher": tuple(launcher),
        "capabilities": frozenset({"artifact.receive", REMOTE_TEST_ACTION}),
        "artifact_inputs": (("source", InputDirectory(root, frozenset({".tar"}))),),
        "capability_config": NodeCapabilityConfig(value["offer_revision"],
            (LocalCapability(capability, (REMOTE_TEST_ACTION,)),), (reference,), slots=1),
    }
