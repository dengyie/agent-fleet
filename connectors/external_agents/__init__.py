"""External Agent adapters with explicit capability declarations."""

from .base import (
    CancelReceipt,
    ExternalAgentCapabilities,
    ExternalAgentError,
    ExternalAgentStatus,
    ExternalTaskRef,
)
from .muse import MuseExternalAgentAdapter

__all__ = [
    "CancelReceipt", "ExternalAgentCapabilities", "ExternalAgentError",
    "ExternalAgentStatus", "ExternalTaskRef", "MuseExternalAgentAdapter",
]
