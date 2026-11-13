"""Administrator configuration of the existing owner-scoped platform catalogs."""
from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from types import SimpleNamespace

from hub.application.errors import ApplicationError
from hub.infrastructure.platform_db_catalog import catalog_digest
from hub.platform.providers.openai_compatible import (
    ProviderFactory,
    ProviderUnavailable,
)
from platform_schema import (
    public_model,
    validate_id,
    validate_model_profile,
    validate_node,
    validate_workspace,
)

KINDS = {'models': ('model', 'profile_id', validate_model_profile),
         'workspaces': ('workspace', 'workspace_id', validate_workspace),
         'nodes': ('node', 'node_id', validate_node)}


class SettingsService:
    def __init__(self, repository, secrets):
        self.repository = repository
        self.secrets = secrets

    @staticmethod
    def public(kind, row):
        result = {k: v for k, v in row.items() if k not in {'owner_id', 'secret_ref'}}
        if kind == 'models':
            result.update(public_model(row))
        return {**result, 'digest': catalog_digest(row)}

    def catalog(self, owner):
        return {'ok': True, 'owner_id': owner,
                **{kind: [self.public(kind, row) for row in getattr(self.repository, 'list_' + kind)(owner)] for kind in KINDS},
                'defaults': self.repository.get_defaults(owner)}

    def save(self, owner, kind, identifier, body):
        if kind == 'defaults' and identifier == 'current':
            if not isinstance(body, dict) or not isinstance(body.get('defaults'), dict):
                raise ApplicationError('invalid_settings', '需要默认配置对象', 400)
            return {'ok': True, 'defaults': self.repository.update_defaults(owner, body['defaults'], body.get('revision'))}
        if kind not in KINDS:
            raise ApplicationError('not_found', '配置类别不存在', 404)
        if not isinstance(body, dict) or not isinstance(body.get('expected_digest'), str) or not isinstance(body.get('value'), dict):
            raise ApplicationError('invalid_settings', '需要配置对象及当前版本；请重新加载', 400)
        singular, id_field, validate = KINDS[kind]
        value = dict(body['value'])
        if set(value) - {'models': {'profile_id', 'provider', 'model', 'provider_config', 'capabilities', 'enabled'},
                         'nodes': {'node_id', 'label', 'capabilities', 'enabled'},
                         'workspaces': {'workspace_id', 'name', 'backend', 'root_path', 'default_node_id', 'enabled'}}[kind]:
            raise ApplicationError('invalid_settings', '配置包含不支持的字段', 400)
        value[id_field] = validate_id(identifier, id_field)
        if not isinstance(value.get('enabled', True), bool):
            raise ApplicationError('invalid_settings', '启用状态必须为布尔值', 400)
        if 'capabilities' in value and (not isinstance(value['capabilities'], dict) or any(not isinstance(flag, bool) for flag in value['capabilities'].values())):
            raise ApplicationError('invalid_settings', '能力声明必须是布尔值对象', 400)
        previous = getattr(self.repository, 'get_' + singular)(owner, identifier)
        if catalog_digest(previous) != body['expected_digest']:
            raise ApplicationError('revision_conflict', '配置已更改，请重新加载后保存', 409)
        if kind == 'models':
            if value.get('provider') not in {'deterministic', 'openai_compatible', 'openai-compatible'}:
                raise ApplicationError('invalid_settings', '请选择已支持的模型协议', 400)
            value['secret_ref'] = previous.get('secret_ref') if previous else None
            validate(value)
            if value['provider'] != 'deterministic' and not (value.get('provider_config', {}).get('base_url') or value.get('provider_config', {}).get('endpoint')):
                raise ApplicationError('invalid_settings', '请填写模型 API URL', 400)
            if 'api_key' in body and not isinstance(body['api_key'], str):
                raise ApplicationError('invalid_settings', 'API Key 必须是文本', 400)
            if body.get('api_key'):
                # Validate the actual provider contract before creating an
                # immutable credential file. Rejected forms must not persist keys.
                try:
                    ProviderFactory(secret_broker=SimpleNamespace(resolve=lambda _reference: body['api_key']), allow_network=False)(
                        {**value, 'secret_ref': 'submitted-key'})
                except ProviderUnavailable:
                    raise ApplicationError('invalid_settings', '模型配置无法使用，请核对 API Key、URL 和支持的请求头', 400) from None
                try:
                    value['secret_ref'] = self.secrets.store(body['api_key'])
                except ValueError:
                    raise ApplicationError('invalid_settings', 'API Key 不能为空、含换行或超过 8192 字节', 400) from None
            if value.get('enabled', True) and value['provider'] != 'deterministic' and not value['secret_ref']:
                raise ApplicationError('invalid_settings', '请填写 API Key', 400)
            value = validate(value)
            try:
                if value['enabled']:
                    ProviderFactory(secret_broker=self.secrets, allow_network=False)(value)
            except ProviderUnavailable:
                raise ApplicationError('invalid_settings', '模型配置无法使用，请核对 API Key、URL 和支持的请求头', 400) from None
        if kind == 'workspaces':
            if value.get('backend', 'directory') != 'directory':
                raise ApplicationError('invalid_settings', '当前工作区支持 directory 存储', 400)
            root_path = value.get('root_path')
            if not isinstance(root_path, str) or not (PurePosixPath(root_path).is_absolute() or PureWindowsPath(root_path).is_absolute()):
                raise ApplicationError('invalid_settings', '工作目录需要使用绝对路径', 400)
            node_id = value.get('default_node_id')
            if node_id and not self.repository.get_node(owner, node_id):
                raise ApplicationError('reference_forbidden', '默认执行节点不存在', 409)
        validate(value)
        saved = getattr(self.repository, 'upsert_' + singular)(owner, value, expected_digest=body['expected_digest'])
        return {'ok': True, 'value': self.public(kind, saved)}
