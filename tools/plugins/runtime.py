"""Registered node handlers for bundled official plugins."""
from pathlib import Path

from fleet_core.plugins import OFFICIAL_PLUGINS, PluginError
from .github_cli import GitHubCliPlugin

NODE_PLUGIN_FACTORIES = {'github-cli': GitHubCliPlugin}


class PluginExecutor:
    def __init__(self, state_directory, configurations, *, registry=OFFICIAL_PLUGINS, factories=None):
        if not isinstance(configurations, dict):
            raise PluginError('invalid_plugin_config')
        self.registry = registry
        self.handlers = {}
        factories = NODE_PLUGIN_FACTORIES if factories is None else factories
        try:
            for plugin_id, configuration in configurations.items():
                registry.get(plugin_id)
                if plugin_id not in factories:
                    raise PluginError('plugin_not_installed')
                self.handlers[plugin_id] = factories[plugin_id](Path(state_directory) / plugin_id, configuration)
        except Exception:
            self.close()
            raise

    def manifest(self):
        return [handler.manifest() for handler in self.handlers.values()]

    def execute(self, command):
        command_id = command.get('command_id')
        try:
            parts = command.get('action', '').split('.')
            if len(parts) != 3 or parts[0] != 'plugin':
                raise PluginError('unknown_plugin_action')
            _, plugin_id, action = parts
            definition = self.registry.get(plugin_id)
            arguments = definition.validate(action, command.get('arguments'))
            if command.get('resource_id') != arguments['context_id']:
                raise PluginError('context_unavailable')
            handler = self.handlers.get(plugin_id)
            if handler is None:
                raise PluginError('plugin_not_installed')
            result = handler.execute(action, arguments, command_id=command_id)
            return {'state': 'succeeded', 'result': result, 'command_id': command_id}
        except PluginError as exc:
            return {'state': 'failed', 'result': {}, 'error_code': exc.code, 'command_id': command_id}

    def close(self):
        for handler in self.handlers.values():
            handler.close()
