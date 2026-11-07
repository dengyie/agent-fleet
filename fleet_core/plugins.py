"""Pure, versioned contracts shared by the Hub and official plugin nodes."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, Mapping

PLUGIN_API_VERSION = 1
OFFER_FRESHNESS_S = 120
RECOVERY_ACTIONS = frozenset({'login_status', 'login_cancel'})
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
_HOST = re.compile(r'^(?=.{1,253}$)[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?$')
_USER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]{0,38}$')
_REPO = re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$')


class PluginError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def identifier(value, code='invalid_plugin_id') -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value) or value in {'.', '..'}:
        raise PluginError(code)
    return value


def hostname(value) -> str:
    if not isinstance(value, str) or not _HOST.fullmatch(value) or '..' in value:
        raise PluginError('invalid_hostname')
    return value.lower()


def _github_arguments(action: str, arguments: Mapping) -> dict:
    extra = {'switch': {'username'}, 'logout': {'username'},
             'repo_check': {'repository'}, 'login_status': {'login_id'},
             'login_cancel': {'login_id'}}.get(action, set())
    if not isinstance(arguments, dict) or set(arguments) - ({'context_id', 'hostname'} | extra):
        raise PluginError('invalid_arguments')
    context = identifier(arguments.get('context_id', 'default'), 'invalid_context')
    result = {'context_id': context, 'hostname': hostname(arguments.get('hostname', 'github.com'))}
    if action in {'switch', 'logout'}:
        user = arguments.get('username')
        if not isinstance(user, str) or not _USER.fullmatch(user):
            raise PluginError('invalid_username')
        result['username'] = user
    if action == 'repo_check':
        repo = arguments.get('repository')
        if not isinstance(repo, str) or not _REPO.fullmatch(repo) or repo.split('/')[1] in {'.', '..'}:
            raise PluginError('invalid_repository')
        result['repository'] = repo
    if action in RECOVERY_ACTIONS:
        result['login_id'] = identifier(arguments.get('login_id'), 'invalid_login_id')
    return result


@dataclass(frozen=True)
class PluginDefinition:
    plugin_id: str
    name: str
    description: str
    version: str
    actions: tuple[str, ...]
    validator: Callable
    api_version: int = PLUGIN_API_VERSION
    ui_module: str = ''

    def validate(self, action: str, arguments) -> dict:
        if not isinstance(action, str) or action not in self.actions:
            raise PluginError('unknown_plugin_action')
        return self.validator(action, arguments)

    def manifest(self) -> dict:
        return {'plugin_id': self.plugin_id, 'name': self.name,
                'description': self.description, 'version': self.version,
                'api_version': self.api_version, 'official': True,
                'actions': list(self.actions), 'ui_module': self.ui_module,
                'platforms': ['linux', 'darwin', 'win32']}


class PluginRegistry:
    def __init__(self, definitions=()):
        self._definitions = {}
        for definition in definitions:
            identifier(definition.plugin_id)
            if definition.plugin_id in self._definitions:
                raise PluginError('duplicate_plugin')
            self._definitions[definition.plugin_id] = definition

    def get(self, plugin_id) -> PluginDefinition:
        if not isinstance(plugin_id, str) or plugin_id not in self._definitions:
            raise PluginError('plugin_not_found')
        return self._definitions[plugin_id]

    def definitions(self) -> tuple[PluginDefinition, ...]:
        return tuple(self._definitions.values())


OFFICIAL_PLUGINS = PluginRegistry((PluginDefinition(
    plugin_id='github-cli', name='GitHub CLI', version='1.0.0',
    description='管理各节点的 GitHub CLI 登录、账号切换与仓库访问。',
    actions=('status', 'login_start', 'login_status', 'login_cancel', 'switch', 'logout', 'repo_check'),
    validator=_github_arguments, ui_module='github-cli',
),))


def validate_offers(offers, registry=OFFICIAL_PLUGINS) -> list[dict]:
    if not isinstance(offers, list) or len(offers) > 32:
        raise PluginError('invalid_plugin_offer')
    normalized, seen = [], set()
    for offer in offers:
        if not isinstance(offer, dict):
            raise PluginError('invalid_plugin_offer')
        plugin = registry.get(offer.get('plugin_id'))
        if plugin.plugin_id in seen:
            raise PluginError('duplicate_plugin')
        seen.add(plugin.plugin_id)
        version, api = offer.get('version'), offer.get('api_version')
        if (not isinstance(version, str) or not re.fullmatch(r'\d{1,4}\.\d{1,4}\.\d{1,4}', version)
                or type(api) is not int or not 1 <= api <= 999
                or type(offer.get('available')) is not bool):
            raise PluginError('invalid_plugin_offer')
        contexts = offer.get('contexts')
        if not isinstance(contexts, list) or not 1 <= len(contexts) <= 16:
            raise PluginError('invalid_plugin_contexts')
        clean_contexts, ids = [], set()
        for context in contexts:
            if not isinstance(context, dict):
                raise PluginError('invalid_plugin_contexts')
            cid = identifier(context.get('context_id'), 'invalid_context')
            if cid in ids or type(context.get('env_override')) is not bool:
                raise PluginError('invalid_plugin_contexts')
            ids.add(cid)
            clean = {'context_id': cid, 'env_override': context['env_override']}
            sources = context.get('environment_sources', [])
            if not isinstance(sources, list) or any(not isinstance(s, str) or s not in {
                    'GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'} for s in sources):
                raise PluginError('invalid_plugin_contexts')
            clean['environment_sources'] = list(dict.fromkeys(sources))
            for key, limit in (('label', 128), ('os_user', 128), ('config_dir', 1024)):
                value = context.get(key, '')
                if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
                    raise PluginError('invalid_plugin_contexts')
                clean[key] = value
            clean_contexts.append(clean)
        dependency = offer.get('dependency_version', '')
        if not isinstance(dependency, str) or len(dependency) > 160:
            raise PluginError('invalid_plugin_offer')
        normalized.append({'plugin_id': plugin.plugin_id, 'version': version,
                           'api_version': api, 'available': offer['available'],
                           'dependency_version': dependency, 'contexts': clean_contexts})
    return normalized
