"""Compatibility import for shared workflow_validators; implementation lives in fleet_core.workflow.validators."""
import sys as _sys

from fleet_core.workflow import validators as _implementation

_sys.modules[__name__] = _implementation
