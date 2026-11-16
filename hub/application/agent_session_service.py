"""Hub-only assistant adapter for existing fleet sessions and signed controls.

The fleet is administrator-scoped, unlike owner-scoped assistant conversations.
Authorization is injected and rechecked for every call, including receipt reads.
Machine identity always comes from the session; a workspace Node is unrelated.
"""
from __future__ import annotations

import hashlib
import json
import logging

from session_schema import is_valid_session_id
from hub.repositories import MACHINE_NAME_RE
from hub.application.errors import ApplicationError
from hub.application.session_service import SessionServiceError
from hub.application.adoption_service import AdoptionServiceError
from hub.application.supervisor_service import SupervisorServiceError
from hub.infrastructure.adoption_repository import AdoptionRepositoryError
from hub.domain.agent_session import SESSION_CONTROL_ACTIONS

logger = logging.getLogger(__name__)


def _invalid():
    raise ApplicationError('invalid_arguments', 'invalid_arguments')


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        _invalid()
    return value


class AgentSessionService:
    def __init__(self, sessions, *, observe=None, adoptions=None, supervisor=None,
                 authorize=None):
        self.sessions = sessions
        self.observe = observe
        self.adoptions = adoptions
        self.supervisor = supervisor
        self.authorize = authorize

    def _authorized(self, owner_id):
        return bool(owner_id and (self.authorize is None or self.authorize(owner_id)))

    def available_tools(self, owner_id):
        if self.sessions is None or not self._authorized(owner_id):
            return frozenset()
        tools = {'session.list', 'session.get', 'session.read_events'}
        if self.observe is not None:
            tools.add('fleet.list_machines')
        if self.supervisor is not None:
            tools.add('session.get_command')
            actions = self.supervisor.available_actions()
            if set(SESSION_CONTROL_ACTIONS) & actions:
                tools.add('session.control')
            if 'append_user_turn' in actions:
                tools.add('session.send_message')
        return frozenset(tools)

    def _adoption(self, session_id):
        if not session_id.startswith('adopt_'):
            return None
        if self.adoptions is None:
            raise ApplicationError('adoption_unavailable', 'adoption_unavailable', 503)
        row = self.adoptions.adoption_repo.get(session_id)
        if row is None:
            raise ApplicationError('unknown_session', 'unknown_session', 404)
        return row

    def _session(self, session_id):
        adoption = self._adoption(session_id)
        try:
            session = self.sessions.get_session(session_id)['session']
        except SessionServiceError as exc:
            if exc.code != 'session_not_found' or adoption is None:
                raise
            # An adoption may be pending before its first captured event.
            session = {'session_id': session_id, 'machine_id': adoption.machine_id,
                       'managed': False, 'agent_family': adoption.agent_family,
                       'capture_quality': adoption.capture_quality, 'current_state': 'unknown'}
        actions = set(self.supervisor.available_actions()) if self.supervisor is not None else set()
        actions &= {*SESSION_CONTROL_ACTIONS, 'append_user_turn'}
        if adoption is not None:
            session['adoption_status'] = adoption.status
            if adoption.status != 'adopted':
                actions.clear()
            # Adoption control can operate before the capture stream exists.
        elif not session.get('managed'):
            actions.clear()
        if session.get('agent_family') == 'hermes':
            actions.discard('append_user_turn')
        session['allowed_actions'] = sorted(actions)
        return session, adoption

    def execute(self, *, tool, arguments, owner_id, command_id):
        if not self._authorized(owner_id):
            raise ApplicationError('forbidden', 'forbidden', 403)
        if tool not in self.available_tools(owner_id):
            raise ApplicationError('tool_not_allowed', 'tool_not_allowed', 403)
        try:
            return self._execute(tool, arguments, owner_id, command_id)
        except (SessionServiceError, AdoptionServiceError, SupervisorServiceError,
                AdoptionRepositoryError) as exc:
            raise ApplicationError(exc.code, exc.code, getattr(exc, 'status', 400)) from exc

    def _original_command(self, key, session_id, action, payload):
        original = self.supervisor.find_idempotency(key)
        if not original:
            return None
        if (original['target']['session_id'] != session_id or original['action'] != action
                or original.get('payload') != payload):
            raise ApplicationError('idempotency_conflict', 'idempotency_conflict', 409)
        return {'session_id': session_id, 'command_id': original['command_id'], 'status': 'pending'}

    def _execute(self, tool, args, owner_id, command_id):
        keys, required = {
            'fleet.list_machines': ({'offset', 'limit'}, set()),
            'session.list': ({'machine_id', 'active', 'limit'}, set()),
            'session.get': ({'session_id'}, {'session_id'}),
            'session.read_events': ({'session_id', 'limit', 'after_sequence'}, {'session_id'}),
            'session.send_message': ({'session_id', 'text'}, {'session_id', 'text'}),
            'session.control': ({'session_id', 'action'}, {'session_id', 'action'}),
            'session.get_command': ({'session_id', 'command_id'}, {'session_id', 'command_id'}),
        }[tool]
        if not isinstance(args, dict) or set(args) - keys or not required <= args.keys():
            _invalid()
        if tool == 'fleet.list_machines':
            limit = _integer(args.get('limit', 50), 1, 100)
            offset = _integer(args.get('offset', 0), 0, (1 << 31) - 1)
            rows = self.observe.status()['machines']
            return {'machines': [{'machine_id': row['machine'], 'description': row.get('desc', ''),
                                 'reachable': row.get('online', False), 'observed_at': row.get('timestamp')}
                                for row in rows[offset:offset + limit]],
                    'next_offset': offset + limit if offset + limit < len(rows) else None}
        if tool == 'session.list':
            machine = args.get('machine_id')
            if machine is not None and (not isinstance(machine, str) or not MACHINE_NAME_RE.fullmatch(machine)):
                _invalid()
            active = args.get('active', False)
            if type(active) is not bool:
                _invalid()
            limit = _integer(args.get('limit', 50), 1, 1000)
            result = self.sessions.list_sessions(machine=machine, active=active, limit=limit)
            return {**result, 'possibly_more': len(result['sessions']) == limit}
        session_id = args.get('session_id')
        if not isinstance(session_id, str) or not is_valid_session_id(session_id):
            _invalid()
        if tool == 'session.get_command':
            cid = args.get('command_id')
            if not isinstance(cid, str) or not 1 <= len(cid) <= 128:
                _invalid()
            receipt = self.supervisor.receipt_status(cid)
            if receipt['session_id'] != session_id:
                raise ApplicationError('command_not_found', 'command_not_found', 404)
            return {'receipt': receipt}
        session, adoption = self._session(session_id)
        if tool == 'session.get':
            return {'session': session}
        if tool == 'session.read_events':
            limit = _integer(args.get('limit', 20), 1, 100)
            after = args.get('after_sequence', '0')
            if (not isinstance(after, str) or not after.isascii() or not after.isdecimal()
                    or not 1 <= len(after) <= 19 or int(after) > (1 << 63) - 1):
                _invalid()
            rows = self.sessions.session_events(session_id, limit=limit, after_sequence=int(after))['events']
            events, size = [], 0
            for row in rows:
                event_size = len(json.dumps(row, ensure_ascii=False).encode('utf-8'))
                if events and size + event_size > 128 * 1024:
                    break
                events.append(row)
                size += event_size
            return {'events': events, 'next_sequence': str(events[-1]['sequence']) if events else after,
                    'has_more': len(events) < len(rows) or len(rows) == limit}
        if tool == 'session.send_message':
            text = args.get('text')
            if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                _invalid()
            action, payload = 'append_user_turn', {'text': text}
        else:
            action, payload = args.get('action'), None
            if not isinstance(action, str) or action not in SESSION_CONTROL_ACTIONS:
                _invalid()
        if not isinstance(command_id, str) or not command_id:
            _invalid()
        key = 'assistant-session:' + hashlib.sha256(f'{owner_id}\0{command_id}'.encode()).hexdigest()
        original = self._original_command(key, session_id, action, payload)
        if original:
            return original
        try:
            if adoption is not None:
                # This service owns adoption state, identity-drift and family guards.
                return self.adoptions.control(session_id, action=action, payload=payload,
                    reason_code='operator_requested', actor=owner_id, idempotency_key=key)
            if not session.get('managed'):
                raise ApplicationError('session_not_managed', 'session_not_managed', 409)
            if action not in session['allowed_actions']:
                raise ApplicationError('session_action_unavailable', 'session_action_unavailable', 409)
            envelope = self.supervisor.enqueue(session['machine_id'], session_id, session.get('attempt_id'),
                action, 'operator_requested', payload=payload, idempotency_key=key)
        except Exception as exc:
            # Admission and its Supervisor audit commit atomically. If a later
            # adoption audit/response fails, recover the existing command instead
            # of falsely reporting an unqueued task or sending a replacement.
            original = self._original_command(key, session_id, action, payload)
            if original is None:
                raise
            logger.warning('assistant_session_control_reconciled', extra={
                'session_id': session_id, 'command_id': original['command_id'],
                'error_code': getattr(exc, 'code', 'control_response_lost')})
            return original
        return {'session_id': session_id, 'command_id': envelope['command_id'], 'status': 'pending'}


__all__ = ['AgentSessionService']
