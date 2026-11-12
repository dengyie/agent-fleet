"""Compatibility alias for the shared capability protocol."""
import sys as _sys
from fleet_core import capability as _implementation

_sys.modules[__name__] = _implementation
