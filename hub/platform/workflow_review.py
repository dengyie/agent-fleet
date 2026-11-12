"""Compatibility import for shared workflow_review; implementation lives in fleet_core.workflow.review."""
import sys as _sys

from fleet_core.workflow import review as _implementation

_sys.modules[__name__] = _implementation
