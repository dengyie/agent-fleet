"""Translate explicit workspace enrollment into the existing Node runtime."""
from pathlib import Path

from fleet_core.node_action_contract import POSTCHECK_ACTION
from fleet_core.workspace_execution import parse_workspace_execution


def workspace_settings(value):
    config = parse_workspace_execution(value)
    roots = {key: Path(path).expanduser() for key, path in config['roots'].items()}
    if any(not root.is_absolute() for root in roots.values()):
        raise ValueError('workspace paths must be absolute on this Node')
    return {'execution_mode': config['execution_mode'], 'workspace_roots': roots,
            'sandbox_launcher': tuple(config['sandbox_launcher']),
            'capabilities': frozenset({'workspace.list', 'workspace.read', 'workspace.write',
                                       'workspace.exec', POSTCHECK_ACTION})}
