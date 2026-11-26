"""Operator AGY account use cases; the node owns credentials and native login."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import re

from fleet_core.provider_accounts import ACCOUNT_STATES, OPERATIONS, account_id, client_token
from hub.application.errors import ApplicationError
from hub.infrastructure.provider_account_repository import ProviderAccountStoreError

_MACHINE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')
_COMMAND = re.compile(r'cmd_[a-f0-9]{32}\Z')


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')


class ProviderAccountService:
    def __init__(self, repository, supervisor, observation_repo):
        self.repository, self.supervisor, self.observations = repository, supervisor, observation_repo
        self.authorize = None

    @staticmethod
    def _machine(machine):
        if not isinstance(machine, str) or not _MACHINE.fullmatch(machine):
            raise ApplicationError('invalid_machine', '', 400)
        return machine

    @staticmethod
    def _identifier(identifier):
        try:
            return account_id(identifier)
        except ValueError:
            raise ApplicationError('invalid_provider_account_id', '', 400) from None

    @staticmethod
    def _command(command_id):
        if not isinstance(command_id, str) or not _COMMAND.fullmatch(command_id):
            raise ApplicationError('invalid_command_id', '', 400)
        return command_id

    @staticmethod
    def _token(data):
        try:
            return client_token(data.get('client_token'))
        except ValueError:
            raise ApplicationError('invalid_client_token', '', 400) from None

    @staticmethod
    def _call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ProviderAccountStoreError as exc:
            raise ApplicationError(exc.code, '', exc.status) from None

    def _supported(self, machine):
        snapshot = self.observations.read_current(machine)
        if not isinstance(snapshot, dict) or not snapshot.get('reachable', True):
            raise ApplicationError('machine_offline', '', 409)
        agents = snapshot.get('agents')
        agent = agents.get('agy') if isinstance(agents, dict) else None
        if (not isinstance(agent, dict) or agent.get('installed') is not True
                or not isinstance(agent.get('provider_accounts'), dict)
                or type(agent['provider_accounts'].get('schema_version')) is not int
                or agent['provider_accounts']['schema_version'] != 1):
            raise ApplicationError('provider_accounts_unavailable', '', 409)

    def _enqueue(self, connection, machine, identifier, payload, key):
        self._supported(machine)
        return self.supervisor.enqueue(machine, identifier, None, 'manage_provider_account',
                                       'operator_requested', payload=payload,
                                       idempotency_key=key, connection=connection)

    @staticmethod
    def _public_account(row):
        account = {key: row[key] for key in ('account_id', 'machine', 'name', 'provider', 'revision', 'state')}
        account['enabled'] = bool(row['enabled'])
        account['created_at'], account['updated_at'] = _iso(row['created_s']), _iso(row['updated_s'])
        if row['login_command_id']:
            account['login_command_id'] = row['login_command_id']
        if row['authorization_url'] and row['state'] == 'waiting':
            account['authorization_url'] = row['authorization_url']
            account['expires_at'] = row['auth_expires_s']
        if row['last_error']:
            account['error'] = row['last_error']
        return account

    @staticmethod
    def _public_operation(row):
        return {**{key: row[key] for key in ('command_id', 'account_id', 'revision', 'operation', 'state')},
                'status': row['receipt_status'] or row['command_status'],
                'created_at': _iso(row['created_s']), 'expires_at': _iso(row['expires_s'])}

    def create(self, machine, data, actor):
        self._machine(machine)
        if not isinstance(data, dict) or set(data) != {'name', 'client_token'}:
            raise ApplicationError('invalid_provider_account', '', 400)
        name = data.get('name')
        if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 80
                or any(ord(c) < 32 or ord(c) == 127 for c in name)):
            raise ApplicationError('invalid_provider_account_name', '', 400)
        account, operation, created = self._call(self.repository.submit, machine=machine,
            actor=actor, operation='create', token=self._token(data), name=name.strip(), enqueue=self._enqueue)
        return {'ok': True, 'created': created, 'account': self._public_account(account),
                'operation': self._public_operation(operation)}

    def list(self, machine, actor):
        self._machine(machine)
        return {'ok': True, 'accounts': [self._public_account(row) for row in self._call(self.repository.list, machine, actor)]}

    def get(self, identifier, actor):
        self._identifier(identifier)
        return {'ok': True, 'account': self._public_account(self._call(self.repository.get, identifier, actor))}

    def operate(self, identifier, data, actor):
        self._identifier(identifier)
        if not isinstance(data, dict) or set(data) - {'operation', 'client_token', 'response', 'login_command_id'}:
            raise ApplicationError('invalid_provider_operation', '', 400)
        operation = data.get('operation')
        if not isinstance(operation, str) or operation not in OPERATIONS - {'create'}:
            raise ApplicationError('invalid_provider_operation', '', 400)
        expected = {'operation', 'client_token'}
        if operation in {'submit_login', 'cancel_login'}:
            expected.add('login_command_id')
            self._command(data.get('login_command_id'))
        if operation == 'submit_login':
            expected.add('response')
            response = data.get('response')
            if not isinstance(response, str) or not 1 <= len(response) <= 8192:
                raise ApplicationError('invalid_login_response', '', 400)
        if set(data) != expected:
            raise ApplicationError('invalid_provider_operation', '', 400)
        account = self._call(self.repository.get, identifier, actor)
        account, operation_row, created = self._call(self.repository.submit,
            machine=account['machine'], actor=actor, operation=operation, token=self._token(data),
            identifier=identifier, response=data.get('response'), login_command_id=data.get('login_command_id'),
            enqueue=self._enqueue)
        return {'ok': True, 'created': created, 'account': self._public_account(account),
                'operation': self._public_operation(operation_row)}

    def operation(self, identifier, command_id, actor):
        self._identifier(identifier)
        self._command(command_id)
        row = self._call(self.repository.operation, identifier, command_id, actor)
        return {'ok': True, 'operation': self._public_operation(row)}

    def resolve(self, machine, command_id):
        self._command(command_id)
        return {'configuration': self._call(self.repository.resolve, machine, command_id, self.authorize)}

    def report(self, machine, data):
        if (not isinstance(data, dict) or set(data) - {'command_id', 'account_id', 'revision',
                'state', 'authorization_url', 'expires_at', 'error'}
                or not {'command_id', 'account_id', 'revision', 'state'} <= set(data)):
            raise ApplicationError('invalid_provider_report', '', 400)
        self._identifier(data['account_id'])
        self._command(data['command_id'])
        if (type(data['revision']) is not int or not 1 <= data['revision'] <= 2**53
                or not isinstance(data['state'], str) or data['state'] not in ACCOUNT_STATES
                or not isinstance(data.get('error', ''), str) or len(data.get('error', '')) > 80
                or 'authorization_url' in data and data['state'] != 'waiting'):
            raise ApplicationError('invalid_provider_report', '', 400)
        return {'ok': True, 'accepted': self._call(self.repository.report, machine, data, self.authorize)}

    def assert_ready(self, identifier, machine, actor):
        self._identifier(identifier)
        if self.authorize is not None and not self.authorize(actor):
            raise ApplicationError('forbidden', '', 403)
        row = self._call(self.repository.get, identifier, actor)
        if row['machine'] != machine:
            raise ApplicationError('provider_account_not_found', '', 404)
        self._supported(machine)
        if not row['enabled'] or row['state'] != 'ready':
            raise ApplicationError('provider_account_not_ready', '', 409)
        return self._public_account(row)

    @contextmanager
    def task_delivery(self, machine):
        """Keep account admission fenced until the task lease commits."""
        try:
            with self.repository.task_delivery(machine, self.authorize) as bindings:
                yield bindings
        except ProviderAccountStoreError as exc:
            raise ApplicationError(exc.code, '', exc.status) from None
