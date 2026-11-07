"""Persistent device-login progress, independent of Hub command receipts."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

from fleet_core.plugins import PluginError

ACTIVE = ('starting', 'waiting_authorization', 'authorizing')


class LoginStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS logins (
                    login_id TEXT PRIMARY KEY, context_id TEXT NOT NULL, hostname TEXT NOT NULL,
                    state TEXT NOT NULL, value TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_active_login ON logins(context_id)
                    WHERE state IN ('starting','waiting_authorization','authorizing');
            ''')

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def get(self, login_id):
        with closing(self.connect()) as conn:
            row = conn.execute('SELECT * FROM logins WHERE login_id=?', (login_id,)).fetchone()
            if not row:
                raise PluginError('login_not_found')
            return {**json.loads(row['value']), 'login_id': row['login_id'],
                    'context_id': row['context_id'], 'hostname': row['hostname'],
                    'state': row['state'], 'cancel_requested': bool(row['cancel_requested']),
                    'created_at': row['created_at'], 'updated_at': row['updated_at']}

    def create(self, login_id, context, hostname):
        try:
            with closing(self.connect()) as conn:
                now = time.time()
                conn.execute('INSERT INTO logins VALUES(?,?,?,?,?,0,?,?)',
                             (login_id, context, hostname, 'starting', '{}', now, now))
        except sqlite3.IntegrityError:
            try:
                row = self.get(login_id)
            except PluginError:
                raise PluginError('context_busy') from None
            if row['context_id'] != context or row['hostname'] != hostname:
                raise PluginError('login_conflict') from None
            return row, False
        return self.get(login_id), True

    def active(self, context):
        with closing(self.connect()) as conn:
            return conn.execute("SELECT login_id FROM logins WHERE context_id=? AND state IN ('starting','waiting_authorization','authorizing')",
                                (context,)).fetchone() is not None

    def update(self, login_id, state, **values):
        with closing(self.connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT state,value FROM logins WHERE login_id=?', (login_id,)).fetchone()
            if row and row['state'] in ACTIVE:
                value = {**json.loads(row['value']), **values}
                conn.execute('UPDATE logins SET state=?,value=?,updated_at=? WHERE login_id=?',
                             (state, json.dumps(value), time.time(), login_id))
        return self.get(login_id)

    def cancel(self, login_id):
        with closing(self.connect()) as conn:
            conn.execute('UPDATE logins SET cancel_requested=1 WHERE login_id=?', (login_id,))
        return self.get(login_id)

    def recover(self):
        # A new runtime does not own previous workers. Their parent pipe will
        # close and stop them; never replay their authentication side effects.
        with closing(self.connect()) as conn:
            conn.execute("UPDATE logins SET state='unknown',value=?,updated_at=? "
                         "WHERE state IN ('starting','waiting_authorization','authorizing')",
                         (json.dumps({'error_code': 'node_restarted'}), time.time()))
