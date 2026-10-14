"""Private Muse credentials, account settings and durable mutation receipts."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import tempfile
import uuid
from contextlib import closing, contextmanager

from cryptography.fernet import Fernet
from hub.integrations.muse.client import MuseError


class MuseRepository:
    # A first request can perform two bounded auth calls plus one gateway call
    # (20 seconds each). Competing callers wait for that receipt; stale claims
    # become unknown after the full upstream bound and are never replayed.
    _DISPATCH_WAIT_SECONDS = 65
    _DISPATCH_STALE_SECONDS = 90
    _ACCOUNT_LEASE_SECONDS = 300
    _ACCOUNT_CHANGE_WAIT_SECONDS = 300
    _LOCK_POLL_SECONDS = 0.05
    _DISPATCH_POLL_SECONDS = 0.05
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'muse.db'
        self._init_lock = threading.Lock()
        self._cipher = None

    @contextmanager
    def _connect(self):
        with self._init_lock:
            if self._cipher is None:
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                if self.directory.is_symlink() or self.directory.stat().st_mode & 0o077:
                    raise MuseError('muse_private_store', 'Muse 状态目录必须仅当前用户可访问（0700）。', 503)
                key_path = self.directory / 'credentials.key'
                if not key_path.exists():
                    # Publish a complete key atomically without replacing a
                    # concurrent worker's key. O_EXCL on the final filename
                    # alone allows another process to observe an empty file.
                    fd, staging = tempfile.mkstemp(prefix='.key-', dir=self.directory)
                    try:
                        with os.fdopen(fd, 'wb') as stream:
                            stream.write(Fernet.generate_key())
                            stream.flush()
                            os.fsync(stream.fileno())
                        try:
                            os.link(staging, key_path)
                        except FileExistsError:
                            pass
                    finally:
                        os.unlink(staging)
                if key_path.is_symlink() or key_path.stat().st_mode & 0o077:
                    raise MuseError('muse_private_store', 'Muse 密钥文件权限不正确。', 503)
                if self.path.is_symlink():
                    raise MuseError('muse_private_store', 'Muse 数据库路径不正确。', 503)
                with os.fdopen(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600), 'ab'):
                    pass
                os.chmod(self.path, 0o600)
                cipher = Fernet(key_path.read_bytes())
                with closing(sqlite3.connect(self.path, timeout=15)) as conn, conn:
                    self._init_schema(conn)
                self._cipher = cipher
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _init_schema(conn):
        conn.execute('BEGIN IMMEDIATE')
        account_columns = {row[1] for row in conn.execute('PRAGMA table_info(accounts)')}
        if account_columns and 'account_id' not in account_columns:
            conn.execute('''
                CREATE TABLE accounts_v2 (
                    owner TEXT NOT NULL, account_id TEXT NOT NULL, label TEXT NOT NULL,
                    login BLOB NOT NULL, revision INTEGER NOT NULL, settings TEXT NOT NULL,
                    settings_revision INTEGER NOT NULL, updated REAL NOT NULL,
                    PRIMARY KEY(owner, account_id));
            ''')
            conn.execute("INSERT INTO accounts_v2(owner, account_id, label, login, revision, settings, settings_revision, updated) SELECT owner, 'default', 'Muse', login, revision, settings, 1, updated FROM accounts")
            conn.execute('DROP TABLE accounts')
            conn.execute('ALTER TABLE accounts_v2 RENAME TO accounts')
        elif not account_columns:
            conn.execute('''CREATE TABLE accounts (
                owner TEXT NOT NULL, account_id TEXT NOT NULL, label TEXT NOT NULL,
                login BLOB NOT NULL, revision INTEGER NOT NULL, settings TEXT NOT NULL,
                settings_revision INTEGER NOT NULL, updated REAL NOT NULL,
                PRIMARY KEY(owner, account_id))''')
        receipt_columns = {row[1] for row in conn.execute('PRAGMA table_info(receipts)')}
        if receipt_columns and 'account_id' not in receipt_columns:
            conn.execute('''
                CREATE TABLE receipts_v2 (
                    owner TEXT NOT NULL, account_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, action TEXT NOT NULL, state TEXT NOT NULL,
                    result TEXT NOT NULL, created REAL NOT NULL, connection_revision INTEGER NOT NULL,
                    PRIMARY KEY(owner, account_id, request_id));
            ''')
            # Preserve receipts for users who disconnected before this
            # migration.  A missing account is deliberately bound to revision
            # zero, making the old receipt auditable but ineligible for replay
            # under a later login.
            conn.execute("INSERT INTO receipts_v2(owner, account_id, request_id, fingerprint, action, state, result, created, connection_revision) SELECT r.owner, 'default', r.request_id, r.fingerprint, r.action, r.state, r.result, r.created, COALESCE(a.revision, 0) FROM receipts r LEFT JOIN accounts a ON a.owner=r.owner AND a.account_id='default'")
            conn.execute('DROP TABLE receipts')
            conn.execute('ALTER TABLE receipts_v2 RENAME TO receipts')
        elif not receipt_columns:
            conn.execute('''CREATE TABLE receipts (
                owner TEXT NOT NULL, account_id TEXT NOT NULL, request_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, action TEXT NOT NULL, state TEXT NOT NULL,
                result TEXT NOT NULL, created REAL NOT NULL, connection_revision INTEGER NOT NULL,
                PRIMARY KEY(owner, account_id, request_id))''')
        elif 'connection_revision' not in receipt_columns:
            # Existing receipts cannot be tied reliably to one of several
            # subsequently imported logins. Mark them stale so they cannot
            # suppress or impersonate operations for the current login.
            conn.execute('ALTER TABLE receipts ADD COLUMN connection_revision INTEGER NOT NULL DEFAULT 0')
        conn.execute('''CREATE TABLE IF NOT EXISTS account_leases (
            owner TEXT NOT NULL, account_id TEXT NOT NULL, token TEXT NOT NULL,
            revision INTEGER NOT NULL, expires REAL NOT NULL, PRIMARY KEY(owner,account_id,token))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS account_changes (
            owner TEXT NOT NULL, account_id TEXT NOT NULL, token TEXT NOT NULL,
            expires REAL NOT NULL, PRIMARY KEY(owner,account_id))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS account_generations (
            owner TEXT NOT NULL, account_id TEXT NOT NULL, revision INTEGER NOT NULL,
            PRIMARY KEY(owner, account_id))''')

    @contextmanager
    def account_lease(self, owner, account_id=None, expected_revision=None):
        token = uuid.uuid4().hex
        deadline = time.monotonic() + self._ACCOUNT_CHANGE_WAIT_SECONDS
        acquired = None
        while acquired is None:
            with self._connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                now = time.time()
                conn.execute('DELETE FROM account_leases WHERE expires<=?', (now,))
                conn.execute('DELETE FROM account_changes WHERE expires<=?', (now,))
                if account_id is None:
                    row = conn.execute("SELECT * FROM accounts WHERE owner=? ORDER BY account_id='default' DESC, updated DESC LIMIT 1", (owner,)).fetchone()
                else:
                    row = conn.execute('SELECT * FROM accounts WHERE owner=? AND account_id=?', (owner, account_id)).fetchone()
                if row is None:
                    raise MuseError('muse_disconnected', '请先导入 Muse 登录态。', 409)
                if expected_revision is not None and str(row['revision']) != str(expected_revision):
                    raise MuseError('muse_connection_changed', 'Muse 登录态已切换，请重新加载当前账号。', 409)
                account_id = row['account_id']
                changing = conn.execute('SELECT 1 FROM account_changes WHERE owner=? AND account_id=?',
                                         (owner, account_id)).fetchone()
                if not changing:
                    conn.execute('INSERT INTO account_leases VALUES (?,?,?,?,?)',
                                 (owner, account_id, token, row['revision'], now + self._ACCOUNT_LEASE_SECONDS))
                    acquired = self._public(row, self._cipher)
            if acquired is None:
                if time.monotonic() >= deadline:
                    raise MuseError('muse_account_busy', 'Muse 账号正在切换，请稍后重试。', 409)
                time.sleep(self._LOCK_POLL_SECONDS)
        try:
            yield acquired
        finally:
            with self._connect() as conn:
                conn.execute('DELETE FROM account_leases WHERE owner=? AND account_id=? AND token=?',
                             (owner, acquired['account_id'], token))

    @contextmanager
    def account_change(self, owner, account_id):
        token = uuid.uuid4().hex
        deadline = time.monotonic() + self._ACCOUNT_CHANGE_WAIT_SECONDS
        owns_change = False
        while True:
            with self._connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                now = time.time()
                conn.execute('DELETE FROM account_leases WHERE expires<=?', (now,))
                conn.execute('DELETE FROM account_changes WHERE expires<=?', (now,))
                changing = conn.execute('SELECT token FROM account_changes WHERE owner=? AND account_id=?',
                                        (owner, account_id)).fetchone()
                if not owns_change and not changing:
                    conn.execute('INSERT INTO account_changes VALUES (?,?,?,?)',
                                 (owner, account_id, token, now + self._ACCOUNT_CHANGE_WAIT_SECONDS + self._ACCOUNT_LEASE_SECONDS))
                    owns_change = True
                if owns_change:
                    leases = conn.execute('SELECT 1 FROM account_leases WHERE owner=? AND account_id=? LIMIT 1',
                                          (owner, account_id)).fetchone()
                    if not leases:
                        break
            if time.monotonic() >= deadline:
                with self._connect() as conn:
                    conn.execute('DELETE FROM account_changes WHERE owner=? AND account_id=? AND token=?',
                                 (owner, account_id, token))
                raise MuseError('muse_account_busy', 'Muse 账号仍有操作进行，请稍后重试。', 409)
            time.sleep(self._LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            with self._connect() as conn:
                conn.execute('DELETE FROM account_changes WHERE owner=? AND account_id=? AND token=?',
                             (owner, account_id, token))

    @staticmethod
    def _public(row, cipher):
        settings = json.loads(row['settings'])
        settings.setdefault('auto_approve_sessions', [])
        settings.setdefault('auto_approve_all', False)
        return {'account_id': row['account_id'], 'label': row['label'],
                'cookies': json.loads(cipher.decrypt(row['login'])), 'revision': row['revision'],
                'settings': settings, 'settings_revision': row['settings_revision'],
                'updated_at': row['updated']}

    def list_accounts(self, owner):
        with self._connect() as conn:
            rows = conn.execute('''SELECT account_id,label,revision,settings_revision,updated
                                   FROM accounts WHERE owner=? ORDER BY updated DESC, account_id''', (owner,)).fetchall()
        return [{'account_id': row['account_id'], 'label': row['label'],
                 'revision': row['revision'], 'settings_revision': row['settings_revision'],
                 'updated_at': row['updated']} for row in rows]

    def account(self, owner, account_id=None):
        with self._connect() as conn:
            if account_id is None:
                row = conn.execute("SELECT * FROM accounts WHERE owner=? ORDER BY account_id='default' DESC, updated DESC LIMIT 1", (owner,)).fetchone()
            else:
                row = conn.execute('SELECT * FROM accounts WHERE owner=? AND account_id=?', (owner, account_id)).fetchone()
        if row is None:
            return None
        return self._public(row, self._cipher)

    def import_login(self, owner, cookies, account_id='default', label='Muse'):
        account_id = account_id or 'default'
        label = label if isinstance(label, str) and label.strip() else 'Muse'
        label = label.strip()[:80]
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            encrypted = self._cipher.encrypt(json.dumps(cookies).encode())
            current = conn.execute('SELECT revision FROM account_generations WHERE owner=? AND account_id=?',
                                   (owner, account_id)).fetchone()
            account = conn.execute('SELECT revision FROM accounts WHERE owner=? AND account_id=?',
                                   (owner, account_id)).fetchone()
            previous = max((current['revision'] if current else 0), (account['revision'] if account else 0))
            revision = previous + 1
            conn.execute('''INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(owner, account_id) DO UPDATE SET
                label=excluded.label, login=excluded.login, revision=excluded.revision,
                settings=excluded.settings, settings_revision=excluded.settings_revision, updated=excluded.updated''',
                         (owner, account_id, label, encrypted, revision,
                          json.dumps({'auto_approve_sessions': [], 'auto_approve_all': False}), 1, time.time()))
            conn.execute('''INSERT INTO account_generations(owner, account_id, revision) VALUES (?,?,?)
                            ON CONFLICT(owner, account_id) DO UPDATE SET revision=excluded.revision''',
                         (owner, account_id, revision))

    def disconnect(self, owner, account_id=None, expected_connection_revision=None):
        account = self.account(owner, account_id)
        with self._connect() as conn:
            if account is not None:
                revision = (expected_connection_revision if expected_connection_revision is not None
                            else account['revision'])
                deleted = conn.execute('DELETE FROM accounts WHERE owner=? AND account_id=? AND revision=?',
                                       (owner, account['account_id'], revision)).rowcount
                if not deleted:
                    raise MuseError('muse_connection_changed', 'Muse 登录态已切换，请重新加载当前账号。', 409)
                conn.execute('''INSERT INTO account_generations(owner, account_id, revision) VALUES (?,?,?)
                                ON CONFLICT(owner, account_id) DO UPDATE SET revision=MAX(account_generations.revision, excluded.revision)''',
                             (owner, account['account_id'], revision))

    def settings(self, owner, value, account_id=None, expected_revision=None, expected_connection_revision=None):
        account = self.account(owner, account_id)
        if account is None:
            return None
        account_id = account['account_id']
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT settings_revision,revision,settings FROM accounts WHERE owner=? AND account_id=?',
                               (owner, account_id)).fetchone()
            if row is None:
                raise MuseError('muse_disconnected', '请先导入 Muse 登录态。', 409)
            if (expected_connection_revision is not None and
                    str(row['revision']) != str(expected_connection_revision)):
                raise MuseError('muse_connection_changed', 'Muse 登录态已切换，请重新加载当前账号。', 409)
            if expected_revision is not None and row['settings_revision'] != expected_revision:
                    raise MuseError('muse_settings_changed', '自动审批设置已被其他窗口修改，请重新加载。', 409)
            current = json.loads(row['settings'])
            if not isinstance(current, dict):
                raise MuseError('muse_settings_invalid', 'Muse 自动审批设置格式不合法。', 503)
            current.update(value)
            conn.execute('UPDATE accounts SET settings=?, settings_revision=settings_revision+1, updated=? WHERE owner=? AND account_id=?',
                         (json.dumps(current), time.time(), owner, account_id))
        return self.account(owner, account['account_id'])

    def update_session_setting(self, owner, account_id, session_id, enabled, expected_revision=None,
                               expected_connection_revision=None):
        account = self.account(owner, account_id)
        if account is None:
            return None
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT settings_revision,revision,settings FROM accounts WHERE owner=? AND account_id=?',
                               (owner, account['account_id'])).fetchone()
            if row is None:
                raise MuseError('muse_disconnected', '请先导入 Muse 登录态。', 409)
            if (expected_connection_revision is not None and
                    str(row['revision']) != str(expected_connection_revision)):
                raise MuseError('muse_connection_changed', 'Muse 登录态已切换，请重新加载当前账号。', 409)
            if expected_revision is not None and row['settings_revision'] != expected_revision:
                raise MuseError('muse_settings_changed', '自动审批设置已被其他窗口修改，请重新加载。', 409)
            settings = json.loads(row['settings'])
            sessions = list(settings.get('auto_approve_sessions', []))
            if enabled and session_id not in sessions:
                sessions.append(session_id)
            elif not enabled:
                sessions = [value for value in sessions if value != session_id]
            settings['auto_approve_sessions'] = sessions
            conn.execute('UPDATE accounts SET settings=?, settings_revision=settings_revision+1, updated=? WHERE owner=? AND account_id=?',
                         (json.dumps(settings), time.time(), owner, account['account_id']))
        return self.account(owner, account['account_id'])

    def enabled_accounts(self):
        if not self.path.exists():
            return []
        with self._connect() as conn:
            return [{'owner': r['owner'], 'account_id': r['account_id']}
                    for r in conn.execute('SELECT owner,account_id,settings FROM accounts')
                    if (settings := json.loads(r['settings'])).get('auto_approve_all') is True or
                    settings.get('auto_approve_sessions')]

    def claim(self, owner, request_id, action, data, account_id=None, expected_connection_revision=None,
              required_auto_session=None, expected_settings_revision=None, required_auto_approve_all=False):
        fingerprint = hashlib.sha256(json.dumps([action, data], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        wait_key = None
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if account_id is None:
                row = conn.execute("SELECT * FROM accounts WHERE owner=? ORDER BY account_id='default' DESC, updated DESC LIMIT 1", (owner,)).fetchone()
            else:
                row = conn.execute('SELECT * FROM accounts WHERE owner=? AND account_id=?', (owner, account_id)).fetchone()
            if row is None:
                raise MuseError('muse_disconnected', '请先导入 Muse 登录态。', 409)
            account_id = row['account_id']
            if expected_connection_revision is not None and str(row['revision']) != str(expected_connection_revision):
                raise MuseError('muse_connection_changed', 'Muse 登录态已切换，请重新加载当前账号。', 409)
            if required_auto_session is not None or required_auto_approve_all:
                settings = json.loads(row['settings'])
                enabled = settings.get('auto_approve_sessions', [])
                allowed = (settings.get('auto_approve_all') is True if required_auto_approve_all else
                           required_auto_session in enabled)
                if row['settings_revision'] != expected_settings_revision or not allowed:
                    raise MuseError('muse_approval_revoked', '自动审批设置已更改，请重新加载权限请求。', 409)
            connection_revision = row['revision']
            row = conn.execute('SELECT * FROM receipts WHERE owner=? AND account_id=? AND request_id=?', (owner, account_id, request_id)).fetchone()
            if row:
                if row['connection_revision'] != connection_revision:
                    raise MuseError('muse_request_conflict', '此请求标识属于另一份 Muse 登录态。', 409)
                if row['fingerprint'] != fingerprint:
                    raise MuseError('muse_request_conflict', '此请求标识已用于另一项操作。', 409)
                if row['state'] == 'retryable':
                    conn.execute('UPDATE receipts SET state=?,result=?,created=? WHERE owner=? AND account_id=? AND request_id=?',
                                 ('dispatching', '{}', time.time(), owner, account_id, request_id))
                    return None
                if row['state'] == 'dispatching':
                    if time.time() - row['created'] > self._DISPATCH_STALE_SECONDS:
                        result = json.dumps({'error': 'muse_dispatch_unknown'})
                        conn.execute("UPDATE receipts SET state='unknown',result=? WHERE owner=? AND account_id=? AND request_id=? AND state='dispatching'",
                                     (result, owner, account_id, request_id))
                        return {'request_id': request_id, 'state': 'unknown', 'result': json.loads(result)}
                    wait_key = (owner, account_id, request_id)
                else:
                    return {'request_id': request_id, 'state': row['state'], 'result': json.loads(row['result'])}
            else:
                conn.execute('INSERT INTO receipts(owner,account_id,request_id,fingerprint,action,state,result,created,connection_revision) VALUES (?,?,?,?,?,?,?,?,?)',
                             (owner, account_id, request_id, fingerprint, action, 'dispatching', '{}', time.time(), connection_revision))
                return None
        if wait_key is not None:
            deadline = time.monotonic() + self._DISPATCH_WAIT_SECONDS
            while time.monotonic() < deadline:
                with self._connect() as conn:
                    row = conn.execute('SELECT state,result FROM receipts WHERE owner=? AND account_id=? AND request_id=?', wait_key).fetchone()
                if row is None:
                    return {'request_id': request_id, 'state': 'unknown', 'result': {'error': 'muse_dispatch_lost'}}
                if row['state'] != 'dispatching':
                    return {'request_id': request_id, 'state': row['state'], 'result': json.loads(row['result'])}
                time.sleep(self._DISPATCH_POLL_SECONDS)
            return {'request_id': request_id, 'state': 'dispatching', 'result': {}}

    def finish(self, owner, request_id, state, result, account_id=None, connection_revision=None):
        with self._connect() as conn:
            if connection_revision is None:
                conn.execute('UPDATE receipts SET state=?,result=? WHERE owner=? AND account_id=? AND request_id=? AND state=?',
                             (state, json.dumps(result), owner, account_id, request_id, 'dispatching'))
            else:
                conn.execute('UPDATE receipts SET state=?,result=? WHERE owner=? AND account_id=? AND request_id=? AND connection_revision=? AND state=?',
                             (state, json.dumps(result), owner, account_id, request_id, connection_revision, 'dispatching'))
            row = conn.execute('SELECT state,result FROM receipts WHERE owner=? AND account_id=? AND request_id=?',
                               (owner, account_id, request_id)).fetchone()
        if row is None:
            return {'request_id': request_id, 'state': 'unknown', 'result': {'error': 'muse_dispatch_lost'}}
        return {'request_id': request_id, 'state': row['state'], 'result': json.loads(row['result'])}

    def audit(self, owner, account_id=None):
        account = self.account(owner, account_id)
        if account is None:
            return []
        with self._connect() as conn:
            conn.execute("UPDATE receipts SET state='unknown',result=? WHERE owner=? AND account_id=? AND state='dispatching' AND created<?",
                         (json.dumps({'error': 'muse_dispatch_unknown'}), owner, account['account_id'],
                          time.time() - self._DISPATCH_STALE_SECONDS))
            rows = conn.execute('SELECT request_id,action,state,result,created FROM receipts WHERE owner=? AND account_id=? ORDER BY created DESC LIMIT 50',
                                (owner, account['account_id'])).fetchall()
        return [{**dict(r), 'result': json.loads(r['result'])} for r in rows]
