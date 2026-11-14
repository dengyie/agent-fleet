"""Read-only configuration checks; intentionally not a model/network health claim."""
import os
from pathlib import Path

from hub.platform.providers.openai_compatible import ProviderFactory, ProviderUnavailable


def check_platform_readiness(fleet, owner_id):
    config = fleet['config']
    repository = fleet['platform_repository']
    services = fleet['services']
    issues = []
    if services.get('platform_worker') is None: issues.append('worker_disabled')
    if services.get('platform_run_scheduler') is None and not config.dev_operator:
        issues.append('scheduler_disabled')
    elif not config.platform_worker_scheduler_enabled and not config.platform_worker_enabled:
        issues.append('scheduler_disabled')
    if services.get('service_health') is None: issues.append('service_monitoring_disabled')
    defaults = repository.get_defaults(owner_id)
    model = repository.get_model(owner_id, defaults['model_profile_id']) if defaults.get('model_profile_id') else None
    workspace = repository.get_workspace(owner_id, defaults['workspace_id']) if defaults.get('workspace_id') else None
    if not model or not model['enabled']:
        issues.append('default_model_unavailable')
    else:
        if model['provider'] != 'deterministic' and not config.platform_provider_network_enabled:
            issues.append('provider_network_disabled')
        try:
            ProviderFactory(secret_broker=services.get("model_secrets"), allow_network=False)(model)  # builds only; never completes a request
        except ProviderUnavailable as exc:
            issues.append(exc.diagnostic()['provider_error'])
    if not workspace or not workspace['enabled']:
        issues.append('default_workspace_unavailable')
    elif workspace['backend'] != 'directory':
        issues.append('workspace_backend_unavailable')
    else:
        node_id = workspace.get('default_node_id') or defaults.get('execution_node_id')
        if node_id not in (None, '', 'local', 'hub', 'host'):
            node = repository.get_node(owner_id, node_id)
            if not node or not node['enabled']:
                issues.append('default_node_unavailable')
            if not config.platform_remote_execution_enabled:
                issues.append('remote_execution_disabled')
            delivery = services.get('platform_delivery')
            if delivery is None or not delivery.signing_key:
                issues.append('command_signature_required')
        else:
            root = Path(workspace['root_path']).expanduser()
            if not root.is_dir() or not os.access(root, os.R_OK | os.W_OK | os.X_OK):
                issues.append('workspace_directory_unavailable')
    return {'ok': not issues, 'configuration_ready': not issues, 'issues': issues,
            'provider_connectivity': 'not_tested', 'node_connectivity': 'not_tested'}
