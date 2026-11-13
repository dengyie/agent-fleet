"""Muse workspace transport, privacy, replay and UI acceptance."""
import json
import base64
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

import pytest
from werkzeug.serving import make_server
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.application.muse_service import MuseService
from hub.infrastructure.muse_repository import MuseRepository
from hub.integrations.muse.client import MuseError

ROOT = Path(__file__).resolve().parents[1]
LOGIN = {'cookies': [{'name': 'hatch_sess', 'value': 'fixture-secret', 'domain': '.muse.ai'}]}
LOGIN_A = {'cookies': [{'name': 'hatch_sess', 'value': 'account-A', 'domain': '.muse.ai'}]}
LOGIN_B = {'cookies': [{'name': 'hatch_sess', 'value': 'account-B', 'domain': '.muse.ai'}]}
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jv1sAAAAASUVORK5CYII=')


def test_muse_event_projection_model():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for Muse event projection tests')
    result = subprocess.run([node, str(ROOT / 'tests/fixtures/muse_model.mjs'),
                             str(ROOT / 'frontend/assets/views/muse/model.js')],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


class FakeMuse:
    def __init__(self, cookies):
        self.cookies = cookies
        self.sent = []
        self.decisions = []
        self.chats = [dict(session_id='main', title='产品研究', is_primary=True, is_thread=False, status='completed'),
                      dict(session_id='side', title='实现计划', is_primary=False, is_thread=True, status='completed')]
        self.events = {'main': [dict(seq=1, message_id='m1', role='assistant', event_name='message.assistant',
                                    display_text='已完成资料整理。\n\n**下一步**：实现接入。', payload={}),
                               dict(seq=2, message_id='card1', role='assistant', event_name='delta.presentation',
                                    payload={'id': 'browser1', 'kind': 'browser_task', 'data': {
                                        'title': '浏览器', 'display_status': '已完成', 'activity_title': '查阅参考资料',
                                        'current_site': {'page_title': '参考来源', 'url': 'https://example.com'}, 'step_count': 6,
                                        'latest_screenshot': {'url': 'sandbox://workspace/browser.png'}}})],
                       'side': [dict(seq=3, message_id='m3', role='assistant', event_name='message.assistant',
                                    payload={'content': [{'type': 'text', 'text': '这是计划。'},
                                                         {'type': 'plan', 'title': '实施计划', 'steps': ['连接 Muse', '验证对话']},
                                                         {'type': 'code', 'code': '<img src=x onerror=alert(1)>'},
                                                         {'type': 'tool_call', 'name': '资料检索', 'status': 'completed', 'input': {'query':'fixture'}, 'output':'参考资料'},
                                                         {'type': 'file', 'filename': 'notes.txt', 'path': 'workspace/notes.txt'},
                                                         {'type': 'unknown', 'html': '<script>alert(1)</script>'}]})]}
        self.pending = [{'approval_id': 'a1', 'message_revision': 2, 'status': 'pending',
                         'scope': {'runtime_request_trace': {'thread_id': 'side'}},
                         'display': {'summary_title': '访问参考网站', 'purpose_summary': '获取任务需要的资料'}}]

    def request(self, method, path, params=None, **kwargs):
        params = params or {}
        if path == '/api/session/list': return {'sessions': self.chats}
        if path == '/chat/history':
            events = self.events.get(params['session_id'], [])
            if params.get('before_seq'):
                events = [event for event in events if event.get('seq', 0) < params['before_seq']]
                return {'chat_events': events[-60:], 'has_more': len(events) > 60}
            return {'chat_events': events[-60:], 'has_more': len(events) > 60}
        if path == '/chat/stream':
            self.sent.append(params)
            if params['message'] == 'uncertain': raise MuseError('muse_transport', '连接中断')
            sid = params['session_id']
            if sid not in {s['session_id'] for s in self.chats}:
                self.chats.append(dict(session_id=sid, title='接入测试', is_thread=True, status='completed'))
            self.events.setdefault(sid, []).extend([
                dict(seq=100+len(self.sent)*2, message_id='u'+str(len(self.sent)), role='user', event_name='message.user', display_text=params['message']),
                dict(seq=101+len(self.sent)*2, message_id='r'+str(len(self.sent)), role='assistant', event_name='message.assistant', display_text='FLEET_OK')])
            return {'session_id': sid, 'is_thread': True, 'message_id': 'new'}
        if path == '/approvals': return {'pending_approvals': self.pending}
        if path.endswith('/decide'):
            self.decisions.append((path, params))
            self.pending = [a for a in self.pending if a['approval_id'] != path.split('/')[2]]
            return {'approval': {'status': 'approved'}}
        if path == '/chat/cancel': return {'cancelled': True}
        if path == '/fs/read':
            content = PNG if params['path'].endswith('.png') else b'fixture download'
            return {'data_base64': base64.b64encode(content).decode(), 'len': len(content), 'eof': True}
        raise AssertionError(path)


@pytest.fixture
def services(tmp_path):
    remote = FakeMuse(LOGIN['cookies'])
    repo = MuseRepository(tmp_path / 'muse')
    service = MuseService(repo, lambda _: remote)
    yield service, repo, remote
    service.close()


def test_encrypted_owner_scoped_login_and_disconnect(services):
    service, repo, remote = services
    assert not service.status('owner')['connected']
    service.import_login('owner', LOGIN)
    assert b'fixture-secret' not in repo.path.read_bytes()
    assert repo.path.stat().st_mode & 0o777 == 0o600
    assert (repo.directory/'credentials.key').stat().st_mode & 0o777 == 0o600
    assert 'fixture-secret' not in json.dumps(service.status('owner'))
    assert not service.status('another')['connected']
    with pytest.raises(MuseError): service.sessions('another')
    revision = repo.account('owner')['revision']
    service.disconnect('owner'); service.import_login('owner', LOGIN)
    assert repo.account('owner')['revision'] != revision
    service.disconnect('owner')
    assert not service.status('owner')['connected']


def test_legacy_single_account_store_migrates_to_default_account(tmp_path):
    from cryptography.fernet import Fernet
    directory = tmp_path / 'muse'; directory.mkdir(mode=0o700)
    key = Fernet.generate_key(); cipher = Fernet(key)
    (directory / 'credentials.key').write_bytes(key); os.chmod(directory / 'credentials.key', 0o600)
    import sqlite3, time
    with sqlite3.connect(directory / 'muse.db') as conn:
        conn.executescript('''CREATE TABLE accounts(owner TEXT PRIMARY KEY,login BLOB NOT NULL,revision INTEGER NOT NULL,settings TEXT NOT NULL,updated REAL NOT NULL);
        CREATE TABLE receipts(owner TEXT NOT NULL,request_id TEXT NOT NULL,fingerprint TEXT NOT NULL,action TEXT NOT NULL,state TEXT NOT NULL,result TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(owner,request_id));''')
        conn.execute('INSERT INTO accounts VALUES (?,?,?,?,?)', ('legacy', cipher.encrypt(json.dumps(LOGIN['cookies']).encode()), 7,
                     json.dumps({'auto_approve_sessions':['side']}), time.time()))
        conn.execute('INSERT INTO receipts VALUES (?,?,?,?,?,?,?)', ('legacy','old-request','fingerprint','send','unknown','{}',time.time()))
    repository = MuseRepository(directory)
    account = repository.account('legacy', 'default')
    assert account['cookies'] == LOGIN['cookies'] and account['revision'] == 7
    assert account['settings']['auto_approve_sessions'] == ['side']
    assert repository.audit('legacy', 'default')[0]['request_id'] == 'old-request'


def test_legacy_receipt_without_live_account_is_preserved_but_not_replayed(tmp_path):
    from cryptography.fernet import Fernet
    directory = tmp_path / 'muse'; directory.mkdir(mode=0o700)
    key = Fernet.generate_key(); (directory / 'credentials.key').write_bytes(key); os.chmod(directory / 'credentials.key', 0o600)
    import sqlite3, time
    with sqlite3.connect(directory / 'muse.db') as conn:
        conn.executescript('''CREATE TABLE accounts(owner TEXT PRIMARY KEY,login BLOB NOT NULL,revision INTEGER NOT NULL,settings TEXT NOT NULL,updated REAL NOT NULL);
        CREATE TABLE receipts(owner TEXT NOT NULL,request_id TEXT NOT NULL,fingerprint TEXT NOT NULL,action TEXT NOT NULL,state TEXT NOT NULL,result TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(owner,request_id));''')
        conn.execute('INSERT INTO receipts VALUES (?,?,?,?,?,?,?)', ('disconnected','old-request','fingerprint','send','unknown','{}',time.time()))
    repository = MuseRepository(directory)
    assert repository.audit('disconnected', 'default') == []
    repository.import_login('disconnected', LOGIN)
    assert repository.audit('disconnected', 'default')[0]['request_id'] == 'old-request'
    with pytest.raises(MuseError, match='另一份'):
        repository.claim('disconnected', 'old-request', 'send', {'message': 'same'}, 'default', repository.account('disconnected')['revision'])


def test_reimport_always_increments_connection_revision(tmp_path):
    repo = MuseRepository(tmp_path / 'muse')
    repo.import_login('owner', LOGIN)
    first = repo.account('owner', 'default')['revision']
    repo.import_login('owner', LOGIN)
    second = repo.account('owner', 'default')['revision']
    assert second == first + 1


def test_import_rejects_malformed_session_list_without_saving_credentials(services):
    service, repo, remote = services
    remote.request = lambda *a, **k: []
    with pytest.raises(MuseError, match='会话列表格式'):
        service.import_login('owner', LOGIN)
    assert repo.list_accounts('owner') == []


def test_late_finish_cannot_overwrite_unknown_receipt(services):
    service, repo, remote = services
    service.import_login('owner', LOGIN)
    account = repo.account('owner', 'default')
    repo.claim('owner', 'late', 'send', {'message': 'x'}, 'default', account['revision'])
    with repo._connect() as conn:
        conn.execute("UPDATE receipts SET state='unknown',result=? WHERE owner=? AND request_id=?",
                     (json.dumps({'error': 'muse_dispatch_unknown'}), 'owner', 'late'))
    result = repo.finish('owner', 'late', 'confirmed', {'message_id': 'late'}, 'default', account['revision'])
    assert result['state'] == 'unknown'
    assert result['result']['error'] == 'muse_dispatch_unknown'
def test_duplicate_unknown_send_is_never_replayed_after_restart(services):
    service, repo, remote = services
    service.import_login('o', LOGIN)
    result = service.send('o', 'test-session', 'uncertain', 'request-1')
    assert result['state'] == 'unknown'
    restarted = MuseService(MuseRepository(repo.directory), lambda _: remote)
    assert restarted.send('o', 'test-session', 'uncertain', 'request-1')['state'] == 'unknown'
    assert len(remote.sent) == 1
    with pytest.raises(MuseError, match='标识'):
        restarted.send('o', 'test-session', 'changed', 'request-1')


def test_preflight_not_ready_can_retry_same_request_id(services):
    service, repo, remote = services
    service.import_login('o', LOGIN)
    original = remote.request
    first = True
    def not_ready(method, path, params=None, **kwargs):
        nonlocal first
        if path == '/chat/stream' and first:
            first = False
            raise MuseError('muse_not_ready', 'fixture unassigned', 409)
        return original(method, path, params, **kwargs)
    remote.request = not_ready
    assert service.send('o', 'side', 'retry', 'retry-id')['state'] == 'retryable'
    assert service.send('o', 'side', 'retry', 'retry-id')['state'] == 'confirmed'
    assert len(remote.sent) == 1


def test_concurrent_retry_of_retryable_receipt_dispatches_once(services):
    service, repo, remote = services
    service.import_login('o', LOGIN)
    original = remote.request
    first, entered, release = True, threading.Event(), threading.Event()
    def not_ready(method, path, params=None, **kwargs):
        nonlocal first
        if path == '/chat/stream' and first:
            first = False
            raise MuseError('muse_not_ready', 'fixture unassigned', 409)
        if path == '/chat/stream':
            entered.set()
            assert release.wait(5)
        return original(method, path, params, **kwargs)
    remote.request = not_ready
    assert service.send('o', 'side', 'retry', 'retry-race')['state'] == 'retryable'
    results = []
    workers = [threading.Thread(target=lambda: results.append(service.send('o', 'side', 'retry', 'retry-race'))) for _ in range(2)]
    workers[0].start(); assert entered.wait(2); workers[1].start()
    try:
        assert workers[1].is_alive()
    finally:
        release.set()
    for worker in workers: worker.join(3)
    assert all(not worker.is_alive() for worker in workers)
    assert len(remote.sent) == 1
    assert all(item['state'] == 'confirmed' for item in results)


def test_stale_dispatching_receipt_becomes_unknown_without_replay(services):
    service, repo, remote = services
    service.import_login('o', LOGIN)
    request_id = 'crashed-dispatch'
    original = remote.request
    remote.request = lambda method, path, params=None, **kwargs: (
        (_ for _ in ()).throw(MuseError('muse_not_ready', 'not ready', 409))
        if path == '/chat/stream' else original(method, path, params, **kwargs))
    assert service.send('o', 'side', 'lost', request_id)['state'] == 'retryable'
    with repo._connect() as conn:
        conn.execute("UPDATE receipts SET state='dispatching',created=? WHERE owner=? AND request_id=?",
                     (0, 'o', request_id))
    remote.request = original
    result = service.send('o', 'side', 'lost', request_id)
    assert result['state'] == 'unknown'
    assert len(remote.sent) == 0


def test_connection_revision_binds_side_effects_and_accounts_are_isolated(tmp_path):
    first, second = FakeMuse(LOGIN_A['cookies']), FakeMuse(LOGIN_B['cookies'])
    repo = MuseRepository(tmp_path / 'muse')
    service = MuseService(repo, lambda cookies: first if cookies[0]['value'] == 'account-A' else second)
    try:
        old = service.import_login('o', LOGIN_A, account_id='primary', label='工作账号')['connection_revision']
        service.import_login('o', LOGIN_B, account_id='research', label='研究账号')
        service.import_login('o', LOGIN_B, account_id='primary', label='工作账号')
        assert {item['account_id'] for item in service.status('o')['accounts']} == {'primary', 'research'}
        assert service.send('o', 'side', 'right account', 'right', account_id='research')['state'] == 'confirmed'
        with pytest.raises(MuseError, match='切换'):
            service.send('o', 'side', 'stale account', 'stale', account_id='primary', expected_revision=old)
        assert [item['message'] for item in first.sent] == []
        assert [item['message'] for item in second.sent] == ['right account']
    finally:
        service.close()


def test_stale_disconnect_and_receipt_cannot_cross_account_reimport(tmp_path):
    repo = MuseRepository(tmp_path / 'muse')
    first = MuseService(repo, lambda _: FakeMuse(LOGIN_A['cookies']))
    second = MuseService(MuseRepository(repo.directory), lambda _: FakeMuse(LOGIN_B['cookies']))
    try:
        old = first.import_login('o', LOGIN_A, account_id='primary')['connection_revision']
        # Simulate another Hub worker replacing this account after the caller
        # loaded its old revision.
        second.import_login('o', LOGIN_B, account_id='primary')
        with pytest.raises(MuseError, match='切换'):
            repo.disconnect('o', 'primary', int(old))
        current = repo.account('o', 'primary')
        assert current['cookies'][0]['value'] == LOGIN_B['cookies'][0]['value']

        with repo._connect() as conn:
            conn.execute('INSERT INTO receipts(owner,account_id,request_id,fingerprint,action,state,result,created,connection_revision) VALUES (?,?,?,?,?,?,?,?,?)',
                         ('o', 'primary', 'old-receipt', 'fingerprint', 'send', 'confirmed', '{}', 1, int(old)))
        with pytest.raises(MuseError, match='另一份'):
            repo.claim('o', 'old-receipt', 'send', {'message': 'same'}, 'primary', current['revision'])
    finally:
        first.close(); second.close()


def test_account_change_waits_for_leases_across_repository_instances(tmp_path):
    first = MuseRepository(tmp_path / 'muse')
    second = MuseRepository(tmp_path / 'muse')
    service = MuseService(first, lambda _: FakeMuse(LOGIN['cookies']))
    service.import_login('o', LOGIN, account_id='default')
    entered, release, changed = threading.Event(), threading.Event(), threading.Event()

    def hold_lease():
        with first.account_lease('o', 'default'):
            entered.set()
            assert release.wait(5)

    def replace_account():
        with second.account_change('o', 'default'):
            second.import_login('o', LOGIN_B['cookies'], 'default', 'replacement')
            changed.set()

    reader = threading.Thread(target=hold_lease)
    writer = threading.Thread(target=replace_account)
    reader.start(); assert entered.wait(2); writer.start()
    try:
        assert not changed.wait(0.2)
    finally:
        release.set()
    reader.join(3); writer.join(3)
    assert changed.is_set()
    assert first.account('o', 'default')['cookies'] == LOGIN_B['cookies']
    service.close()


def test_settings_revision_prevents_lost_permission_update(services):
    service, repo, remote = services
    service.import_login('o', LOGIN)
    account = repo.account('o')
    service.settings('o', ['main'], expected_revision=account['settings_revision'])
    with pytest.raises(MuseError, match='其他窗口'):
        service.settings('o', ['side'], expected_revision=account['settings_revision'])
    assert repo.account('o')['settings']['auto_approve_sessions'] == ['main']


def test_full_access_is_explicit_per_account_and_never_survives_reimport(services):
    service, repo, remote = services
    service.import_login('o', LOGIN_A, 'account-a', 'A')
    service.import_login('o', LOGIN_B, 'account-b', 'B')
    assert repo.account('o', 'account-a')['settings'] == {
        'auto_approve_sessions': [], 'auto_approve_all': False,
    }
    assert not repo.enabled_accounts()

    first = repo.account('o', 'account-a')
    service.settings('o', account_id='account-a', expected_revision=first['settings_revision'],
                     expected_connection_revision=first['revision'], auto_approve_all=True)
    first = repo.account('o', 'account-a')
    repo.update_session_setting('o', 'account-a', 'side', True, first['settings_revision'], first['revision'])
    assert repo.account('o', 'account-a')['settings'] == {
        'auto_approve_sessions': ['side'], 'auto_approve_all': True,
    }
    assert repo.account('o', 'account-b')['settings']['auto_approve_all'] is False
    assert repo.enabled_accounts() == [{'owner': 'o', 'account_id': 'account-a'}]

    remote.pending = [{'approval_id': 'background', 'message_revision': 9, 'status': 'pending'}]
    service.auto_approve_once('o', 'account-b')
    assert not remote.decisions
    service.auto_approve_once('o', 'account-a')
    assert [entry[1]['expected_message_revision'] for entry in remote.decisions] == [9]

    current = repo.account('o', 'account-a')
    service.settings('o', account_id='account-a', expected_revision=current['settings_revision'],
                     expected_connection_revision=current['revision'], auto_approve_all=False)
    remote.pending = [{'approval_id': 'background-2', 'message_revision': 10, 'status': 'pending'}]
    service.auto_approve_once('o', 'account-a')
    assert len(remote.decisions) == 1

    service.import_login('o', LOGIN_A, 'account-a', 'A')
    assert repo.account('o', 'account-a')['settings'] == {
        'auto_approve_sessions': [], 'auto_approve_all': False,
    }


def test_automatic_approval_claim_rechecks_current_settings_atomically(services):
    service, repo, remote = services
    service.import_login('o', LOGIN)
    account = repo.account('o')
    service.settings('o', ['main'], expected_revision=account['settings_revision'])
    current = repo.account('o')
    repo.settings('o', {'auto_approve_sessions': []}, 'default', current['settings_revision'], current['revision'])
    with pytest.raises(MuseError, match='自动审批设置已更改'):
        repo.claim('o', 'approval:a1:2', 'auto_approve', {'decision': 'allow_once'}, 'default',
                   current['revision'], 'main', current['settings_revision'])
    assert repo.audit('o') == []


def test_full_access_approval_claim_rejects_revoked_settings(services):
    service, repo, remote = services
    service.import_login('o', LOGIN)
    first = repo.account('o')
    repo.settings('o', {'auto_approve_all': True}, 'default', first['settings_revision'], first['revision'])
    authorized = repo.account('o')
    repo.settings('o', {'auto_approve_all': False}, 'default', authorized['settings_revision'], authorized['revision'])
    with pytest.raises(MuseError, match='自动审批设置已更改'):
        repo.claim('o', 'approval:background:4', 'auto_approve', {'decision': 'allow_once'}, 'default',
                   authorized['revision'], expected_settings_revision=authorized['settings_revision'],
                   required_auto_approve_all=True)
    assert repo.audit('o') == []


def test_history_and_cancel_do_not_share_network_lock(services):
    service, repo, remote = services
    service.import_login('o', LOGIN)
    entered, release, dispatched = threading.Event(), threading.Event(), threading.Event()
    original = remote.request
    def slow(method, path, params=None, **kwargs):
        if path == '/chat/history':
            entered.set(); assert release.wait(5)
        if path == '/chat/cancel':
            dispatched.set()
        return original(method, path, params, **kwargs)
    remote.request = slow
    reader = threading.Thread(target=lambda: service.history('o', 'main'))
    stopper = threading.Thread(target=lambda: service.cancel('o', 'main', 'stop'))
    reader.start(); assert entered.wait(2); stopper.start()
    try:
        assert dispatched.wait(1)
    finally:
        release.set(); reader.join(3); stopper.join(3)


def test_history_catchup_paginates_beyond_one_page(services):
    service, repo, remote = services
    service.import_login('o', LOGIN)
    remote.events['main'] = [dict(seq=i, message_id=f'm{i}', role='assistant', event_name='message.assistant', payload={})
                             for i in range(1, 181)]
    result = service.history('o', 'main', after=60)
    sequences = [item['seq'] for item in result['chat_events']]
    assert sequences == list(range(61, 181))
    assert result['has_more_after'] is False
    assert result['has_more_before'] is True


def test_history_catchup_from_first_event_has_no_older_page(services):
    service, repo, remote = services
    service.import_login('o', LOGIN)
    remote.events['main'] = [dict(seq=i, message_id=f'm{i}', role='assistant', event_name='message.assistant', payload={})
                             for i in range(1, 61)]
    result = service.history('o', 'main', after=1)
    assert result['has_more_before'] is False


def test_unexpected_mutation_error_becomes_unknown_receipt(services):
    service, repo, remote = services
    service.import_login('owner', LOGIN)
    original = remote.request
    def broken(method, path, params=None, **kwargs):
        if path == '/chat/stream':
            raise RuntimeError('fixture failure')
        return original(method, path, params, **kwargs)
    remote.request = broken
    result = service.send('owner', 'side', 'unexpected', 'unexpected-error')
    assert result['state'] == 'unknown'
    assert result['result']['error'] == 'muse_internal'


def test_history_catchup_returns_continuation_for_more_than_ten_pages(services):
    service, repo, remote = services
    service.import_login('o', LOGIN)
    remote.events['main'] = [dict(seq=i, message_id=f'm{i}', role='assistant', event_name='message.assistant', payload={})
                             for i in range(1, 1002)]
    result = service.history('o', 'main', after=1)
    assert result['has_more_after'] is True
    assert result['continuation_before_seq'] == min(item['seq'] for item in result['chat_events'])


def test_muse_expiry_is_not_fleet_auth_failure(tmp_path):
    app, service, remote = app_with_remote(tmp_path)
    service.import_login('owner', LOGIN)
    remote.request = lambda *args, **kwargs: (_ for _ in ()).throw(MuseError('muse_login_expired', 'expired', 409))
    client = app.test_client()
    response = client.get('/api/platform/v1/muse/sessions')
    assert response.status_code == 409 and response.json['error'] == 'muse_login_expired'
    assert client.get('/api/operator/session').status_code == 200
    service.close()


def test_new_side_chat_and_reply_use_native_session_scope(services):
    service, repo, remote = services
    service.import_login('o', LOGIN)
    assert service.send('o', 'new-side', 'test', 'id1', 'm1')['state'] == 'confirmed'
    assert remote.sent[0]['session_id'] == 'new-side'
    assert remote.sent[0]['reply_to_message_id'] == 'm1'
    assert service.history('o', 'new-side')['chat_events'][-1]['display_text'] == 'FLEET_OK'
    assert service.send('o', 'new-side', 'test', 'id1', 'm1')['state'] == 'confirmed'
    assert len(remote.sent) == 1


def test_auto_approval_is_scoped_durable_and_audited(services):
    service, repo, remote = services
    service.import_login('o', LOGIN)
    remote.pending.extend([{'approval_id': 'other', 'status': 'pending', 'session_id': 'main'},
                           {'approval_id': 'background', 'status': 'pending'}])
    service.settings('o', ['side'], expected_revision=repo.account('o')['settings_revision']); service.auto_approve_once('o'); service.auto_approve_once('o')
    assert len(remote.decisions) == 1
    path, decision = remote.decisions[0]
    assert path == '/approvals/a1/decide'
    assert decision['decision'] == 'allow_once' and decision['expected_message_revision'] == 2
    assert service.status('o')['audit'][0]['action'] == 'auto_approve'
    assert [a['approval_id'] for a in remote.pending] == ['other', 'background']
    service.settings('o', [], expected_revision=repo.account('o')['settings_revision'])
    with pytest.raises(MuseError): service.settings('o', ['another-account-session'])


def test_two_workers_cannot_dispatch_same_request_twice(services):
    service, repo, remote = services
    service.import_login('o', LOGIN)
    other = MuseService(MuseRepository(repo.directory), lambda _: remote)
    threads = [threading.Thread(target=s.send, args=('o','side','test','same')) for s in (service, other)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert len(remote.sent) == 1


def app_with_remote(tmp_path):
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator='owner', platform_enabled=True, frontend_dir=ROOT/'frontend'))
    remote = FakeMuse(LOGIN['cookies'])
    service = app.extensions['fleet']['services']['muse']
    service.client_factory = lambda _: remote
    return app, service, remote


