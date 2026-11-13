"""Compatibility import for the shared artifact_transfer protocol."""
import sys as _sys

from fleet_core import artifact_transfer as _implementation

_sys.modules[__name__] = _implementation
