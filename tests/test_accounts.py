import pytest
from hub.accounts.store import AccountStore
from hub.http.errors import ApplicationError

PASSWORD = 'first-password-123!'
NEW = 'second-password-123!'

@pytest.fixture
def accounts(tmp_path):
    clock = [1000000.0]
    repo = AccountStore(tmp_path / 'accounts.db', clock=lambda: clock[0])
    return repo, clock


def register(repo, email='alice@example.test', registration='open'):
    mail = []
    repo.issue_code(email, 'register', registration, lambda *args: mail.append(args))
    repo.complete_code(email, 'register', mail[-1][2], PASSWORD, registration=registration)


def test_registration_is_verified_and_code_is_single_use(accounts):
    repo, _ = accounts
    mail = []
    repo.issue_code('ALICE@example.test', 'register', 'open', lambda *a: mail.append(a))
    with pytest.raises(ApplicationError):
        repo.login('alice@example.test', PASSWORD)
    repo.complete_code('alice@example.test', 'register', mail[0][2], PASSWORD, registration='open')
    token, user = repo.login('alice@example.test', PASSWORD)
    assert user['role'] == 'user' and user['id'].startswith('acct_')
    assert repo.identity(token)['id'] == user['id']
    assert 'password' not in user
    with pytest.raises(ApplicationError):
        repo.complete_code('alice@example.test', 'register', mail[0][2], PASSWORD, registration='open')
    assert PASSWORD.encode() not in repo.path.read_bytes()
    assert token.encode() not in repo.path.read_bytes()


def test_codes_expire_and_failed_attempts_persist(accounts):
    repo, now = accounts
    mail = []
    repo.issue_code('a@example.test', 'register', 'open', lambda *a: mail.append(a))
    wrong = '000000' if mail[0][2] != '000000' else '111111'
    for _ in range(5):
        with pytest.raises(ApplicationError):
            repo.complete_code('a@example.test', 'register', wrong, PASSWORD, registration='open')
    with pytest.raises(ApplicationError):
        repo.complete_code('a@example.test', 'register', mail[0][2], PASSWORD, registration='open')
    now[0] += 61
    repo.issue_code('a@example.test', 'register', 'open', lambda *a: mail.append(a))
    now[0] += 601
    with pytest.raises(ApplicationError):
        repo.complete_code('a@example.test', 'register', mail[-1][2], PASSWORD, registration='open')


def test_invite_required_and_consumed(accounts):
    repo, now = accounts
    sent = []
    repo.issue_code('a@example.test', 'register', 'invite', lambda *a: sent.append(a))
    assert not sent
    repo.invite('a@example.test')
    now[0] += 61
    register(repo, 'a@example.test', 'invite')
    with repo.connect() as db:
        assert not db.execute('SELECT * FROM invitations').fetchall()


def test_reset_and_password_change_revoke_all_sessions(accounts):
    repo, now = accounts
    register(repo)
    token, user = repo.login('alice@example.test', PASSWORD)
    second, _ = repo.login('alice@example.test', PASSWORD)
    now[0] += 61
    mail = []
    repo.issue_code('alice@example.test', 'reset', 'open', lambda *a: mail.append(a))
    repo.complete_code('alice@example.test', 'reset', mail[0][2], NEW)
    assert repo.identity(token) is None and repo.identity(second) is None
    with pytest.raises(ApplicationError):
        repo.login('alice@example.test', PASSWORD)
    token, _ = repo.login('alice@example.test', NEW)
    repo.change_password(user['id'], NEW, PASSWORD)
    assert repo.identity(token) is None


def test_session_expiry_revoke_and_owner_scope(accounts):
    repo, now = accounts
    register(repo)
    register(repo, 'b@example.test')
    token, user = repo.login('alice@example.test', PASSWORD)
    other, user2 = repo.login('b@example.test', PASSWORD)
    sid = repo.sessions(user['id'], token)[0]['id']
    repo.revoke(user2['id'], sid)
    assert repo.identity(token)
    repo.logout(token)
    assert repo.identity(token) is None and repo.identity(other)
    now[0] += 604801
    assert repo.identity(other) is None


def test_admin_disable_and_last_admin(accounts):
    repo, _ = accounts
    repo.bootstrap_admin('admin@example.test', PASSWORD)
    admin_token, admin = repo.login('admin@example.test', PASSWORD)
    register(repo)
    token, user = repo.login('alice@example.test', PASSWORD)
    with pytest.raises(ApplicationError):
        repo.manage(user['id'], admin['id'], changes={'role': 'user', 'active': False}, expected_revision=admin['revision'])
    with pytest.raises(ApplicationError):
        repo.manage(admin['id'], admin['id'], changes={'role': 'user', 'active': False}, expected_revision=admin['revision'])
    repo.manage(admin['id'], user['id'], changes={'active': False}, expected_revision=user['revision'])
    assert not repo.identity(token) and repo.identity(admin_token)
    with pytest.raises(ApplicationError):
        repo.login('alice@example.test', PASSWORD)


def test_throttling_and_failed_delivery(accounts):
    repo, _ = accounts
    def broken(*args):
        raise RuntimeError('mail failed')
    with pytest.raises(ApplicationError) as exc:
        repo.issue_code('a@example.test', 'register', 'open', broken)
    assert exc.value.status == 503
    with repo.connect() as db:
        assert not db.execute('SELECT * FROM challenges').fetchall()
    with pytest.raises(ApplicationError) as exc:
        repo.issue_code('a@example.test', 'register', 'open', lambda *a: None)
    assert exc.value.status == 429
