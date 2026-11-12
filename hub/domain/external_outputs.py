"""Compatibility alias for the shared external Agent protocol."""
import sys
from fleet_core.workflow import external_outputs as _implementation
sys.modules[__name__] = _implementation
