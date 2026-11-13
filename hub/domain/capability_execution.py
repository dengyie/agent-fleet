"""Compatibility alias for the shared capability_execution protocol."""
import sys as _sys
from fleet_core import capability_execution as _implementation

_sys.modules[__name__] = _implementation
