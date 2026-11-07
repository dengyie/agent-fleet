"""Plugin enablement and short, durable operations over the existing node bus."""
from __future__ import annotations

import hashlib
import base64
import time

from fleet_core.plugins import OFFICIAL_PLUGINS, OFFER_FRESHNESS_S, RECOVERY_ACTIONS, PluginError, validate_offers, identifier, hostname as normalize_hostname
from fleet_core.platform_command import PlatformCommand
from hub.application.errors import ApplicationError


class PluginService:
    def __init__(self, repository, platform, delivery, *, registry=OFFICIAL_PLUGINS, clock=time.time):
        self.repository, self.platform, self.delivery = repository, platform, delivery
        self.registry, self.clock = registry, clock

    def catalog(self, owner):
        ready = bool(self.delivery.signing_key)
        return {'enrollment_ready': ready, 'setup_error': None if ready else 'command_signature_required',
                'plugins': [{**p.manifest(), **self.repository.setting(owner, p.plugin_id)}
                            for p in self.registry.definitions()]}

    def enroll(self, owner, node_id, label):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        from platform_schema import validate_node
        from hub.infrastructure.platform_db_base import PlatformRepositoryError
        if not self.delivery.signing_key:
            raise ApplicationError('command_signature_required', '请先配置平台命令签名', 503)
        normalized = validate_node({'node_id': node_id, 'label': label})
        public = Ed25519PrivateKey.from_private_bytes(self.delivery.signing_key).public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw)
        try:
            credential = self.platform.enroll_node(owner, normalized)
        except PlatformRepositoryError as exc:
            if exc.code == 'node_already_exists':
                raise PluginError('plugin_node_exists') from None
            raise
        return {'node_id': normalized['node_id'], 'credential': credential['credential'],
                'public_key': base64.b64encode(public).decode('ascii'),
                'plugins': {'github-cli': {'contexts': [{'context_id': 'default', 'label': '默认开发用户'}]}}}

    def configure(self, owner, plugin_id, enabled, revision):
        self.registry.get(plugin_id)
        return self.repository.configure(owner, plugin_id, enabled, revision)

    def set_target(self, owner, plugin_id, node_id, enabled):
        self.registry.get(plugin_id)
        if not self.platform.get_node(owner, node_id):
            raise PluginError('node_not_found')
        self.repository.set_target(owner, plugin_id, node_id, enabled)
        return {'node_id': node_id, 'enabled': enabled}

    def report(self, owner, node_id, offers):
        self.repository.report(owner, node_id, validate_offers(offers, self.registry))

    def targets(self, owner, plugin_id):
        plugin = self.registry.get(plugin_id)
        targets, offers, observations = self.repository.node_data(owner, plugin_id)
        rows = []
        for node in self.platform.list_nodes(owner):
            offer = offers.get(node['node_id'])
            if not node['enabled']:
                ready = 'node_disabled'
            elif not offer:
                ready = 'not_installed'
            elif (self.clock() - offer['reported_at'] > OFFER_FRESHNESS_S
                  or not node.get('last_seen_at') or self.clock() - node['last_seen_at'] > OFFER_FRESHNESS_S):
                ready = 'offline'
            elif offer['api_version'] != plugin.api_version or offer['version'] != plugin.version:
                ready = 'plugin_incompatible'
            elif not offer['available']:
                ready = 'dependency_missing'
            else:
                ready = 'ready'
            rows.append({'node_id': node['node_id'], 'label': node['label'],
                         'enabled': targets.get(node['node_id'], False), 'readiness': ready,
                         'offer': offer, 'observations': observations.get(node['node_id'], [])})
        return {'nodes': rows}

    def submit(self, owner, plugin_id, node_id, action, arguments, idempotency_key):
        plugin = self.registry.get(plugin_id)
        args = plugin.validate(action, arguments)
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise PluginError('invalid_idempotency_key')
        # Scope user-provided keys by owner AND plugin; identical UI keys in
        # separate accounts must not collide in the shared command table.
        digest = hashlib.sha256((owner + '\0' + plugin_id + '\0' + idempotency_key).encode()).hexdigest()
        command_id = 'plugin_' + digest[:40]
        prior = self.delivery.repository.get_for_owner(owner, command_id)
        if prior:
            if prior['target_node'] != node_id or prior['action'] != f'plugin.{plugin_id}.{action}' or prior['arguments'] != args:
                raise PluginError('idempotency_conflict')
            return self._public(prior)
        target = next((n for n in self.targets(owner, plugin_id)['nodes'] if n['node_id'] == node_id), None)
        if target is None:
            raise PluginError('node_not_found')
        if action not in RECOVERY_ACTIONS and (
                not self.repository.setting(owner, plugin_id)['enabled'] or not target['enabled']):
            raise PluginError('plugin_disabled')
        if target['readiness'] != 'ready' and not (
                action in RECOVERY_ACTIONS and target['readiness'] == 'dependency_missing'):
            raise PluginError(target['readiness'])
        if args['context_id'] not in {c['context_id'] for c in target['offer']['contexts']}:
            raise PluginError('context_unavailable')
        if action in RECOVERY_ACTIONS:
            original = self.delivery.repository.get_for_owner(owner, args['login_id'])
            if (not original or original['action'] != f'plugin.{plugin_id}.login_start'
                    or original['target_node'] != node_id
                    or any(original['arguments'][key] != args[key] for key in ('context_id', 'hostname'))):
                raise PluginError('login_not_found')
        if not self.delivery.signing_key:
            raise ApplicationError('command_signature_required', '请先配置平台命令签名', 503)
        command = PlatformCommand.create(
            command_id=command_id, target_node=node_id, owner_id=owner,
            action=f'plugin.{plugin_id}.{action}', resource_id=args['context_id'], arguments=args,
            retry_class='read_only' if action in {'status', 'login_status', 'repo_check'} else 'reconcile_before_retry',
            expires_at=self.clock() + 90)
        return self._public(self.delivery.enqueue(command, idempotency_key='plugin:' + digest))

    @staticmethod
    def _public(row):
        _, plugin, action = row['action'].split('.', 2)
        envelope = row.get('result') or {}
        return {'operation_id': row['command_id'], 'plugin_id': plugin, 'node_id': row['target_node'],
                'action': action, 'arguments': row['arguments'], 'status': row['status'],
                'result': envelope.get('result', {}), 'error_code': envelope.get('error_code'),
                'created_at': row['created_at'], 'updated_at': row['updated_at']}

    def operation(self, owner, plugin_id, operation_id):
        self.registry.get(plugin_id)
        self.delivery.repository.expire_stale()
        row = self.delivery.repository.get_for_owner(owner, operation_id)
        if not row or not row['action'].startswith('plugin.' + plugin_id + '.'):
            raise PluginError('operation_not_found')
        return self._public(row)

    def operations(self, owner, plugin_id, *, node_id=None, context_id=None, hostname=None):
        self.registry.get(plugin_id)
        if node_id is not None:
            identifier(node_id, 'invalid_node_id')
        scoped = context_id is not None or hostname is not None
        if scoped:
            if node_id is None or context_id is None or hostname is None:
                raise PluginError('invalid_operation_scope')
            identifier(context_id, 'invalid_context')
            hostname = normalize_hostname(hostname)
        self.delivery.repository.expire_stale()
        return {'operations': [self._public(r) for r in self.repository.operations(
                    owner, plugin_id, node_id=node_id, context_id=context_id, hostname=hostname)],
                'login': self.repository.current_login(owner, plugin_id, node_id, context_id, hostname) if scoped else None}

    def record_receipt(self, owner, node, row):
        if not row['action'].startswith('plugin.') or row['status'] != 'succeeded':
            return
        _, plugin_id, _ = row['action'].split('.', 2)
        result = (row.get('result') or {}).get('result') or {}
        auth = result.get('auth')
        if not isinstance(auth, dict):
            return
        args = row['arguments']
        self.repository.observe(owner, node, plugin_id, args['context_id'], args['hostname'],
                                {**auth, 'context_id': args['context_id'], 'hostname': args['hostname'],
                                 'observed_at': row['updated_at']}, row['created_at'])
