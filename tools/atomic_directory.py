"""Compatibility import for exclusive directory publication; implementation lives in fleet_core.atomic_directory."""
import sys as _sys

from fleet_core import atomic_directory as _implementation

_sys.modules[__name__] = _implementation
