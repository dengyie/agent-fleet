"""Durable accounts and revocable sessions. All security mutations are serialized."""
from contextlib import contextmanager
import hashlib
import logging
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid

from werkzeug.security import check_password_hash, generate_password_hash
from hub.diagnostics import log_failure
from hub.http.errors import ApplicationError

logger = logging.getLogger(__name__)


def fail(code, detail, status=400):
    raise ApplicationError(code, detail, status)


def email_address(value):
    if not isinstance(value, str) or len(value) > 254:
        fail('invalid_email', '请输入有效邮箱地址')
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}", value):
        fail('invalid_email', '请输入有效邮箱地址')
    return value


def password_hash(value):
    if not isinstance(value, str) or not 12 <= len(value) <= 128:
        fail('invalid_password', '密码长度需为 12–128 个字符')
    return generate_password_hash(value, method='scrypt')


def username(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9_-]{3,32}', value.strip().lower()):
        fail('invalid_username', '账号需为 3–32 位字母、数字、下划线或短横线')
    return value.strip().lower()


def login_identifier(value):
    return email_address(value) if isinstance(value, str) and '@' in value else username(value)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class AccountStore:
    def __init__(self, path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                    password TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','admin')),
                    active INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    created REAL NOT NULL, expires REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
                CREATE TABLE IF NOT EXISTS challenges (
                    email TEXT NOT NULL, purpose TEXT NOT NULL, code TEXT NOT NULL,
                    expires REAL NOT NULL, attempts INTEGER NOT NULL,
                    PRIMARY KEY(email,purpose));
                CREATE TABLE IF NOT EXISTS invitations (
                    code TEXT PRIMARY KEY, email TEXT, created REAL NOT NULL, expires REAL NOT NULL,
                    claimed_by TEXT, claimed_at REAL);
                CREATE INDEX IF NOT EXISTS invitations_email ON invitations(email);
                CREATE TABLE IF NOT EXISTS limits (
                    key TEXT PRIMARY KEY, start REAL NOT NULL, count INTEGER NOT NULL);
            ''')
            # executescript commits the initial transaction. Serialize schema
            # upgrades so concurrent Hub processes cannot race this ALTER.
            db.execute('BEGIN IMMEDIATE')
            if 'revision' not in {row['name'] for row in db.execute('PRAGMA table_info(users)')}:
                db.execute('ALTER TABLE users ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
            if 'username' not in {row['name'] for row in db.execute('PRAGMA table_info(users)')}:
                db.execute('ALTER TABLE users ADD COLUMN username TEXT')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS users_username ON users(username)')
            inv_cols = {row['name'] for row in db.execute('PRAGMA table_info(invitations)')}
            if 'code' not in inv_cols:
                # Migrate legacy email-primary-key invitations table to code-primary-key
                db.execute('''
                    CREATE TABLE IF NOT EXISTS invitations_new (
                        code TEXT PRIMARY KEY, email TEXT, created REAL NOT NULL, expires REAL NOT NULL,
                        claimed_by TEXT, claimed_at REAL)
                ''')
                now_init = self.clock()
                for row in db.execute('SELECT email, expires FROM invitations').fetchall():
                    legacy_code = 'inv_' + secrets.token_hex(8)
                    db.execute('INSERT OR REPLACE INTO invitations_new VALUES (?,?,?,?,NULL,NULL)',
                               (legacy_code, row['email'], now_init, row['expires']))
                db.execute('DROP TABLE invitations')
                db.execute('ALTER TABLE invitations_new RENAME TO invitations')
                db.execute('CREATE INDEX IF NOT EXISTS invitations_email ON invitations(email)')
        self.path.chmod(0o600)
        self.dummy_hash = generate_password_hash(secrets.token_urlsafe(32), method='scrypt')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def limit(self, key, maximum, window):
        with self.connect() as db:
            self._limit(db, key, maximum, window, self.clock())

    def check_and_increment_limit(self, key, window):
        now = self.clock()
        with self.connect() as db:
            db.execute('DELETE FROM limits WHERE start < ?', (now - 86400,))
            row = db.execute('SELECT * FROM limits WHERE key=?', (key,)).fetchone()
            if not row or row['start'] <= now - window:
                db.execute('INSERT OR REPLACE INTO limits VALUES (?,?,1)', (key, now))
                return 0
            count = row['count']
            db.execute('UPDATE limits SET count=count+1 WHERE key=?', (key,))
            return count

    @staticmethod
    def _limit(db, key, maximum, window, now):
        db.execute('DELETE FROM limits WHERE start < ?', (now - 86400,))
        row = db.execute('SELECT * FROM limits WHERE key=?', (key,)).fetchone()
        if row and row['start'] > now - window and row['count'] >= maximum:
            fail('rate_limited', '操作过于频繁，请稍后再试', 429)
        if not row or row['start'] <= now - window:
            db.execute('INSERT OR REPLACE INTO limits VALUES (?,?,1)', (key, now))
        else:
            db.execute('UPDATE limits SET count=count+1 WHERE key=?', (key,))

    @staticmethod
    def public(row):
        return {k: row[k] for k in ('id', 'email', 'name', 'role', 'active', 'created', 'revision')}

    def invite(self, email=None):
        email_clean = email_address(email) if email else None
        code = 'inv_' + secrets.token_hex(8)
        now = self.clock()
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO invitations VALUES (?,?,?,?,NULL,NULL)',
                       (code, email_clean, now, now + 604800))
        return code

    def invitations(self):
        with self.connect() as db:
            now = self.clock()
            rows = db.execute('SELECT code, email, created, expires, claimed_by, claimed_at '
                              'FROM invitations WHERE expires > ? AND claimed_by IS NULL ORDER BY created DESC LIMIT 100',
                              (now,)).fetchall()
            return [{'code': r['code'], 'email': r['email'], 'created': r['created'], 'expires': r['expires']} for r in rows]

    def issue_code(self, email, purpose, registration, sender, invite_code=None):
        email = email_address(email)
        if purpose not in ('register', 'reset'):
            fail('invalid_purpose', '验证码用途无效')
        code = str(secrets.randbelow(1000000)).zfill(6)
        hashed = generate_password_hash(code, method='scrypt')
        with self.connect() as db:
            now = self.clock()
            db.execute('DELETE FROM challenges WHERE expires<=?', (now,))
            user = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
            
            # Check invitation eligibility
            invitation = None
            if purpose == 'register' and not user:
                if registration == 'open':
                    invitation = True
                else:
                    # Invite-only registration requires a valid, unclaimed, unexpired invite code
                    cleaned_inv = (invite_code.strip() if isinstance(invite_code, str) else '')
                    if cleaned_inv:
                        inv_row = db.execute('SELECT * FROM invitations WHERE code=? AND expires>? AND claimed_by IS NULL',
                                             (cleaned_inv, now)).fetchone()
                        if inv_row and (not inv_row['email'] or inv_row['email'] == email):
                            invitation = inv_row
                    if not invitation:
                        # Legacy fallback: check if email was explicitly invited
                        inv_row = db.execute('SELECT * FROM invitations WHERE email=? AND expires>? AND claimed_by IS NULL',
                                             (email, now)).fetchone()
                        if inv_row:
                            invitation = inv_row

            eligible = (purpose == 'reset' and user and user['active']) or (
                purpose == 'register' and not user and bool(invitation))
            if eligible:
                # If an open invitation was used without pre-bound email, lock it to this target email
                # and limit code generations per invite token to prevent mass SMTP bombing
                if purpose == 'register' and isinstance(invitation, sqlite3.Row):
                    self._limit(db, 'mail-invite:' + invitation['code'], 5, 86400, now)
                    if not invitation['email']:
                        db.execute('UPDATE invitations SET email=? WHERE code=? AND email IS NULL',
                                   (email, invitation['code']))
                # Eligibility, both reservations and the new grant commit together.
                self._limit(db, 'mail-minute:' + email, 1, 60, now)
                self._limit(db, 'mail-day:' + email, 10, 86400, now)
                db.execute('INSERT OR REPLACE INTO challenges VALUES (?,?,?,?,5)', (email, purpose, hashed, now + 600))
        if eligible:
            try:
                sender(email, purpose, code)
            except Exception as exc:
                log_failure(logger, 'account_mail_delivery_failed', exc, purpose=purpose)
                with self.connect() as db:
                    db.execute('DELETE FROM challenges WHERE email=? AND purpose=? AND code=?', (email, purpose, hashed))
                fail('mail_unavailable', '邮件暂时无法发送，请稍后重试', 503)

    def complete_code(self, email, purpose, code, password, *, registration='invite', name='', invite_code=None):
        email = email_address(email)
        hashed = password_hash(password)
        if not isinstance(code, str) or not re.fullmatch(r'\d{6}', code):
            fail('invalid_code', '验证码无效或已过期')
        if not isinstance(name, str) or len(name.strip()) > 80:
            fail('invalid_name', '昵称最多 80 个字符')
        now = self.clock()
        success = False
        with self.connect() as db:
            row = db.execute('SELECT * FROM challenges WHERE email=? AND purpose=?', (email, purpose)).fetchone()
            if row and row['expires'] > now and row['attempts'] > 0:
                db.execute('UPDATE challenges SET attempts=attempts-1 WHERE email=? AND purpose=?', (email, purpose))
                if check_password_hash(row['code'], code):
                    user = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
                    
                    invitation_code_to_consume = None
                    if purpose == 'register' and not user:
                        if registration == 'open':
                            invitation_code_to_consume = True
                        else:
                            cleaned_inv = (invite_code.strip() if isinstance(invite_code, str) else '')
                            if cleaned_inv:
                                inv_row = db.execute('SELECT * FROM invitations WHERE code=? AND expires>? AND claimed_by IS NULL',
                                                     (cleaned_inv, now)).fetchone()
                                if inv_row and (not inv_row['email'] or inv_row['email'] == email):
                                    invitation_code_to_consume = inv_row['code']
                            if not invitation_code_to_consume:
                                inv_row = db.execute('SELECT * FROM invitations WHERE email=? AND expires>? AND claimed_by IS NULL',
                                                     (email, now)).fetchone()
                                if inv_row:
                                    invitation_code_to_consume = inv_row['code']

                    if purpose == 'register' and not user and invitation_code_to_consume:
                        db.execute('INSERT INTO users (id,email,name,password,role,active,created) VALUES (?,?,?,?,?,1,?)',
                                   ('acct_' + uuid.uuid4().hex, email, name.strip() or email.split('@')[0], hashed, 'user', now))
                        if isinstance(invitation_code_to_consume, str):
                            db.execute('UPDATE invitations SET claimed_by=?, claimed_at=? WHERE code=?',
                                       (email, now, invitation_code_to_consume))
                        success = True
                    elif purpose == 'reset' and user and user['active']:
                        self._replace_password(db, user, hashed)
                        success = True
                    db.execute('DELETE FROM challenges WHERE email=? AND purpose=?', (email, purpose))
        if not success:
            fail('invalid_code', '验证码无效或已过期')

    def login(self, email, password):
        identifier = login_identifier(email)
        with self.connect() as db:
            user = db.execute('SELECT * FROM users WHERE email=? OR username=?', (identifier, identifier)).fetchone()
        # Alias and email share the account's credential-attempt budget.
        self.limit('login:' + (user['email'] if user else identifier), 10, 900)
        if not isinstance(password, str) or len(password) > 128:
            fail('invalid_login', '账号或密码错误', 401)
        valid = check_password_hash(user['password'] if user else self.dummy_hash, password)
        if not valid or not user or not user['active']:
            fail('invalid_login', '账号或密码错误', 401)
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self.connect() as db:
            current = db.execute('SELECT * FROM users WHERE id=?', (user['id'],)).fetchone()
            if not current['active'] or current['password'] != user['password']:
                fail('invalid_login', '账号或密码错误', 401)
            db.execute('DELETE FROM sessions WHERE expires<=?', (now,))
            db.execute('INSERT INTO sessions VALUES (?,?,?,?)', (digest(token), user['id'], now, now + 604800))
        return token, self.public(current)

    def identity(self, token):
        if not token or len(token) > 128:
            return None
        with self.connect() as db:
            row = db.execute('SELECT users.* FROM sessions JOIN users ON users.id=sessions.user_id '
                             'WHERE token=? AND expires>? AND active=1', (digest(token), self.clock())).fetchone()
            return self.public(row) if row else None

    def logout(self, token):
        with self.connect() as db:
            db.execute('DELETE FROM sessions WHERE token=?', (digest(token),))

    def sessions(self, user_id, token):
        with self.connect() as db:
            rows = db.execute('SELECT token,created,expires FROM sessions WHERE user_id=? AND expires>? ORDER BY created DESC LIMIT 100',
                              (user_id, self.clock())).fetchall()
            return [{'id': row['token'], 'created': row['created'], 'expires': row['expires'],
                     'current': row['token'] == digest(token)} for row in rows]

    def revoke(self, user_id, session_id=None):
        with self.connect() as db:
            if session_id:
                db.execute('DELETE FROM sessions WHERE user_id=? AND token=?', (user_id, session_id))
            else:
                db.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))

    @staticmethod
    def _replace_password(db, user, hashed):
        db.execute('UPDATE users SET password=? WHERE id=?', (hashed, user['id']))
        db.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],))
        # Verified replacement retires the old credential's attempt state only.
        db.execute('DELETE FROM limits WHERE key IN (?,?)',
                   ('login:' + user['email'], 'password:' + user['id']))

    def change_password(self, user_id, old_password, new_password):
        hashed = password_hash(new_password)
        if not isinstance(old_password, str) or len(old_password) > 128:
            fail('invalid_password', '当前密码错误')
        self.limit('password:' + user_id, 10, 900)
        with self.connect() as db:
            row = db.execute('SELECT * FROM users WHERE id=? AND active=1', (user_id,)).fetchone()
            if not row or not check_password_hash(row['password'], old_password):
                fail('invalid_password', '当前密码错误')
            self._replace_password(db, row, hashed)
            # Password rotation also revokes previously issued recovery grants.
            db.execute('DELETE FROM challenges WHERE email=? AND purpose="reset"', (row['email'],))

    def update_profile(self, user_id, name):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            fail('invalid_name', '昵称长度需为 1–80 个字符')
        with self.connect() as db:
            db.execute('UPDATE users SET name=? WHERE id=?', (name.strip(), user_id))

    def users(self, after=''):
        with self.connect() as db:
            return [self.public(row) for row in db.execute('SELECT * FROM users WHERE id>? ORDER BY id LIMIT 100', (after,))]

    def manage(self, actor, user_id, *, changes, expected_revision):
        if type(expected_revision) is not int or expected_revision < 0:
            fail('precondition_required', '请重新加载账号信息后操作', 428)
        if not isinstance(changes, dict) or not changes or set(changes) - {'role', 'active'}:
            fail('invalid_user', '只允许修改角色或启用状态')
        if ('role' in changes and changes['role'] not in ('user', 'admin')) or (
                'active' in changes and type(changes['active']) is not bool):
            fail('invalid_user', '账号角色或状态无效')
        with self.connect() as db:
            admin = db.execute('SELECT * FROM users WHERE id=? AND active=1 AND role="admin"', (actor,)).fetchone()
            if not admin:
                fail('forbidden', '需要管理员权限', 403)
            row = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
            if not row:
                fail('not_found', '账号不存在', 404)
            if row['revision'] != expected_revision:
                fail('revision_conflict', '账号信息已被修改，请查看最新状态后重试', 409)
            role = changes.get('role', row['role'])
            active = changes.get('active', bool(row['active']))
            if row['role'] == 'admin' and row['active'] and (role != 'admin' or not active):
                count = db.execute('SELECT count(*) FROM users WHERE role="admin" AND active=1').fetchone()[0]
                if count <= 1:
                    fail('last_admin', '不能停用或降级最后一个管理员', 409)
            db.execute('UPDATE users SET role=?,active=?,revision=revision+1 WHERE id=?', (role, int(active), user_id))
            db.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))
            return self.public(db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone())

    def bootstrap_admin(self, email, password, *, login_name=None):
        email = email_address(email)
        alias = username(login_name) if login_name is not None else None
        hashed = password_hash(password)
        with self.connect() as db:
            if db.execute('SELECT 1 FROM users WHERE role="admin"').fetchone():
                fail('admin_exists', '管理员已存在', 409)
            db.execute('INSERT INTO users (id,email,name,password,role,active,created,username) VALUES (?,?,?,?,?,1,?,?)',
                       ('acct_' + uuid.uuid4().hex, email, '管理员', hashed, 'admin', self.clock(), alias))
