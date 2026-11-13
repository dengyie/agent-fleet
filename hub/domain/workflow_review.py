"""Compatibility import for the shared Workflow protocol; implementation lives in fleet_core.workflow.review_protocol."""
import sys as _sys

from fleet_core.workflow import review_protocol as _implementation

_sys.modules[__name__] = _implementation