def test_routes_require_identity_origin_and_hide_credentials(tmp_path):
    app, service, remote = app_with_remote(tmp_path)
    client = app.test_client(); prefix = '/api/platform/v1/muse'
    assert client.get('/muse').status_code == 200
    assert client.post(prefix+'/login', json={'login': LOGIN}).status_code == 403
    result = client.post(prefix+'/login', json={'login': LOGIN}, headers={'Origin':'http://localhost'})
    assert result.status_code == 200, result.json
    assert b'fixture-secret' not in result.data
    assert result.headers['Cache-Control'] == 'no-store'
    assert client.get(prefix+'/sessions').json['sessions']
    assert client.get(prefix+'/sessions/main/history?before=bad').status_code == 400
    app.config['DEV_OPERATOR'] = None
    assert client.get(prefix+'/status').status_code == 401
    service.close()


def test_stale_account_revision_cannot_send_or_disconnect_new_login(tmp_path):
    app, service, remote = app_with_remote(tmp_path)
    client = app.test_client(); prefix = '/api/platform/v1/muse'; headers = {'Origin': 'http://localhost'}
    try:
        first = client.post(prefix+'/login', json={'login': LOGIN}, headers=headers).json
        second = client.post(prefix+'/login', json={'login': LOGIN}, headers=headers).json
        assert first['account_id'] == second['account_id'] == 'default'
        stale = first['connection_revision']
        sent = client.post(prefix+'/sessions/main/messages', json={
            'text':'stale tab', 'request_id':'stale-tab', 'account_id':'default', 'connection_revision':stale,
        }, headers=headers)
        disconnected = client.post(prefix+'/disconnect', json={
            'account_id':'default', 'connection_revision':stale,
        }, headers=headers)
        assert sent.status_code == disconnected.status_code == 409
        assert sent.json['error'] == disconnected.json['error'] == 'muse_connection_changed'
        assert not remote.sent and service.status('owner')['connected']
    finally:
        service.close()


