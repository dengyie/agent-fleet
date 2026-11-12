"""Compatibility import for the shared Workflow protocol; implementation lives in fleet_core.workflow.identity."""
import sys as _sys

from fleet_core.workflow import identity as _implementation

_sys.modules[__name__] = _implementation
