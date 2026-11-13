"""Compatibility import for owned asynchronous I/O; implementation lives in fleet_core.async_io."""
import sys as _sys

from fleet_core import async_io as _implementation

_sys.modules[__name__] = _implementation
