"""Account directory and command-bound OAuth handoff in the supervisor database.

Long-lived Google credentials never enter this store. The one-time login response
uses AEAD and is discarded on completion/expiry. Account mutation and signed
outbox insertion share one SQLite writer transaction.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import time

from fleet_core.provider_accounts import AUTH_TTL_S, authorization_url
from fleet_core.session.crypto import AeadBox
from hub.infrastructure.sqlite import connect


class ProviderAccountStoreError(RuntimeError):
    def __init__(self, code='provider_accounts_unavailable', status=503):
        self.code, self.status = code, status
        super().__init__(code)


_ACCOUNT_COLUMNS = {
    'account_id': 'TEXT', 'owner_id': 'TEXT', 'machine': 'TEXT', 'name': 'TEXT',
    'provider': 'TEXT', 'revision': 'INTEGER', 'state': 'TEXT', 'enabled': 'INTEGER',
    'login_command_id': 'TEXT', 'authorization_url': 'TEXT', 'auth_expires_s': 'REAL',
    'last_error': 'TEXT', 'created_s': 'REAL', 'updated_s': 'REAL',
}
_OPERATION_COLUMNS = {
    'token_digest': 'TEXT', 'binding_digest': 'TEXT', 'command_id': 'TEXT',
    'account_id': 'TEXT', 'revision': 'INTEGER', 'operation': 'TEXT', 'state': 'TEXT',
    'secret': 'BLOB', 'login_command_id': 'TEXT', 'created_s': 'REAL', 'expires_s': 'REAL',
}
_OPTIONAL = {'login_command_id', 'authorization_url', 'secret'}
_TRANSITIONS = {
    'starting': {'starting', 'waiting', 'verifying', 'ready', 'signed_out', 'disabled', 'failed'},
    'waiting': {'waiting', 'verifying', 'ready', 'signed_out', 'disabled', 'failed'},
    'verifying': {'verifying', 'ready', 'signed_out', 'disabled', 'failed'},
    'ready': {'ready'}, 'signed_out': {'signed_out'}, 'disabled': {'disabled'}, 'failed': {'failed'},
}
_ERROR_CODES = frozenset({
    '', 'login_failed', 'login_expired', 'login_interrupted', 'invalid_authorization_url',
    'invalid_login_response', 'account_busy', 'provider_unavailable', 'provider_account_failed',
    'provider_cleanup_failed', 'provider_authentication_failed', 'login_cancelled',
    'provider_account_unavailable', 'provider_account_disabled', 'provider_account_signed_out',
    'provider_account_busy', 'provider_account_stale', 'provider_account_store',
    'provider_account_logout_unsupported', 'provider_account_owner_mismatch',
    'provider_account_stale_operation', 'provider_account_operation_conflict',
    'provider_account_state_conflict', 'provider_account_not_ready',
})


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True).encode()).hexdigest()


class ProviderAccountRepository:
    def __init__(self, db_path: Path, *, key: bytes | None = None, clock=time.time):
        self.db_path, self.clock = Path(db_path), clock
        self.box = AeadBox(key) if key is not None else None

    def _connect(self):
        return connect(self.db_path, timeout=10)

    @contextmanager
    def _transaction(self):
        connection = None
        try:
            connection = self._connect()
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            connection.commit()
        except ProviderAccountStoreError:
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError):
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise ProviderAccountStoreError() from None
        finally:
            if connection is not None:
                connection.close()

    def init(self):
        with self._transaction() as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='supervisor_commands'").fetchone():
                raise ProviderAccountStoreError()
            conn.execute('CREATE TABLE IF NOT EXISTS supervisor_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            stored = conn.execute("SELECT value FROM supervisor_meta WHERE key='provider_accounts_schema'").fetchone()
            if stored is not None and stored['value'] != '1':
                raise ProviderAccountStoreError('provider_account_schema')
            present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('provider_accounts','provider_account_operations')")}
            if stored is not None and len(present) != 2 or stored is None and present:
                raise ProviderAccountStoreError('provider_account_schema')
            conn.execute("""CREATE TABLE IF NOT EXISTS provider_accounts(
                account_id TEXT NOT NULL PRIMARY KEY,owner_id TEXT NOT NULL,machine TEXT NOT NULL,
                name TEXT NOT NULL,provider TEXT NOT NULL CHECK(provider='agy'),
                revision INTEGER NOT NULL CHECK(revision>=1),
                state TEXT NOT NULL CHECK(state IN ('signed_out','ready','disabled','starting','waiting','verifying','failed')),
                enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),login_command_id TEXT,
                authorization_url TEXT,auth_expires_s REAL NOT NULL,last_error TEXT NOT NULL,
                created_s REAL NOT NULL,updated_s REAL NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS provider_account_operations(
                token_digest TEXT NOT NULL PRIMARY KEY CHECK(length(token_digest)=64),
                binding_digest TEXT NOT NULL CHECK(length(binding_digest)=64),
                command_id TEXT NOT NULL UNIQUE REFERENCES supervisor_commands(command_id),
                account_id TEXT NOT NULL REFERENCES provider_accounts(account_id),
                revision INTEGER NOT NULL CHECK(revision>=1),operation TEXT NOT NULL
                CHECK(operation IN ('create','login','submit_login','cancel_login','enable','disable','logout')),
                state TEXT NOT NULL CHECK(state IN ('signed_out','ready','disabled','starting','waiting','verifying','failed')),
                secret BLOB,login_command_id TEXT,created_s REAL NOT NULL,expires_s REAL NOT NULL)""")
            for table, columns, primary in (
                    ('provider_accounts', _ACCOUNT_COLUMNS, 'account_id'),
                    ('provider_account_operations', _OPERATION_COLUMNS, 'token_digest')):
                actual = {row['name']: row for row in conn.execute('PRAGMA table_info(' + table + ')')}
                if set(actual) != set(columns):
                    raise ProviderAccountStoreError('provider_account_schema')
                for name, kind in columns.items():
                    col = actual[name]
                    if (col['type'].upper() != kind or col['pk'] != int(name == primary)
                            or col['notnull'] != int(name not in _OPTIONAL) or col['dflt_value'] is not None):
                        raise ProviderAccountStoreError('provider_account_schema')
            foreign = {(r['from'], r['table'], r['to']) for r in conn.execute('PRAGMA foreign_key_list(provider_account_operations)')}
            if foreign != {('command_id', 'supervisor_commands', 'command_id'), ('account_id', 'provider_accounts', 'account_id')}:
                raise ProviderAccountStoreError('provider_account_schema')
            unique = [tuple(r['name'] for r in conn.execute('PRAGMA index_info(' + index['name'] + ')'))
                      for index in conn.execute('PRAGMA index_list(provider_account_operations)')
                      if index['unique'] and not index['partial']]
            if ('command_id',) not in unique:
                raise ProviderAccountStoreError('provider_account_schema')
            if conn.execute('PRAGMA foreign_key_check(provider_account_operations)').fetchone():
                raise ProviderAccountStoreError('provider_account_schema')
            self._validate_constraints(conn)
            conn.execute('CREATE INDEX IF NOT EXISTS idx_provider_accounts_owner ON provider_accounts(owner_id,machine,account_id)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_provider_operations_account ON provider_account_operations(account_id,revision)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_provider_accounts_delivery ON provider_accounts(machine,enabled,state,account_id,owner_id)')
            conn.execute("INSERT OR IGNORE INTO supervisor_meta(key,value) VALUES('provider_accounts_schema','1')")

    @staticmethod
    def _validate_constraints(conn):
        """Probe CHECK behavior without persisting test rows or accepting lax schemas."""
        conn.execute('SAVEPOINT provider_account_schema_probe')
        try:
            identifier = 'pa_' + secrets.token_hex(16)
            command = 'cmd_' + secrets.token_hex(16)
            conn.execute("INSERT INTO provider_accounts VALUES(?,?,?,?,?,1,'starting',1,NULL,NULL,0,'',0,0)",
                         (identifier, 'schema-probe', 'schema-probe', 'schema-probe', 'agy'))
            conn.execute("INSERT INTO supervisor_commands(command_id,machine_id,session_id,action,nonce,wire_json,issued_s,expires_s,status) "
                         "VALUES(?,?,?,'manage_provider_account',?,'{}',0,1,'queued')",
                         (command, 'schema-probe', identifier, secrets.token_hex(16)))
            conn.execute("INSERT INTO provider_account_operations VALUES(?,?,?,?,1,'create','starting',NULL,NULL,0,1)",
                         (secrets.token_hex(32), secrets.token_hex(32), command, identifier))
            for table, key, invalid in (
                    ('provider_accounts', 'account_id', {'provider': 'unknown', 'revision': 0, 'state': 'unknown', 'enabled': 2}),
                    ('provider_account_operations', 'command_id', {'token_digest': 'short', 'binding_digest': 'short',
                         'revision': 0, 'operation': 'unknown', 'state': 'unknown'})):
                identity = identifier if key == 'account_id' else command
                for column, value in invalid.items():
                    try:
                        conn.execute('UPDATE ' + table + ' SET ' + column + '=? WHERE ' + key + '=?', (value, identity))
                    except sqlite3.IntegrityError:
                        continue
                    raise ProviderAccountStoreError('provider_account_schema')
        finally:
            conn.execute('ROLLBACK TO provider_account_schema_probe')
            conn.execute('RELEASE provider_account_schema_probe')

    def _expire(self, conn, now):
        conn.execute("UPDATE provider_account_operations SET secret=NULL WHERE expires_s<=? OR command_id IN "
                     "(SELECT command_id FROM supervisor_commands WHERE status IN "
                     "('succeeded','already_finished','failed','rejected','expired'))", (now,))
        conn.execute("UPDATE provider_accounts SET state='failed',authorization_url=NULL,last_error='provider_account_failed',updated_s=? "
                     "WHERE state IN ('starting','waiting','verifying') AND EXISTS "
                     "(SELECT 1 FROM provider_account_operations o JOIN supervisor_commands c USING(command_id) "
                     "WHERE o.account_id=provider_accounts.account_id AND o.revision=provider_accounts.revision "
                     "AND ((o.expires_s<=? AND provider_accounts.login_command_id IS NULL) OR c.status IN ('failed','rejected','expired')))", (now, now))
        conn.execute("UPDATE provider_account_operations SET state='failed' WHERE state IN ('starting','waiting','verifying') "
                     "AND (expires_s<=? OR command_id IN (SELECT command_id FROM supervisor_commands "
                     "WHERE status IN ('failed','rejected','expired')))", (now,))
        conn.execute("UPDATE provider_accounts SET state='failed',authorization_url=NULL,last_error='login_expired',updated_s=? "
                     "WHERE state IN ('starting','waiting','verifying') AND login_command_id IS NOT NULL AND auth_expires_s<=?", (now, now))

    @staticmethod
    def _owned(conn, identifier, actor):
        row = conn.execute('SELECT * FROM provider_accounts WHERE account_id=? AND owner_id=?', (identifier, actor)).fetchone()
        if row is None:
            raise ProviderAccountStoreError('provider_account_not_found', 404)
        return row

    @staticmethod
    def _operation(conn, command_id):
        row = conn.execute('SELECT o.*,c.status AS command_status,c.receipt_status,c.receipt_reason,c.dispatched_s '
                           'FROM provider_account_operations o JOIN supervisor_commands c USING(command_id) '
                           'WHERE o.command_id=?', (command_id,)).fetchone()
        return dict(row) if row is not None else None

    def get(self, identifier, actor):
        with self._transaction() as conn:
            self._expire(conn, self.clock())
            return dict(self._owned(conn, identifier, actor))

    def list(self, machine, actor):
        with self._transaction() as conn:
            self._expire(conn, self.clock())
            return [dict(row) for row in conn.execute('SELECT * FROM provider_accounts WHERE owner_id=? AND machine=? ORDER BY account_id LIMIT 65', (actor, machine))]

    def operation(self, identifier, command_id, actor):
        with self._transaction() as conn:
            self._expire(conn, self.clock())
            self._owned(conn, identifier, actor)
            result = self._operation(conn, command_id)
            if result is None or result['account_id'] != identifier:
                raise ProviderAccountStoreError('provider_operation_not_found', 404)
            return result

    @contextmanager
    def task_delivery(self, machine, authorize=None):
        """Serialize ready bindings with disable/logout until lease commit.

        The caller may acquire task storage only while holding this fence.
        Authorization is checked once per owner on every poll, never cached
        across requests. No task or long-lived credential enters this store.
        """
        with self._transaction() as conn:
            grants, bindings = {}, []
            for row in conn.execute('SELECT account_id,owner_id FROM provider_accounts '
                    "WHERE machine=? AND enabled=1 AND state='ready'", (machine,)):
                owner = row['owner_id']
                if owner not in grants:
                    grants[owner] = authorize is None or bool(authorize(owner))
                if grants[owner]:
                    bindings.append((row['account_id'], owner))
            yield bindings

    def submit(self, *, machine, actor, operation, token, enqueue, identifier=None,
               name=None, response=None, login_command_id=None):
        """The writer lock serializes token matching, state admission and outbox."""
        now = self.clock()
        token_digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
        binding = _digest({'actor': actor, 'machine': machine, 'account': identifier,
                           'operation': operation, 'name': name, 'response': response,
                           'login_command_id': login_command_id})
        with self._transaction() as conn:
            self._expire(conn, now)
            prior = conn.execute('SELECT * FROM provider_account_operations WHERE token_digest=?', (token_digest,)).fetchone()
            if prior is not None:
                if prior['binding_digest'] != binding:
                    raise ProviderAccountStoreError('idempotency_conflict', 409)
                account = dict(self._owned(conn, prior['account_id'], actor))
                return account, self._operation(conn, prior['command_id']), False
            if operation == 'login' and self.box is None:
                raise ProviderAccountStoreError('provider_login_encryption_unavailable')
            if operation == 'create':
                if conn.execute('SELECT count(*) FROM provider_accounts WHERE owner_id=? AND machine=?', (actor, machine)).fetchone()[0] >= 64:
                    raise ProviderAccountStoreError('provider_account_limit', 409)
                identifier = 'pa_' + secrets.token_hex(16)
                conn.execute("INSERT INTO provider_accounts VALUES(?,?,?,?,?,1,'starting',1,NULL,NULL,0,'',?,?)",
                             (identifier, actor, machine, name, 'agy', now, now))
                account = self._owned(conn, identifier, actor)
                revision = 1
            else:
                account = self._owned(conn, identifier, actor)
                if account['machine'] != machine:
                    raise ProviderAccountStoreError('provider_account_not_found', 404)
                active = account['state'] in {'starting', 'waiting', 'verifying'}
                if operation in {'submit_login', 'cancel_login'}:
                    if (not login_command_id or account['login_command_id'] != login_command_id
                            or account['auth_expires_s'] <= now):
                        raise ProviderAccountStoreError('login_unavailable', 409)
                    if operation == 'submit_login':
                        if account['state'] != 'waiting' or not account['authorization_url']:
                            raise ProviderAccountStoreError('login_unavailable', 409)
                        from fleet_core.provider_accounts import login_response as validate_response
                        try:
                            response = validate_response(account['authorization_url'], response)
                        except ValueError:
                            raise ProviderAccountStoreError('invalid_login_response', 400) from None
                        if self.box is None:
                            raise ProviderAccountStoreError('provider_login_encryption_unavailable')
                elif active and operation not in {'disable', 'logout'}:
                    raise ProviderAccountStoreError('provider_account_busy', 409)
                if operation == 'login' and not account['enabled']:
                    raise ProviderAccountStoreError('provider_account_disabled', 409)
                revision = account['revision'] + 1
            payload = {'account_id': identifier, 'revision': revision, 'operation': operation,
                       'owner_digest': hashlib.sha256(actor.encode('utf-8')).hexdigest()}
            wire = enqueue(conn, account['machine'], identifier, payload, 'provider-account:' + token_digest)
            command_id = wire['command_id']
            # The command deadline bounds one-time handoff even if Hub restarts.
            from fleet_core.control import _parse_ts
            expires = _parse_ts(wire['expires_at'])
            if expires is None or expires <= now:
                raise ProviderAccountStoreError()
            enabled = 0 if operation in {'disable', 'logout'} else 1 if operation == 'enable' else account['enabled']
            flow = command_id if operation == 'login' else account['login_command_id'] if operation in {'submit_login', 'cancel_login'} else None
            deadline = now + AUTH_TTL_S if operation == 'login' else account['auth_expires_s'] if flow else 0
            state = 'verifying' if operation == 'submit_login' else 'starting'
            # Fail closed immediately; readiness only comes from a current node report.
            conn.execute('UPDATE provider_accounts SET revision=?,state=?,enabled=?,login_command_id=?,authorization_url=NULL,auth_expires_s=?,last_error=?,updated_s=? WHERE account_id=?',
                         (revision, state, enabled, flow, deadline, '', now, identifier))
            secret = self.box.encrypt(response.encode('utf-8'), command_id.encode('ascii')) if response is not None else None
            conn.execute('INSERT INTO provider_account_operations VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                         (token_digest, binding, command_id, identifier, revision, operation, state,
                          secret, flow, now, min(expires, deadline) if flow else expires))
            return dict(self._owned(conn, identifier, actor)), self._operation(conn, command_id), True

    def resolve(self, machine, command_id, authorize=None):
        with self._transaction() as conn:
            now = self.clock()
            self._expire(conn, now)
            op = self._operation(conn, command_id)
            if op is None:
                raise ProviderAccountStoreError('provider_operation_not_found', 404)
            account = conn.execute('SELECT * FROM provider_accounts WHERE account_id=?', (op['account_id'],)).fetchone()
            if account['machine'] != machine:
                raise ProviderAccountStoreError('provider_operation_not_found', 404)
            if authorize is not None and not authorize(account['owner_id']):
                raise ProviderAccountStoreError('forbidden', 403)
            if (account['revision'] != op['revision'] or op['expires_s'] <= now
                    or op['command_status'] not in {'delivered', 'accepted', 'executing'}):
                raise ProviderAccountStoreError('provider_operation_unavailable', 409)
            config = {'account_id': account['account_id'], 'revision': op['revision'],
                      'operation': op['operation'], 'owner_digest': hashlib.sha256(account['owner_id'].encode()).hexdigest()}
            if op['login_command_id']:
                config['login_command_id'] = op['login_command_id']
                config['expires_at'] = account['auth_expires_s']
            if op['secret'] is not None:
                if self.box is None:
                    raise ProviderAccountStoreError('provider_login_encryption_unavailable')
                try:
                    config['response'] = self.box.decrypt(bytes(op['secret']), command_id.encode('ascii')).decode('utf-8')
                except Exception:
                    raise ProviderAccountStoreError('provider_login_encryption_unavailable') from None
            if op['operation'] == 'submit_login' and 'response' not in config:
                raise ProviderAccountStoreError('provider_operation_unavailable', 409)
            return config

    def report(self, machine, data, authorize=None):
        with self._transaction() as conn:
            now = self.clock()
            self._expire(conn, now)
            op = self._operation(conn, data['command_id'])
            if op is None:
                raise ProviderAccountStoreError('provider_operation_not_found', 404)
            account = conn.execute('SELECT * FROM provider_accounts WHERE account_id=?', (op['account_id'],)).fetchone()
            if (account['machine'] != machine or op['account_id'] != data['account_id']):
                raise ProviderAccountStoreError('provider_operation_not_found', 404)
            if data.get('state') == 'ready' and authorize is not None and not authorize(account['owner_id']):
                return False
            if (op['revision'] != data['revision'] or account['revision'] != data['revision']
                    or op['expires_s'] <= now or op['dispatched_s'] <= 0
                    or op['command_status'] in {'queued', 'rejected', 'failed', 'expired'}):
                return False
            state = data['state']
            if state not in _TRANSITIONS.get(op['state'], set()):
                return False
            url = None
            expires = account['auth_expires_s']
            if state == 'waiting':
                if op['operation'] != 'login' or expires <= now:
                    return False
                try:
                    url = authorization_url(data.get('authorization_url'))
                except ValueError:
                    raise ProviderAccountStoreError('invalid_authorization_url', 400) from None
                if data.get('expires_at') is not None:
                    reported = data['expires_at']
                    if type(reported) not in (int, float) or not now < reported <= expires:
                        raise ProviderAccountStoreError('invalid_provider_report', 400)
                    expires = reported
            if state == 'ready' and (not account['enabled'] or op['operation'] not in {'login', 'submit_login', 'enable'}):
                return False
            if state == 'disabled' and account['enabled']:
                return False
            error = data.get('error', '')
            if error not in _ERROR_CODES:
                error = 'provider_account_failed'
            conn.execute('UPDATE provider_accounts SET state=?,authorization_url=?,auth_expires_s=?,last_error=?,updated_s=? WHERE account_id=?',
                         (state, url, expires, error, now, op['account_id']))
            conn.execute('UPDATE provider_account_operations SET state=?,secret=CASE WHEN ? THEN NULL ELSE secret END WHERE command_id=?',
                         (state, int(state in {'ready', 'failed', 'signed_out', 'disabled'}), op['command_id']))
            return True
