"""Compatibility import for shared workflow_source; implementation lives in fleet_core.workflow.source."""
import sys as _sys

from fleet_core.workflow import source as _implementation

_sys.modules[__name__] = _implementation
