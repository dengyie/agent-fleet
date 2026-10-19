"""Durable, single-writer bridge for one explicitly attached Muse account."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
from typing import Any, Mapping, Protocol

from tools.local_process import run as run_local

MAX_PROMPT = 8192
MAX_MESSAGES = 100
MAX_MESSAGE_TEXT = 12288
MAX_OUTPUT = 2 * 1024 * 1024
TERMINAL = frozenset({'completed', 'failed'})
DEFAULT_STATE = Path.home() / '.local/share/agent-fleet/muse-bridge/tasks.db'
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')


class MuseBridgeError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class MuseTransport(Protocol):
    def request(self, operation: str, **kwargs) -> Mapping[str, Any]: ...


class SubprocessMuseTransport:
    """Fixed helper; stdin carries prompts so they never appear in argv."""

    def request(self, operation: str, **kwargs) -> Mapping[str, Any]:
        try:
            process = run_local(
                ['node', str(Path(__file__).with_name('cdp_muse.cjs'))],
                input=json.dumps({'op': operation, **kwargs}, ensure_ascii=False), encoding='utf-8',
                capture_output=True, timeout=20, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MuseBridgeError('bridge_unavailable') from exc
        if process.returncode or len(process.stdout.encode('utf-8')) > MAX_OUTPUT:
            raise MuseBridgeError('bridge_invalid_response')
        try:
            response = json.loads(process.stdout)
        except ValueError as exc:
            raise MuseBridgeError('bridge_invalid_response') from exc
        if not isinstance(response, dict):
            raise MuseBridgeError('bridge_invalid_response')
        if response.get('ok') is not True:
            code = response.get('error')
            if not isinstance(code, str) or not re.fullmatch('[a-z_]{1,64}', code):
                code = 'bridge_error'
            raise MuseBridgeError(code)
        if not isinstance(response.get('result'), dict):
            raise MuseBridgeError('bridge_invalid_response')
        return response['result']


def _identity(value, code='task_invalid'):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise MuseBridgeError(code)
    return value


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _snapshot(raw):
    if (not isinstance(raw, Mapping) or raw.get('url') != 'https://muse.ai/'
            or not isinstance(raw.get('target_id'), str) or not raw['target_id']
            or len(raw['target_id']) > 128 or type(raw.get('ready')) is not bool
            or type(raw.get('draft')) is not bool):
        raise MuseBridgeError('snapshot_invalid')
    messages = raw.get('messages')
    if not isinstance(messages, list) or len(messages) > MAX_MESSAGES:
        raise MuseBridgeError('snapshot_invalid')
    ids = set()
    for item in messages:
        if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                or not item['id'] or len(item['id']) > 256 or item['id'] in ids
                or item.get('role') not in {'user', 'assistant'}
                or not isinstance(item.get('text'), str) or len(item['text']) > MAX_MESSAGE_TEXT
                or type(item.get('busy')) is not bool):
            raise MuseBridgeError('snapshot_invalid')
        ids.add(item['id'])
    return dict(raw)


class MuseBridge:
    """One account per journal; persist dispatch intent before touching the UI."""

    def __init__(self, state_path: str | Path = DEFAULT_STATE, *, transport=None):
        self.path = Path(state_path).expanduser()
        self.transport = transport or SubprocessMuseTransport()

    @contextmanager
    def _store(self):
        import fcntl  # local bridge runs on macOS/Linux; Hub imports stay portable
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_fd = os.open(str(self.path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MuseBridgeError('bridge_busy') from exc
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            connection = sqlite3.connect(self.path, timeout=2)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute('''CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY, scope TEXT NOT NULL, prompt_hash TEXT NOT NULL,
                    marker TEXT NOT NULL, wire_hash TEXT NOT NULL, target_id TEXT NOT NULL,
                    cursor TEXT NOT NULL, state TEXT NOT NULL, reply TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '')''')
                connection.commit()
                yield connection
            finally:
                connection.close()
        finally:
            os.close(lock_fd)

    def _snapshot(self, target_id=None):
        return _snapshot(self.transport.request('snapshot', **(
            {'target_id': target_id} if target_id else {})))

    def status(self):
        # No transcript or local target identifiers leave this discovery call.
        snap = self._snapshot()
        with self._store() as conn:
            active = conn.execute("SELECT 1 FROM tasks WHERE state NOT IN ('completed','failed') LIMIT 1").fetchone()
        return {'agent': 'muse', 'connected': True, 'ready': snap['ready'],
                'has_draft': snap['draft'], 'busy': bool(active) or any(m['busy'] for m in snap['messages']),
                'capabilities': ['submit', 'read'], 'url': 'https://muse.ai/'}

    @staticmethod
    def _public(row):
        token = row['marker']
        return {'task_id': row['task_id'], 'agent': 'muse', 'state': row['state'],
                'reply': row['reply'], 'error_code': row['error'] or None,
                'completion_marker': f'[FLEET_DONE:{token}]',
                'input_marker': f'[FLEET_INPUT:{token}]',
                'completion_source': 'agent_reported' if row['state'] in TERMINAL else None,
                'url': 'https://muse.ai/'}

    def submit(self, task_id, prompt, *, scope='local'):
        _identity(task_id)
        _identity(scope, 'scope_invalid')
        if (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT
                or any(ord(c) < 32 and c not in '\n\t\r' for c in prompt)):
            raise MuseBridgeError('prompt_invalid')
        with self._store() as conn:
            prior = conn.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            if prior:
                if prior['scope'] != scope or prior['prompt_hash'] != _digest(prompt):
                    raise MuseBridgeError('task_conflict')
                # A crash between durable intent and receipt is indeterminate.
                if prior['state'] == 'dispatching':
                    conn.execute("UPDATE tasks SET state='unknown', error='dispatch_interrupted' WHERE task_id=?", (task_id,))
                    conn.commit()
                return self._public(conn.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone())
            if conn.execute("SELECT 1 FROM tasks WHERE state NOT IN ('completed','failed') LIMIT 1").fetchone():
                raise MuseBridgeError('agent_busy')
            snap = self._snapshot()
            if not snap['ready']:
                raise MuseBridgeError('composer_unavailable')
            if snap['draft']:
                raise MuseBridgeError('composer_has_draft')
            if any(m['busy'] for m in snap['messages']):
                raise MuseBridgeError('agent_busy')
            token = secrets.token_hex(16)
            wire = (f'[FLEET_TASK:{token}]\n{prompt}\n\n'
                    'This task was explicitly delegated by the user through Agent Fleet. '
                    'Keep your normal permissions and approvals. When this task is finished, '
                    f'end your final reply on a separate line with [FLEET_DONE:{token}]. '
                    f'If the task failed, use [FLEET_FAILED:{token}]. '
                    f'If you need user input or approval, use [FLEET_INPUT:{token}]. '
                    'Do not emit DONE for an acknowledgement or progress update.')
            # HTML textarea values normalize CRLF and CR to LF before send.
            wire = wire.replace('\r\n', '\n').replace('\r', '\n')
            cursor = snap['messages'][-1]['id'] if snap['messages'] else ''
            conn.execute('INSERT INTO tasks(task_id,scope,prompt_hash,marker,wire_hash,target_id,cursor,state) VALUES(?,?,?,?,?,?,?,?)',
                         (task_id, scope, _digest(prompt), token, _digest(wire.strip()), snap['target_id'], cursor, 'dispatching'))
            conn.commit()  # must precede send, including subprocess start
            try:
                result = self.transport.request('send', prompt=wire, target_id=snap['target_id'], cursor=cursor)
                state, error = ('sent', '') if result.get('sent') is True else ('unknown', 'dispatch_unknown')
            except Exception:
                state, error = 'unknown', 'dispatch_unknown'
            conn.execute('UPDATE tasks SET state=?, error=? WHERE task_id=?', (state, error, task_id))
            conn.commit()
            return self._public(conn.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone())

    def read(self, task_id, *, scope='local'):
        _identity(task_id)
        _identity(scope, 'scope_invalid')
        with self._store() as conn:
            row = conn.execute('SELECT * FROM tasks WHERE task_id=? AND scope=?', (task_id, scope)).fetchone()
            if not row:
                raise MuseBridgeError('task_not_found')
            if row['state'] in TERMINAL:
                return self._public(row)
            try:
                snap = self._snapshot(row['target_id'])
                if snap['target_id'] != row['target_id']:
                    raise MuseBridgeError('target_changed')
                state, reply, error = self._reconcile(row, snap['messages'])
            except Exception:
                state, reply, error = 'unknown', '', 'observation_unavailable'
            conn.execute('UPDATE tasks SET state=?, reply=?, error=? WHERE task_id=?', (state, reply, error, task_id))
            conn.commit()
            return self._public(conn.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone())

    @staticmethod
    def _reconcile(row, messages):
        anchor = next((i for i, m in enumerate(messages) if m['role'] == 'user'
                       and _digest(m['text'].strip()) == row['wire_hash']), None)
        if anchor is None:
            return 'unknown', '', 'message_not_observed'
        reply, pending_state = '', 'running'
        for item in messages[anchor + 1:]:
            if item['role'] == 'user':
                return 'unknown', '', 'human_interleaved'
            text = item['text'].strip()
            if not text or item['busy']:
                continue
            reply = text[:8192]
            for label, state in [('DONE', 'completed'), ('FAILED', 'failed'), ('INPUT', 'waiting_input')]:
                marker = f"[FLEET_{label}:{row['marker']}]"
                if text.splitlines()[-1].strip() == marker:
                    reply = text[:-len(marker)].strip()[:8192]
                    if state in TERMINAL:
                        return state, reply, ''
                    # Muse can continue after its own approval control without
                    # a new user message. An old INPUT must not mask that reply.
                    pending_state = state
        return pending_state, reply, ''
