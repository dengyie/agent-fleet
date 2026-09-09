"""Agent-side session capture tooling.

``tools.session.probe`` produces honest capability manifests from bounded,
observed subprocess probes.  This package never calls the Hub and never imports
Flask.
"""
from .probe import (  # noqa: F401
    KNOWN_AGENTS,
    CapabilityManifest,
    as_dict,
    probe_agent,
    probe_all,
    to_json,
)

__all__ = [
    'KNOWN_AGENTS',
    'CapabilityManifest',
    'as_dict',
    'probe_agent',
    'probe_all',
    'to_json',
]