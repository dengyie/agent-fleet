"""Compatibility import for the shared artifact_receive protocol."""
import sys as _sys

from fleet_core import artifact_receive as _implementation

_sys.modules[__name__] = _implementation
