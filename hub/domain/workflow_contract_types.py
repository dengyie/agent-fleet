"""Compatibility import for the shared Workflow protocol; implementation lives in fleet_core.workflow.contract_types."""
import sys as _sys

from fleet_core.workflow import contract_types as _implementation

_sys.modules[__name__] = _implementation
