"""Compatibility alias for the shared remote fixed-test protocol."""
import sys
from fleet_core import remote_workflow_test as _implementation

sys.modules[__name__] = _implementation
