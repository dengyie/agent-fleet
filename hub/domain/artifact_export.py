"""Compatibility import for the shared artifact_export protocol."""
import sys as _sys

from fleet_core import artifact_export as _implementation

_sys.modules[__name__] = _implementation
