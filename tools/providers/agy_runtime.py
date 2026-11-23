"""Antigravity ACP lifecycle; native IDs and protocol stay on the node."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from fleet_core.session.redact import redact_secrets_text
from tools.providers.acp_transport import AcpTransport, AcpTransportError
from tools.providers.acp_mailbox import AcpMailbox

MAX_FIELD = 8192
_NATIVE = re.compile(r'^[^\x00-\x20\x7f]{1,256}$')


def _text(value, limit=MAX_FIELD):
    return value[:limit] if isinstance(value, str) else ''


def native_identity(value):
    return value if isinstance(value, str) and _NATIVE.fullmatch(value) else None


class AgyRuntime:
    def __init__(self, process, *, cwd, emit, control_dir=None, should_abort=None,
                 request_timeout=90, settle_gap=.5, background_timeout=1800):
        self.process, self.cwd, self.emit = process, str(Path(cwd).resolve()), emit
        self.control_dir, self.should_abort = control_dir, should_abort or (lambda: False)
        self.timeout, self.settle_gap = request_timeout, settle_gap
        self.background_timeout = background_timeout
        self.session_id = None
        self.invocation = secrets.token_hex(16)
        self.tools = {}
        self.active_message = None
        self.message_number = 0
        self.active_text = False
        self.last_activity = time.monotonic()
        self.background_pending, self.permissions = set(), {}
        self._lock = threading.RLock()
        self._cancelled = False
        self._creating = False
        self._early_updates = []
        self.transport = AcpTransport(process, on_notification=self._notification,
            on_client_request=self._client_request, should_abort=self.should_abort)

    def _id(self, native):
        return 'agy_' + hashlib.sha256(f'{self.invocation}:{native}'.encode()).hexdigest()[:32]

    def _emit(self, kind, **payload):
        self.emit({'type': kind, **payload})

    def _state(self, state, reason):
        self._emit('agent_state_changed', state=state, source='structured_stream',
            confidence='high', reason=reason)

    def _finish_message(self):
        if self.active_message and self.active_text:
            self._emit('AssistantMessage', text='', message_id=self.active_message,
                content_mode='delta', is_complete=True)
        self.active_message, self.active_text = None, False

    def _client_request(self, row):
        params = row.get('params') if isinstance(row.get('params'), Mapping) else {}
        if row.get('method') != 'session/request_permission':
            return {'error': {'code': -32601, 'message': 'Method not found'}}
        if params.get('sessionId') != self.session_id:
            return {'result': {'outcome': {'outcome': 'cancelled'}}}
        raw_id, options = row.get('id'), params.get('options')
        subject = params.get('subject')
        tool = params.get('toolCall') or (subject.get('toolCall') if isinstance(subject, Mapping) else None)
        if (type(raw_id) not in (str, int) or isinstance(raw_id, str) and len(raw_id) > 256
                or not isinstance(options, list) or not 1 <= len(options) <= 16
                or not isinstance(tool, Mapping)):
            return {'result': {'outcome': {'outcome': 'cancelled'}}}
        public_id = self._id('permission:' + str(raw_id))
        clean, native = [], {}
        for index, option in enumerate(options):
            if not isinstance(option, Mapping): continue
            oid, name = native_identity(option.get('optionId')), _text(option.get('name'), 200)
            if oid and name:
                key = f'option_{index}'
                native[key] = oid
                clean.append({'id': key, 'label': name})
        if not clean or not self.control_dir or not AcpMailbox.publish(self.control_dir, public_id, clean):
            return {'result': {'outcome': {'outcome': 'cancelled'}}}
        with self._lock: self.permissions[public_id] = raw_id
        self._emit('interaction_request', request_id=public_id, question_type='single',
            prompt=_text(params.get('title') or tool.get('title'), 1000) or 'Agent requests permission',
            options=clean, allow_custom=False, delivery='native_permission',
            tool_name=_text(tool.get('title'), 128) or 'Agent action')
        self._state('waiting_for_approval', 'approval_required')
        deadline = time.monotonic() + 300
        selected = None
        while not self.should_abort() and time.monotonic() < deadline:
            selected = AcpMailbox.wait(self.control_dir, public_id, timeout=.05)
            if selected is not None: break
        if selected not in native:
            with self._lock: self.permissions.pop(public_id, None)
            return {'result': {'outcome': {'outcome': 'cancelled'}}}
        # Transport invokes this hook only after response bytes have flushed;
        # writing a mailbox answer alone is never a delivery receipt.
        def delivered():
            try:
                if not AcpMailbox.ack(self.control_dir, public_id):
                    self.transport._fail('permission_delivery_unknown')
                    self._state('unknown', 'source_unavailable')
                    return
                self._emit('interaction_response', request_id=public_id, selected=selected)
                self._state('running', 'user_prompt_submitted')
            finally:
                with self._lock:
                    self.permissions.pop(public_id, None)
                    self.last_activity = time.monotonic()
        return {'result': {'outcome': {'outcome': 'selected', 'optionId': native[selected]}},
                '_on_delivered': delivered}

    def _notification(self, row):
        if row.get('method') != 'session/update': return
        params = row.get('params')
        if not isinstance(params, Mapping): raise AcpTransportError('invalid_update')
        if self.session_id is None and self._creating:
            with self._lock:
                if len(self._early_updates) >= 8: raise AcpTransportError('early_update_limit')
                self._early_updates.append(row)
            return
        if params.get('sessionId') != self.session_id:
            # Updates for unrelated root/child sessions must not be attributed
            # to this conversation. No native identity is put in errors.
            raise AcpTransportError('session_mismatch')
        update = params.get('update')
        if not isinstance(update, Mapping): raise AcpTransportError('invalid_update')
        with self._lock:
            self.last_activity = time.monotonic()
            kind = update.get('sessionUpdate')
            if not isinstance(kind, str): raise AcpTransportError('invalid_update')
            if kind == 'agent_message_chunk':
                content = update.get('content')
                if not isinstance(content, Mapping) or content.get('type') != 'text': return
                text = content.get('text')
                if not isinstance(text, str): return
                if not text: return
                if self.active_message is None:
                    self.message_number += 1
                    self.active_message = self._id('message:' + str(self.message_number))
                self.active_text = True
                for start in range(0, len(text), 8192):
                    self._emit('AssistantMessage', text=text[start:start + 8192], message_id=self.active_message,
                        content_mode='delta', is_complete=False)
            elif kind in {'tool_call', 'tool_call_update'}:
                self._tool(kind, update)
            elif kind == 'session_info_update':
                title = _text(update.get('title'), 512)
                if title: self._emit('session_metadata', title=title, runtime_protocol='acp')
            elif kind == 'config_option_update':
                options = update.get('configOptions')
                if isinstance(options, list):
                    for option in options[:64]:
                        if isinstance(option, Mapping) and option.get('category') == 'model' and isinstance(option.get('currentValue'), str):
                            self._emit('session_metadata', model=option['currentValue'][:256], runtime_protocol='acp')

    def _tool(self, kind, update):
        native = native_identity(update.get('toolCallId'))
        if not native: raise AcpTransportError('invalid_tool_identity')
        if native not in self.tools and len(self.tools) >= 256: raise AcpTransportError('tool_limit')
        if kind == 'tool_call':
            self._finish_message()
            self.tools[native] = {'title': 'Agent tool', 'arguments': '', 'result': '', 'status': 'pending'}
        tool = self.tools.get(native)
        if tool is None: raise AcpTransportError('unknown_tool_update')
        title = _text(update.get('title'), 128)
        if title: tool['title'] = title
        if 'rawInput' in update:
            tool['arguments'] = self._tool_field(update['rawInput'])
        if 'rawOutput' in update:
            tool['result'] = self._tool_field(update['rawOutput'])
        elif isinstance(update.get('content'), list):
            texts = []
            for block in update['content']:
                content = block.get('content') if isinstance(block, Mapping) else None
                if isinstance(content, Mapping) and content.get('type') == 'text' and isinstance(content.get('text'), str):
                    texts.append(content['text'])
            if texts: tool['result'] = self._tool_field('\n'.join(texts))
        status = update.get('status', tool['status'])
        if status not in {'pending', 'in_progress', 'completed', 'failed'}: raise AcpTransportError('invalid_tool_status')
        tool['status'] = status
        call_id = self._id('tool:' + native)
        if kind == 'tool_call':
            self._emit('PreToolUse', tool_name=tool['title'], call_id=call_id,
                arguments=tool['arguments'], status='running')
        if status in {'completed', 'failed'}:
            self.background_pending.discard(native)
            self._emit('PostToolUse', tool_name=tool['title'], call_id=call_id,
                result=tool['result'], status='error' if status == 'failed' else 'ok')
        else: self.background_pending.add(native)

    def _tool_field(self, value):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        text = redact_secrets_text(text)[0]
        if len(text) <= MAX_FIELD:
            return text
        # Retain only the same visible tool field, never the surrounding ACP
        # frame or hidden provider metadata. Existing capture redacts before
        # encrypted persistence; source archives remain excluded from chat DTOs.
        for start in range(0, len(text), 4000):
            self.emit({'kind': 'source_record', 'payload': {'text': text[start:start + 4000],
                'is_complete': start + 4000 >= len(text)}})
        marker = '\n[truncated; full tool field retained in source archive]'
        return text[:MAX_FIELD - len(marker)] + marker

    def _configure(self, setup, category, requested):
        if requested is None: return
        if not native_identity(requested): raise AcpTransportError('invalid_configuration')
        options = setup.get('configOptions')
        advertised = next((option for option in options[:64]
            if isinstance(option, Mapping) and option.get('category') == category
            and option.get('type') == 'select'), None) if isinstance(options, list) else None
        if advertised is not None:
            values, pending = set(), [advertised.get('options')]
            while pending:
                entries = pending.pop()
                if not isinstance(entries, list): continue
                if len(entries) > 64: raise AcpTransportError('invalid_configuration')
                for entry in entries:
                    if not isinstance(entry, Mapping): continue
                    if isinstance(entry.get('value'), str): values.add(entry['value'])
                    if 'options' in entry:
                        if len(pending) > 8: raise AcpTransportError('invalid_configuration')
                        pending.append(entry['options'])
                if len(values) > 256: raise AcpTransportError('invalid_configuration')
            if requested not in values or not native_identity(advertised.get('id')):
                raise AcpTransportError('configuration_unavailable')
            if requested != advertised.get('currentValue'):
                self.transport.request('session/set_config_option', {'sessionId': self.session_id,
                    'configId': advertised['id'], 'value': requested}, timeout=self.timeout)
        else:
            key, field, current, method = (('models', 'modelId', 'currentModelId', 'session/set_model')
                if category == 'model' else ('modes', 'id', 'currentModeId', 'session/set_mode'))
            state = setup.get(key)
            entries = state.get('availableModels' if category == 'model' else 'availableModes') if isinstance(state, Mapping) else None
            if (not isinstance(entries, list) or len(entries) > 128
                    or not any(isinstance(entry, Mapping) and entry.get(field) == requested for entry in entries)):
                raise AcpTransportError('configuration_unavailable')
            if requested != state.get(current):
                self.transport.request(method, {'sessionId': self.session_id,
                    'modelId' if category == 'model' else 'modeId': requested}, timeout=self.timeout)
        if category == 'model': self._emit('session_metadata', model=requested, runtime_protocol='acp')

    def run(self, text, resume_session=None, *, requested_model=None, requested_mode=None):
        try:
            init = self.transport.request('initialize', {'protocolVersion': 1,
                'clientInfo': {'name': 'agent-fleet', 'title': 'Agent Fleet', 'version': '1.0.0'},
                'clientCapabilities': {'fs': {'readTextFile': False, 'writeTextFile': False}, 'terminal': False}}, timeout=self.timeout)
            if type(init.get('protocolVersion')) is not int or init['protocolVersion'] != 1:
                raise AcpTransportError('protocol_version_unsupported')
            info = init.get('agentInfo') if isinstance(init.get('agentInfo'), Mapping) else {}
            self._emit('session_metadata', runtime_protocol='acp', adapter='acp', client_version=_text(info.get('version'), 64))
            self.transport.request('authenticate', {'methodId': 'oauth-personal'}, timeout=self.timeout)
            caps = init.get('agentCapabilities') if isinstance(init.get('agentCapabilities'), Mapping) else {}
            session_caps = caps.get('sessionCapabilities') if isinstance(caps.get('sessionCapabilities'), Mapping) else {}
            if resume_session:
                if not native_identity(resume_session) or not isinstance(session_caps.get('resume'), Mapping):
                    raise AcpTransportError('resume_unsupported')
                # Set identity before RPC: resume implementations may notify
                # metadata while responding. These are still exact-session rows.
                self.session_id = resume_session
                setup = self.transport.request('session/resume', {'sessionId': resume_session,
                    'cwd': self.cwd, 'mcpServers': []}, timeout=self.timeout)
                if 'sessionId' in setup and setup['sessionId'] != resume_session:
                    raise AcpTransportError('session_mismatch')
            else:
                self._creating = True
                setup = self.transport.request('session/new', {'cwd': self.cwd, 'mcpServers': []}, timeout=self.timeout)
                with self._lock:
                    self.session_id = native_identity(setup.get('sessionId'))
                    if not self.session_id: raise AcpTransportError('invalid_session_identity')
                    self._creating = False
                    early, self._early_updates = self._early_updates, []
                    for update in early: self._notification(update)
            model = setup.get('models')
            if isinstance(model, Mapping) and isinstance(model.get('currentModelId'), str):
                self._emit('session_metadata', runtime_protocol='acp', model=model['currentModelId'][:256])
            # Private row is intercepted by Supervisor and never passed to spool.
            self._emit('fleet_acp_session', session_id=self.session_id, cwd=self.cwd,
                resume_supported=isinstance(session_caps.get('resume'), Mapping))
            self._configure(setup, 'model', requested_model)
            self._configure(setup, 'mode', requested_mode)
            self._emit('session_metadata', interactive_questions=bool(self.control_dir), runtime_protocol='acp')
            self._state('running', 'user_prompt_submitted')
            response = self.transport.request('session/prompt', {'sessionId': self.session_id,
                'prompt': [{'type': 'text', 'text': text}]}, timeout=1800)
            stop = response.get('stopReason')
            if stop not in {'end_turn', 'max_tokens', 'max_turn_requests', 'refusal', 'cancelled'}:
                raise AcpTransportError('invalid_stop_reason')
            if stop == 'cancelled': raise AcpTransportError('cancelled')
            if stop != 'end_turn': raise AcpTransportError('turn_incomplete')
            self._settle()
            with self._lock: self._finish_message()
            self._state('completed', 'session_ended')
            return 0
        except AcpTransportError as exc:
            if exc.code == 'cancelled': self.cancel()
            self._state('stopped' if exc.code == 'cancelled' else 'failed',
                        'agent_stopped' if exc.code == 'cancelled' else 'source_unavailable')
            self._emit('fleet_acp_error', code=exc.code)
            return 130 if exc.code == 'cancelled' else 124 if exc.code == 'request_timeout' else 125
        finally:
            self.transport.close()

    def _settle(self):
        deadline = time.monotonic() + self.background_timeout
        while True:
            if self.should_abort(): raise AcpTransportError('cancelled')
            if self.transport.error: raise AcpTransportError(self.transport.error)
            with self._lock:
                busy = bool(self.background_pending or self.permissions or self.transport.client_inflight)
                quiet = time.monotonic() - self.last_activity >= self.settle_gap
            if not busy and quiet: return
            if time.monotonic() >= deadline: raise AcpTransportError('background_unsettled')
            time.sleep(.02)

    def cancel(self):
        if self._cancelled or not self.session_id: return
        self._cancelled = True
        try: self.transport.notify('session/cancel', {'sessionId': self.session_id})
        except AcpTransportError: pass
