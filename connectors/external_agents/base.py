"""Compatibility imports for the framework-free external Agent I/O contract."""
from fleet_core.workflow.external_agent import (
    CancelReceipt,
    ExternalAgentAdapter,
    ExternalAgentCapabilities,
    ExternalAgentError,
    ExternalAgentStatus,
    ExternalTaskRef,
    _id as _id,  # Compatibility export used by the Muse adapter.
)

__all__ = [
    "CancelReceipt", "ExternalAgentCapabilities", "ExternalAgentError",
    "ExternalAgentStatus", "ExternalTaskRef", "ExternalAgentAdapter",
]