def test_muse_workspace_in_browser(tmp_path):
    if not os.environ.get('FLEET_PLAYWRIGHT_MODULE'):
        pytest.skip('FLEET_PLAYWRIGHT_MODULE is required for UI acceptance')
    app, service, remote = app_with_remote(tmp_path)
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        result = subprocess.run(['node', str(ROOT/'tests/fixtures/browser/muse_workspace.cjs'),
                                 'http://127.0.0.1:'+str(server.server_port)], capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(remote.sent) == 2
        assert remote.decisions and remote.decisions[0][1]['decision'] == 'allow_once'
    finally:
        service.close(); server.shutdown(); server.server_close(); thread.join(5)


def test_automatic_permissions_respect_account_revocation(services):
    service, repo, remote = services
    service.import_login('o', LOGIN)
    repo.settings('o', {'auto_approve_sessions':['side']})
    service.owner_enabled = lambda owner: False
    service.auto_approve_once('o')
    assert not remote.decisions


def test_automatic_permissions_use_structured_thread_scope(services):
    service, repo, remote = services
    service.import_login('o', LOGIN)
    remote.pending[0]['scope'] = {'thread_id':'side'}
    repo.settings('o', {'auto_approve_sessions':['side']})
    service.auto_approve_once('o')
    assert len(remote.decisions) == 1


def test_workspace_resources_are_bounded_and_paths_cannot_escape(services):
    import base64
    service, repo, remote = services
    service.import_login('o', LOGIN)
    original=remote.request
    png=b'\x89PNG\r\n\x1a\n'+b'fixture'
    def read(method,path,params=None,**kwargs):
        if path=='/fs/read':
            assert params['path']=='workspace/image.png'
            return {'data_base64':base64.b64encode(png).decode(),'len':len(png),'eof':True}
        return original(method,path,params,**kwargs)
    remote.request=read
    assert service.resource('o','sandbox://workspace/image.png')['mime']=='image/png'
    for path in ['workspace/../secret','sandbox://workspace/%2e%2e/secret','workspace/%252e%252e/a','/etc/passwd','https://evil/a','workspace/a%2fb']:
        with pytest.raises(MuseError):service.resource('o',path)


def test_concurrent_first_use_never_observes_partial_key(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from cryptography.fernet import Fernet
    entered, release = threading.Event(), threading.Event()
    generate = Fernet.generate_key
    def delayed_key():
        if threading.current_thread().name.endswith('_0'):
            entered.set()
            assert release.wait(5)
        return generate()
    monkeypatch.setattr(Fernet, 'generate_key', delayed_key)
    directory = tmp_path / 'muse'
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(MuseRepository(directory).import_login, 'first', LOGIN['cookies'])
        assert entered.wait(5)
        second = pool.submit(MuseRepository(directory).import_login, 'second', LOGIN['cookies'])
        try:
            second.result(5)
        finally:
            release.set()
        first.result(5)
    restarted = MuseRepository(directory)
    assert restarted.account('first')['cookies'] == LOGIN['cookies']
    assert restarted.account('second')['cookies'] == LOGIN['cookies']


@pytest.mark.parametrize('change', ['disconnect', 'reimport', 'disable'])
def test_auto_approval_rechecks_login_and_settings_after_read(services, change):
    service, repo, remote = services
    service.import_login('o', LOGIN)
    repo.settings('o', {'auto_approve_sessions': ['side']})
    original = remote.request
    def changed(method, path, params=None, **kwargs):
        result = original(method, path, params, **kwargs)
        if path == '/approvals':
            if change == 'disconnect': repo.disconnect('o')
            elif change == 'reimport': repo.import_login('o', LOGIN['cookies'])
            else: repo.settings('o', {'auto_approve_sessions': []})
        return result
    remote.request = changed
    service.auto_approve_once('o')
    assert not remote.decisions


def test_full_access_revoked_after_approval_read_is_not_dispatched(services):
    service, repo, remote = services
    service.import_login('o', LOGIN)
    account = repo.account('o')
    repo.settings('o', {'auto_approve_all': True}, 'default', account['settings_revision'], account['revision'])
    original_claim = repo.claim

    def revoke_before_claim(*args, **kwargs):
        current = repo.account('o')
        repo.settings('o', {'auto_approve_all': False}, 'default', current['settings_revision'], current['revision'])
        return original_claim(*args, **kwargs)

    repo.claim = revoke_before_claim
    service.auto_approve_once('o')
    assert not remote.decisions
    assert repo.audit('o') == []


def test_account_users_have_separate_muse_logins_and_revocation(tmp_path):
    origin, password = 'https://fleet.example.test', 'Muse-test-password-123!'
    app = create_app(FleetConfig.from_root(tmp_path, platform_enabled=True, frontend_dir=ROOT/'frontend',
        accounts={'enabled': True, 'origin': origin, 'registration': 'open', 'sender': lambda *a: None}))
    accounts = app.extensions['accounts']
    service = app.extensions['fleet']['services']['muse']
    remote = FakeMuse(LOGIN['cookies']); service.client_factory = lambda _: remote
    accounts.bootstrap_admin('admin@example.test', password)
    admin = next(u for u in accounts.users() if u['role'] == 'admin')
    clients, users = [], []
    for email in ['a@example.test', 'b@example.test']:
        mail = []
        accounts.issue_code(email, 'register', 'open', lambda *a: mail.append(a))
        accounts.complete_code(email, 'register', mail[0][2], password, registration='open')
        client = app.test_client()
        result = client.post('/api/accounts/login', json={'email': email, 'password': password},
                             base_url=origin, headers={'Origin': origin})
        assert result.status_code == 200
        clients.append(client); users.append(next(u for u in accounts.users() if u['email'] == email))
    a, b = clients; prefix = '/api/platform/v1/muse'
    try:
        assert a.get('/muse', base_url=origin).status_code == 200
        assert a.post(prefix+'/login', json={'login': LOGIN}, base_url=origin, headers={'Origin':origin}).status_code == 200
        assert not b.get(prefix+'/status', base_url=origin).json['connected']
        assert b.get(prefix+'/sessions/main/history', base_url=origin).status_code == 409
        assert a.get(prefix+'/sessions', base_url=origin).status_code == 200
        service.repository.settings(users[0]['id'], {'auto_approve_sessions':['side']})
        accounts.manage(admin['id'], users[0]['id'], changes={'active':False}, expected_revision=users[0]['revision'])
        service.auto_approve_once(users[0]['id'])
        assert not remote.decisions
        assert a.get(prefix+'/status', base_url=origin).status_code == 401
        assert b.get(prefix+'/status', base_url=origin).status_code == 200
    finally:
        service.close()


def test_deactivation_fences_cross_process_automatic_approval_dispatch(tmp_path):
    origin, password = 'https://fleet.example.test', 'Muse-test-password-123!'
    app = create_app(FleetConfig.from_root(tmp_path, platform_enabled=True, frontend_dir=ROOT/'frontend',
        accounts={'enabled': True, 'origin': origin, 'registration': 'open', 'sender': lambda *a: None}))
    accounts = app.extensions['accounts']
    service = app.extensions['fleet']['services']['muse']
    remote = FakeMuse(LOGIN['cookies'])
    service.client_factory = lambda _: remote
    accounts.bootstrap_admin('admin@example.test', password)
    admin = next(user for user in accounts.users() if user['role'] == 'admin')
    mail = []
    accounts.issue_code('auto@example.test', 'register', 'open', lambda *a: mail.append(a))
    accounts.complete_code('auto@example.test', 'register', mail[0][2], password, registration='open')
    _, user = accounts.login('auto@example.test', password)
    service.import_login(user['id'], LOGIN)
    service.repository.settings(user['id'], {'auto_approve_sessions': ['side']})

    dispatch_entered, release_dispatch = threading.Event(), threading.Event()
    original_request = remote.request

    def blocked_decision(method, path, params=None, **kwargs):
        if path.endswith('/decide'):
            dispatch_entered.set()
            assert release_dispatch.wait(5)
        return original_request(method, path, params, **kwargs)

    remote.request = blocked_decision
    first = threading.Thread(target=service.auto_approve_once, args=(user['id'],))
    first.start()
    assert dispatch_entered.wait(5)
    disabled = []
    manager = threading.Thread(target=lambda: disabled.append(accounts.manage(
        admin['id'], user['id'], changes={'active': False}, expected_revision=user['revision'])))
    manager.start()
    deadline = time.monotonic() + 5
    barrier_ready = False
    while time.monotonic() < deadline:
        with accounts.connect() as db:
            barrier_ready = db.execute(
                'SELECT 1 FROM background_operation_barriers WHERE user_id=?', (user['id'],),
            ).fetchone() is not None
        if barrier_ready:
            break
        time.sleep(0.01)
    assert barrier_ready and accounts.is_active(user['id'])

    parallel_service = MuseService(
        MuseRepository(service.repository.directory), lambda _: remote,
        owner_enabled=accounts.is_active, owner_operation=accounts.background_operation)
    try:
        parallel_service.auto_approve_once(user['id'])
        assert not remote.decisions
        assert manager.is_alive()
    finally:
        release_dispatch.set()
        first.join(5)
        manager.join(5)
        parallel_service.close()
        service.close()
    assert not first.is_alive() and not manager.is_alive()
    assert len(remote.decisions) == 1
    assert disabled and not disabled[0]['active']


@pytest.mark.parametrize('chunk', [
    {'data_base64':'invalid!', 'len':1, 'eof':True},
    {'data_base64':'YQ==', 'len':2, 'eof':True},
    {'data_base64':'', 'len':0, 'eof':False},
    {'data_base64':base64.b64encode(b'x'*(1024*1024+1)).decode(), 'len':1024*1024+1, 'eof':True},
])
def test_malformed_resource_reads_fail_without_returning_partial_content(services, chunk):
    service, repo, remote = services
    service.import_login('o', LOGIN)
    remote.request = lambda *a, **k: chunk
    with pytest.raises(MuseError, match='Muse 文件'):
        service.resource('o', 'workspace/file.bin')


@pytest.mark.parametrize('chunk', [None, [], {'data_base64':'YQ==', 'len':True, 'eof':True}])
def test_resource_requires_well_typed_chunk_envelope(services, chunk):
    service, repo, remote = services
    service.import_login('o', LOGIN)
    remote.request = lambda *a, **k: chunk
    with pytest.raises(MuseError, match='Muse 文件'):
        service.resource('o', 'workspace/file.bin')


def test_resource_total_limit_stops_upstream_reads(services):
    service, repo, remote = services
    service.import_login('o', LOGIN)
    offsets = []
    def read(method, path, params, **kwargs):
        offsets.append(params['offset'])
        return {'data_base64':base64.b64encode(b'x'*(1024*1024)).decode(), 'len':1024*1024, 'eof':False}
    remote.request = read
    with pytest.raises(MuseError) as error:
        service.resource('o', 'workspace/large.bin')
    assert error.value.status == 413 and len(offsets) == 9
