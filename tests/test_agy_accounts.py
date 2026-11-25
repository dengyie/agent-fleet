"""Owned AGY profiles and actual fixture login processes (no vendor/network)."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.providers.agy_accounts import AgyAccounts
from tools.providers.agy_auth import AgyAccountControl

A = 'pa_' + 'a' * 32
B = 'pa_' + 'b' * 32
OWNER = hashlib.sha256(b'operator@example.test').hexdigest()


def test_profiles_never_share_or_inherit_google_credentials(tmp_path):
    accounts = AgyAccounts(tmp_path / 'accounts')
    source = {'HOME': str(tmp_path), 'PATH': '/bin', 'GEMINI_HOME': '/ambient',
              'GOOGLE_API_KEY': 'ambient', 'google_api_key': 'ambient',
              'GOOGLE_APPLICATION_CREDENTIALS': '/private.json', 'GEMINI_API_KEY': 'ambient',
              'AGENT_FLEET_TOKEN': 'hub-secret', 'SSH_CONNECTION': 'untrusted',
              'BROWSER': 'open', 'DISPLAY': ':0'}
    a, b = (accounts.environment(identifier, source) for identifier in (A, B))
    assert a['GEMINI_HOME'] != b['GEMINI_HOME']
    assert Path(a['GEMINI_HOME']).parent.name == hashlib.sha256(A.encode()).hexdigest()
    assert not {'GOOGLE_API_KEY', 'google_api_key', 'GOOGLE_APPLICATION_CREDENTIALS',
                'GEMINI_API_KEY', 'AGENT_FLEET_TOKEN', 'DISPLAY'} & a.keys()
    assert a['SSH_CONNECTION'] == '127.0.0.1 0 127.0.0.1 0'
    assert a['AGY_ACP_FORCE_FILE_STORAGE'] == '1'
    assert a['AGY_CLI_DISABLE_AUTO_UPDATE'] == 'true'
    assert a['BROWSER'] != 'open'


@pytest.mark.parametrize('winerror', [5, 32, 33])
def test_state_publish_retries_transient_windows_read_sharing(tmp_path, monkeypatch, winerror):
    from tools.providers import agy_accounts
    accounts = AgyAccounts(tmp_path / 'accounts')
    accounts.apply(A, 'create', 1, owner_digest=OWNER)
    original = os.replace
    calls = []
    def replace(source, target):
        if Path(target).name == 'state.json':
            calls.append(str(source))
            if len(calls) < 3:
                error = PermissionError('fixture sharing contention')
                error.winerror = winerror
                raise error
        return original(source, target)
    monkeypatch.setattr(agy_accounts.os, 'replace', replace)
    accounts.apply(A, 'disable', 2, owner_digest=OWNER)
    assert len(calls) == 3 and len(set(calls)) == 1
    assert accounts.read(A)['state'] == 'disabled'


def test_runtime_requires_ready_bound_owner_and_generation_fences(tmp_path):
    accounts = AgyAccounts(tmp_path / 'accounts')
    with pytest.raises(ValueError, match='provider_account_unavailable'):
        with accounts.runtime(A):
            pass
    accounts.apply(A, 'create', 1, owner_digest=OWNER)
    with pytest.raises(ValueError, match='provider_account_not_ready'):
        with accounts.runtime(A, owner_digest=OWNER):
            pass
    accounts.apply(A, 'login', 2, owner_digest=OWNER)
    accounts.set_state(A, 2, 'ready')
    with pytest.raises(ValueError, match='provider_account_owner_mismatch'):
        with accounts.runtime(A, owner_digest='0' * 64):
            pass
    with accounts.runtime(A, owner_digest=OWNER) as lease:
        assert not lease.cancelled()
        accounts.apply(A, 'disable', 3, owner_digest=OWNER)
        assert lease.cancelled()
    assert accounts.read(A)['state'] == 'disabled'
    accounts.apply(A, 'enable', 4, owner_digest=OWNER)
    with accounts.runtime(A, owner_digest=OWNER) as lease:
        assert not lease.cancelled()
    with pytest.raises(ValueError, match='provider_account_stale_operation'):
        accounts.apply(A, 'logout', 3, owner_digest=OWNER)


def test_account_mutation_is_isolated_and_disable_preserves_profile(tmp_path):
    accounts = AgyAccounts(tmp_path / 'accounts')
    for identifier in (A, B):
        accounts.apply(identifier, 'create', 1, owner_digest=OWNER)
        accounts.apply(identifier, 'login', 2, owner_digest=OWNER)
        accounts.set_state(identifier, 2, 'ready')
    marker = accounts.profile(A) / 'vendor-token.json'
    marker.write_text('private-token', encoding='utf-8')
    with accounts.runtime(B, owner_digest=OWNER) as other:
        accounts.apply(A, 'disable', 3, owner_digest=OWNER)
        assert not other.cancelled()
    assert marker.read_text(encoding='utf-8') == 'private-token'
    assert accounts.apply(A, 'disable', 3, owner_digest=OWNER)['state'] == 'disabled'
    with pytest.raises(ValueError, match='provider_account_operation_conflict'):
        accounts.apply(A, 'enable', 3, owner_digest=OWNER)


def test_invalid_and_corrupt_state_fail_closed(tmp_path):
    accounts = AgyAccounts(tmp_path / 'accounts')
    for value in ('../escape', '', 'pa_' + 'A' * 32, None):
        with pytest.raises(ValueError):
            accounts.profile(value)
    accounts.apply(A, 'create', 1, owner_digest=OWNER)
    state = accounts._directory(A) / 'state.json'
    state.write_text('{broken', encoding='utf-8')
    with pytest.raises(ValueError, match='provider_account_store'):
        accounts.read(A)


def test_profile_symlinks_are_refused(tmp_path):
    accounts = AgyAccounts(tmp_path / 'owned')
    outside = tmp_path / 'outside'
    outside.mkdir()
    profile = accounts.profile(A)
    # Empty directory created by environment is replaceable in this fixture.
    if profile.exists():
        profile.rmdir()
    try:
        profile.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('symlink creation unavailable')
    with pytest.raises(ValueError, match='provider_account_store'):
        accounts.environment(A, {})


def _fixture(tmp_path, *, mode='normal'):
    script = tmp_path / ('fixture-' + mode + '.py')
    script.write_text('MODE = ' + repr(mode) + '\n' + '''import json, os, sys, pathlib, time
profile = pathlib.Path(os.environ['GEMINI_HOME'])
with (profile/'fixture-calls.jsonl').open('a',encoding='utf-8') as log:
    log.write(json.dumps(sys.argv[1:])+'\\n')
if '--login' in sys.argv:
    if MODE == 'early_exit': raise SystemExit(3)
    if MODE == 'no_url': time.sleep(30)
    if MODE == 'oversized': print('x'*8193,flush=True); time.sleep(30)
    if MODE == 'invalid_utf8': sys.stdout.buffer.write(b'\\xff\\n'); sys.stdout.flush(); time.sleep(30)
    if MODE == 'prefix':
        print('Status: waiting for Google login',flush=True)
        sys.stderr.write('fixture diagnostic\\n'*20000); sys.stderr.flush()
    if MODE == 'stderr_flood': sys.stderr.write('x'*1100000); sys.stderr.flush(); time.sleep(30)
    if MODE == 'bad_url': print('https://evil.invalid/oauth?token=secret',flush=True); time.sleep(30)
    print('https://accounts.google.com/o/oauth2/auth?redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback&response_type=code&state=fixture-state', flush=True)
    value = sys.stdin.readline().strip()
    if value != 'fixture-code': raise SystemExit(4)
    (profile / 'vendor-token.json').write_text('private-token', encoding='utf-8')
    if MODE == 'after_write_failure': raise SystemExit(6)
    raise SystemExit(0)
for line in sys.stdin:
    row=json.loads(line); method=row['method']
    with (profile/'fixture-methods.jsonl').open('a',encoding='utf-8') as log:
        log.write(json.dumps(method)+'\\n')
    if method == 'initialize': result={'protocolVersion':1,'agentCapabilities':{'auth':{'logout':MODE != 'logout_unsupported'}}}
    elif method == 'authenticate':
        if MODE == 'verify_failure' or not (profile/'vendor-token.json').exists():
            print(json.dumps({'jsonrpc':'2.0','id':row['id'],'error':{'code':-32000,'message':'secret-failure'}}),flush=True); continue
        result={}
    elif method == 'session/new': result={'sessionId':'fixture-session','models':{'currentModelId':'fixture-model'}}
    elif method == 'logout':
        (profile/'vendor-token.json').unlink(missing_ok=True); result={}
    else: raise SystemExit(5)
    print(json.dumps({'jsonrpc':'2.0','id':row['id'],'result':result}),flush=True)
''', encoding='utf-8')
    return SimpleNamespace(supervisor_manifest_dir=tmp_path / 'supervisor', machine='fixture',
        agents={'agy': {'command': [sys.executable, str(script)]}})


class _Hub:
    def __init__(self, control):
        self.control, self.configs, self.reports = control, {}, []
        self.offline, self.accepted = False, True

    def transport(self, path, body, _headers):
        if path.endswith('/resolve'):
            return 200, {'configuration': self.configs[body['command_id']]}
        assert path.endswith('/report')
        self.reports.append(dict(body))
        return (503, {}) if self.offline else (200, {'ok': True, 'accepted': self.accepted})

    def command(self, operation, revision, *, identifier=A, owner=OWNER, **fields):
        cid = 'cmd_' + f'{len(self.configs)+1:032x}'
        payload = {'account_id': identifier, 'revision': revision, 'operation': operation, 'owner_digest': owner}
        self.configs[cid] = {**payload, **fields}
        command = {'action': 'manage_provider_account', 'command_id': cid,
                   'target': {'session_id': identifier, 'attempt_id': None}, 'payload': payload}
        return command

    def execute(self, operation, revision, **fields):
        command = self.command(operation, revision, **fields)
        return command, self.control.execute(command, self.transport, {})


def _state(control, expected, *, identifier=A):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        state = control.accounts.read(identifier)
        if state['state'] == expected:
            return state
        time.sleep(.02)
    pytest.fail(f'account state {state}, expected {expected}')


def _login(hub, *, identifier=A):
    command, result = hub.execute('login', 2, identifier=identifier, expires_at=time.time()+120)
    assert result == ('succeeded', 'login_started')
    _state(hub.control, 'waiting', identifier=identifier)
    # Local state commits before the asynchronous HTTP report; wait for that
    # independently observable boundary rather than racing a slower filesystem.
    deadline = time.monotonic() + 6
    while not any(row['account_id'] == identifier and row['state'] == 'waiting'
                  for row in hub.reports):
        if time.monotonic() >= deadline:
            pytest.fail('waiting report not delivered')
        time.sleep(.02)
    return command


@pytest.mark.parametrize('mode', ['normal', 'prefix'])
def test_actual_personal_login_disable_enable_logout_preserves_history(tmp_path, mode):
    control = AgyAccountControl(_fixture(tmp_path, mode=mode))
    hub = _Hub(control)
    try:
        assert hub.execute('create', 1)[1] == ('succeeded', 'created')
        login = _login(hub)
        waiting = next(r for r in hub.reports if r['state'] == 'waiting')
        assert waiting['expires_at'] == hub.configs[login['command_id']]['expires_at']
        submit, result = hub.execute('submit_login', 3, login_command_id=login['command_id'], response='fixture-code')
        assert result == ('succeeded', 'submitted')
        assert control.execute(submit, hub.transport, {})[0] == 'succeeded'
        _state(control, 'ready')
        profile = control.accounts.profile(A)
        history = profile / 'history.json'
        history.write_text('user history', encoding='utf-8')
        assert hub.execute('disable', 4)[1] == ('succeeded', 'disabled')
        assert (profile/'vendor-token.json').exists()
        assert hub.execute('enable', 5)[1] == ('succeeded', 'enabled')
        assert control.accounts.read(A)['state'] == 'ready'
        assert hub.execute('logout', 6)[1] == ('succeeded', 'signed_out')
        assert not (profile/'vendor-token.json').exists()
        assert history.read_text(encoding='utf-8') == 'user history'
        assert hub.execute('enable', 7)[1] == ('succeeded', 'enabled')
        assert control.accounts.read(A)['state'] == 'signed_out'
        calls = [json.loads(line) for line in (profile/'fixture-calls.jsonl').read_text().splitlines()]
        assert calls.count(['--login']) == 1
        methods = [json.loads(line) for line in (profile/'fixture-methods.jsonl').read_text().splitlines()]
        assert methods == ['initialize', 'authenticate', 'session/new', 'initialize', 'logout']
        assert any(r['state']=='ready' and r['command_id']==submit['command_id'] and r['revision']==3 for r in hub.reports)
        for path in control.accounts.root.rglob('*.json'):
            assert 'fixture-code' not in path.read_text(encoding='utf-8')
    finally:
        control.close()


@pytest.mark.parametrize('mode,expected', [
    ('early_exit', 'login_failed'), ('no_url', 'login_expired'),
    ('oversized', 'login_failed'), ('invalid_utf8', 'login_failed'),
    ('stderr_flood', 'login_failed'), ('bad_url', 'invalid_authorization_url'),
])
def test_bounded_login_streams_fail_closed_and_reap_tree(tmp_path, mode, expected):
    control = AgyAccountControl(_fixture(tmp_path, mode=mode))
    control.login_timeout = .4
    hub = _Hub(control)
    try:
        hub.execute('create', 1)
        assert hub.execute('login', 2, expires_at=time.time()+60)[1][0] == 'succeeded'
        _state(control, 'failed')
        control.close()
        assert not control._flows
        assert hub.reports[-1]['error'] == expected
        assert 'secret' not in json.dumps(hub.reports)
        with pytest.raises(ValueError, match='provider_account_not_ready'):
            with control.accounts.runtime(A, owner_digest=OWNER):
                pass
    finally:
        control.close()


@pytest.mark.parametrize('mode', ['verify_failure', 'after_write_failure'])
def test_failed_login_with_written_credentials_still_runs_native_logout(tmp_path, mode):
    control = AgyAccountControl(_fixture(tmp_path, mode=mode))
    hub = _Hub(control)
    try:
        hub.execute('create', 1)
        login = _login(hub)
        assert hub.execute('submit_login', 3, login_command_id=login['command_id'], response='fixture-code')[1][0]=='succeeded'
        assert not _state(control, 'failed')['logged_in']
        marker = control.accounts.profile(A)/'vendor-token.json'
        assert marker.exists()
        assert hub.execute('logout', 4)[1] == ('succeeded', 'signed_out')
        assert not marker.exists()
    finally:
        control.close()


def test_failed_ready_reauthentication_logout_cleans_retained_credentials(tmp_path):
    cfg = _fixture(tmp_path)
    control, hub = AgyAccountControl(cfg), None
    hub = _Hub(control)
    try:
        hub.execute('create', 1)
        login = _login(hub)
        hub.execute('submit_login', 3, login_command_id=login['command_id'], response='fixture-code')
        _state(control, 'ready')
        control.close()
        cfg.agents = _fixture(tmp_path, mode='early_exit').agents
        assert hub.execute('login', 4, expires_at=time.time()+120)[1][0]=='succeeded'
        _state(control, 'failed')
        assert (control.accounts.profile(A)/'vendor-token.json').exists()
        assert hub.execute('logout', 5)[1] == ('succeeded', 'signed_out')
        assert not (control.accounts.profile(A)/'vendor-token.json').exists()
    finally:
        control.close()


def test_response_owner_flow_revision_binding_cancel_and_other_account_isolation(tmp_path):
    control = AgyAccountControl(_fixture(tmp_path))
    hub = _Hub(control)
    try:
        hub.execute('create', 1)
        hub.execute('create', 1, identifier=B)
        control.accounts.apply(B, 'login', 2, owner_digest=OWNER)
        control.accounts.set_state(B, 2, 'ready')
        login = _login(hub)
        for fields in (
            {'owner': '1'*64}, {'login_command_id': 'cmd_wrong'},
            {'response': 'https://antigravity.google/oauth-callback?state=wrong&code=fixture-code'},
            {'response': 'fixture-code\nsecond-line'},
        ):
            command, result = hub.execute('submit_login', 3,
                **({'login_command_id': login['command_id'], 'response': 'fixture-code'} | fields))
            assert result[0] == 'failed'
            assert 'fixture-code' not in result[1]
            assert control.accounts.read(A)['state'] == 'waiting'
        with control.accounts.runtime(B, owner_digest=OWNER) as lease:
            assert hub.execute('cancel_login', 3, login_command_id=login['command_id'])[1] == ('succeeded', 'cancelled')
            assert not lease.cancelled()
        assert control.accounts.read(A)['state'] == 'signed_out'
        assert not control._flows
        assert not (control.accounts.profile(A)/'vendor-token.json').exists()
        assert hub.execute('disable', 4)[1] == ('succeeded', 'disabled')
    finally:
        control.close()


def test_cancel_fences_login_before_launch(tmp_path):
    entered, resume = threading.Event(), threading.Event()
    class Supervisor:
        def stop_provider_account(self, identifier):
            entered.set()
            assert resume.wait(3)
            return True
    control = AgyAccountControl(_fixture(tmp_path), Supervisor())
    hub = _Hub(control)
    try:
        hub.execute('create', 1)
        login, result = hub.execute('login', 2, expires_at=time.time()+120)
        assert result[0] == 'succeeded' and entered.wait(2)
        done = []
        thread = threading.Thread(target=lambda: done.append(hub.execute('cancel_login', 3, login_command_id=login['command_id'])[1]))
        thread.start()
        _state(control, 'signed_out')
        resume.set()
        thread.join(3)
        assert done == [('succeeded', 'cancelled')]
        assert not (control.accounts.profile(A)/'fixture-calls.jsonl').exists()
    finally:
        resume.set()
        control.close()


def test_expired_login_rejected_before_local_state_mutation(tmp_path):
    control = AgyAccountControl(_fixture(tmp_path))
    hub = _Hub(control)
    hub.execute('create', 1)
    assert hub.execute('login', 2, expires_at=time.time()-1)[1] == ('failed', 'login_expired')
    assert control.accounts.read(A)['revision'] == 1
    assert control.accounts.read(A)['state'] == 'signed_out'
    control.close()


def test_native_logout_capability_required_and_credentials_preserved_on_failure(tmp_path):
    control = AgyAccountControl(_fixture(tmp_path, mode='logout_unsupported'))
    hub = _Hub(control)
    try:
        hub.execute('create', 1)
        login = _login(hub)
        hub.execute('submit_login', 3, login_command_id=login['command_id'], response='fixture-code')
        _state(control, 'ready')
        assert hub.execute('logout', 4)[1] == ('failed', 'provider_account_logout_unsupported')
        assert control.accounts.read(A)['state'] == 'failed'
        assert (control.accounts.profile(A)/'vendor-token.json').exists()
        assert hub.execute('enable', 5)[1] == ('succeeded', 'enabled')
        assert control.accounts.read(A)['state'] == 'signed_out'
        command, result = hub.execute('login', 6, expires_at=time.time()+60)
        assert result == ('succeeded', 'login_started')
        _state(control, 'waiting')
        assert hub.execute('cancel_login', 7, login_command_id=command['command_id'])[1][0] == 'succeeded'
    finally:
        control.close()


def test_durable_report_retries_after_restart_and_discards_stale_receipts(tmp_path):
    cfg = _fixture(tmp_path)
    first = AgyAccountControl(cfg)
    hub = _Hub(first)
    hub.offline = True
    hub.execute('create', 1)
    pending = first._report_path(A)
    original = pending.read_bytes()
    first.close()
    second = AgyAccountControl(cfg)
    try:
        second.tick(hub.transport, {})
        assert pending.read_bytes() == original
        hub.offline, hub.accepted = False, False
        second.tick(hub.transport, {})
        assert not pending.exists()
        assert hub.reports[-1]['state'] == 'signed_out'
    finally:
        second.close()


def test_restart_recovers_interrupted_flow_and_all_accounts_beyond_first_batch(tmp_path):
    cfg = _fixture(tmp_path)
    first = AgyAccountControl(cfg)
    hub = _Hub(first)
    identifiers = ['pa_' + f'{i:032x}' for i in range(205)]
    for identifier in identifiers:
        payload = {'account_id': identifier, 'revision': 2, 'operation': 'login', 'owner_digest': OWNER}
        first.accounts.apply(identifier, 'create', 1, owner_digest=OWNER)
        first.accounts.apply(identifier, 'login', 2, owner_digest=OWNER)
        first._save_operation(identifier, 'cmd_interrupted', payload, expires_at=time.time()+60)
    first.close()
    second = AgyAccountControl(cfg)
    try:
        for _ in range(4): second.tick(hub.transport, {})
        assert all(second.accounts.read(i)['state']=='failed' for i in identifiers)
        assert len(hub.reports)==205
        assert all(r['error']=='login_interrupted' for r in hub.reports)
    finally:
        second.close()


def test_second_live_controller_does_not_recover_or_kill_active_login(tmp_path):
    cfg = _fixture(tmp_path)
    first, second = AgyAccountControl(cfg), AgyAccountControl(cfg)
    hub = _Hub(first)
    try:
        hub.execute('create', 1)
        login = _login(hub)
        second.tick(hub.transport, {})
        assert first.accounts.read(A)['state'] == 'waiting'
        assert first._flows[A].child.proc.poll() is None
        assert hub.execute('submit_login', 3, login_command_id=login['command_id'], response='fixture-code')[1][0] == 'succeeded'
        _state(first, 'ready')
        assert not any(row.get('error')=='login_interrupted' for row in hub.reports)
    finally:
        first.close()
        second.close()


def test_repeated_login_command_does_not_spawn_again(tmp_path):
    control = AgyAccountControl(_fixture(tmp_path))
    hub = _Hub(control)
    try:
        hub.execute('create', 1)
        login = _login(hub)
        assert control.execute(login, hub.transport, {}) == ('succeeded', 'already_applied')
        assert hub.execute('submit_login', 3, login_command_id=login['command_id'], response='fixture-code')[1][0]=='succeeded'
        _state(control, 'ready')
        calls = [json.loads(line) for line in (control.accounts.profile(A)/'fixture-calls.jsonl').read_text().splitlines()]
        assert calls.count(['--login'])==1
    finally:
        control.close()


def test_real_controller_crash_releases_lock_and_recovers_owned_login_tree(tmp_path):
    cfg = _fixture(tmp_path)
    script, marker = tmp_path/'controller.py', tmp_path/'waiting'
    script.write_text('''import json,sys,time,pathlib
from types import SimpleNamespace
from tools.providers.agy_auth import AgyAccountControl
raw=json.loads(sys.argv[1])
control=AgyAccountControl(SimpleNamespace(**raw))
configs={}
def transport(path,body,headers):
    if path.endswith('/resolve'): return 200, {'configuration':configs[body['command_id']]}
    return 200, {'ok':True,'accepted':True}
for revision,operation in ((1,'create'),(2,'login')):
    payload={'account_id':sys.argv[2],'owner_digest':sys.argv[3],'revision':revision,'operation':operation}
    cid='cmd_'+str(revision)*32
    configs[cid]={**payload,'expires_at':time.time()+120}
    command={'action':'manage_provider_account','command_id':cid,'target':{'session_id':sys.argv[2]},'payload':payload}
    assert control.execute(command,transport,{})[0]=='succeeded'
deadline=time.monotonic()+6
while control.accounts.read(sys.argv[2])['state']!='waiting':
    if time.monotonic()>deadline: raise SystemExit(4)
    time.sleep(.02)
pathlib.Path(sys.argv[4]).write_text('waiting',encoding='utf-8')
time.sleep(30)
''', encoding='utf-8')
    config = {'supervisor_manifest_dir':str(cfg.supervisor_manifest_dir), 'machine':cfg.machine, 'agents':cfg.agents}
    proc = subprocess.Popen([sys.executable, str(script), json.dumps(config), A, OWNER, str(marker)],
        env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1])},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    restarted = None
    try:
        deadline = time.monotonic()+8
        while not marker.exists() and proc.poll() is None and time.monotonic()<deadline:
            time.sleep(.02)
        assert marker.exists(), proc.communicate(timeout=2)
        proc.kill()
        proc.wait(timeout=3)
        restarted = AgyAccountControl(cfg)
        hub = _Hub(restarted)
        restarted.tick(hub.transport,{})
        assert restarted.accounts.read(A)['state']=='failed'
        assert hub.reports[-1]['error']=='login_interrupted'
        assert not restarted.accounts._directory(A).joinpath('auth.lock.owner.json').exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)
        for stream in (proc.stdout,proc.stderr): stream.close()
        if restarted is not None: restarted.close()


def test_failed_logout_operation_journal_write_is_retryable_without_skipping_cleanup(tmp_path, monkeypatch):
    control = AgyAccountControl(_fixture(tmp_path))
    hub = _Hub(control)
    try:
        hub.execute('create', 1)
        login = _login(hub)
        hub.execute('submit_login', 3, login_command_id=login['command_id'], response='fixture-code')
        _state(control, 'ready')
        control.close()
        original = control._save_operation
        def broken(*args, **kwargs):
            raise ValueError('provider_account_store')
        monkeypatch.setattr(control,'_save_operation',broken)
        command,result = hub.execute('logout',4)
        assert result==('failed','provider_account_store')
        assert (control.accounts.profile(A)/'vendor-token.json').exists()
        monkeypatch.setattr(control,'_save_operation',original)
        assert control.execute(command,hub.transport,{})==('succeeded','signed_out')
        assert not (control.accounts.profile(A)/'vendor-token.json').exists()
        assert control.execute(command,hub.transport,{})==('succeeded','already_applied')
    finally:
        control.close()


def test_login_journal_failure_does_not_leave_an_unrecoverable_starting_account(tmp_path, monkeypatch):
    control = AgyAccountControl(_fixture(tmp_path))
    hub = _Hub(control)
    try:
        hub.execute('create', 1)
        original = control._save_operation
        def broken(*args, **kwargs):
            raise ValueError('provider_account_store')
        monkeypatch.setattr(control,'_save_operation',broken)
        command,result = hub.execute('login',2,expires_at=time.time()+60)
        assert result==('failed','provider_account_store')
        monkeypatch.setattr(control,'_save_operation',original)
        assert control.execute(command,hub.transport,{})==('failed','login_interrupted')
        assert control.accounts.read(A)['state']=='failed'
        assert not control._flows
    finally:
        control.close()
