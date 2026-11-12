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

from fleet_core.process.local_process import run as run_local
from fleet_core.web_agent_contract import MAX_PROMPT, MuseBridgeError, _identity

MAX_MESSAGES = 100
MAX_MESSAGE_TEXT = 12288
MAX_OUTPUT = 2 * 1024 * 1024
TERMINAL = frozenset({'completed', 'failed'})
DEFAULT_STATE = Path.home() / '.local/share/agent-fleet/muse-bridge/tasks.db'


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
        from fleet_core.locking import LockBusy, OwnedLock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = OwnedLock(Path(str(self.path) + '.lock'), 'muse-bridge')
        try:
            lock.acquire()
        except LockBusy as exc:
            raise MuseBridgeError('bridge_busy') from exc
        try:
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
            lock.release()

    def _snapshot(self, target_id=None):
        return _snapshot(self.transport.request('snapshot', **(
            {'target_id': target_id} if target_id else {})))

    def initialize(self) -> str:
        """Explicitly provision this account journal's immutable local identity."""
        with self._store() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS journal_identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), identity TEXT NOT NULL)')
            conn.execute('INSERT OR IGNORE INTO journal_identity VALUES(1,?)', (secrets.token_hex(32),))
            conn.commit()
        identity = self.journal_identity()
        if identity is None:
            raise MuseBridgeError('journal_identity_invalid')
        return identity

    def journal_identity(self) -> str | None:
        """Read without creating a journal; a replacement file is a new receiver."""
        if not self.path.exists():
            return None
        conn = sqlite3.connect(self.path.absolute().as_uri() + '?mode=ro', uri=True, timeout=2)
        try:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='journal_identity' AND type='table'").fetchone() is None:
                return None
            rows = conn.execute('SELECT singleton,identity FROM journal_identity LIMIT 2').fetchall()
            if len(rows) != 1 or rows[0][0] != 1 or not isinstance(rows[0][1], str) or not re.fullmatch('[0-9a-f]{64}', rows[0][1]):
                raise MuseBridgeError('journal_identity_invalid')
            return rows[0][1]
        except sqlite3.Error as exc:
            raise MuseBridgeError('journal_identity_unavailable') from exc
        finally:
            conn.close()

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

    def lookup(self, task_id: str, prompt: str, *, scope: str = 'local') -> dict[str, Any] | None:
        """Read the original journal under the existing cross-platform lock."""
        from fleet_core.locking import try_lock, unlock_and_close

        _identity(task_id)
        _identity(scope, 'scope_invalid')
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
            raise MuseBridgeError('prompt_invalid')
        if not self.path.exists():
            return None
        handle = None
        conn = None
        try:
            handle = open(str(self.path) + '.lock', 'r+b')
            if not try_lock(handle):
                handle.close()
                handle = None
                raise MuseBridgeError('journal_lookup_unavailable')
            conn = sqlite3.connect(self.path.absolute().as_uri() + '?mode=ro', uri=True, timeout=2)
            conn.row_factory = sqlite3.Row
            row = conn.execute('SELECT task_id,scope,prompt_hash,marker,state,reply,error FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            if row is None:
                return None
            if (row['scope'], row['prompt_hash']) != (scope, _digest(prompt)):
                raise MuseBridgeError('task_conflict')
            return self._public(row)
        except (OSError, sqlite3.Error) as exc:
            raise MuseBridgeError('journal_lookup_unavailable') from exc
        finally:
            if conn is not None:
                conn.close()
            if handle is not None:
                unlock_and_close(handle)

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
