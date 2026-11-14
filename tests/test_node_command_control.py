"""Command renewal and cancellation through real Node/process boundaries."""
from __future__ import annotations

from contextlib import contextmanager
from threading import Event, Thread, enumerate as threads
import sqlite3
import sys
import time

import pytest
from werkzeug.serving import make_server

from fleet_core.backends.base import ToolReceipt
from fleet_core.backends.directory import DirectoryBackend
from fleet_core.control import generate_ed25519_keypair
from fleet_core.command_timing import CommandTiming
from fleet_core.process import bounded_process
from fleet_core.platform_command import PlatformCommand
from hub.infrastructure.command_repository import CommandRepository, CommandRepositoryError
from tests.test_platform_node_http_e2e import OWNER, _app
from tools.platform.node_executor import NodeToolExecutor
from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig
from tools.transport import Transport
from tools.platform.command_control import CommandControl


PRIVATE_KEY, PUBLIC_KEY = generate_ed25519_keypair()
CONTROL = '/api/platform/v1/nodes/commands/control'


def command(*, expires_at=200, command_id='control-command', arguments=None):
    return PlatformCommand.create(command_id=command_id, target_node='control-node',
        owner_id=OWNER, resource_id='workspace', run_id='control-run',
        action='tool.workspace.exec', arguments=arguments or {'argv': ['pwd']},
        retry_class='reconcile_before_retry', expires_at=expires_at).signed(PRIVATE_KEY)


def test_command_control_renews_only_the_original_active_lease(tmp_path):
    now = [100.0]
    repo = CommandRepository(tmp_path / 'commands.db', clock=lambda: now[0])
    repo.init()
    original = command()
    repo.enqueue(original)
    repo.claim_for_node('control-node', 'worker', owner_id=OWNER, lease_s=2)
    now[0] = 101
    control = repo.renew_command(original.command_id, 'worker', owner_id=OWNER,
        node_id='control-node', lease_s=4)
    assert control == {'command_id': original.command_id, 'status': 'leased',
        'continue': True, 'execution_deadline': 200.0, 'valid_for_s': 4.0}
    saved = repo.get(original.command_id)
    assert saved['lease_until'] == 105 and saved['attempt'] == 1
    assert saved['signature'] == original.signature and saved['expires_at'] == 200
    now[0] = 106
    assert repo.renew_command(original.command_id, 'worker', owner_id=OWNER,
        node_id='control-node', lease_s=300)['continue'] is False
    assert repo.get(original.command_id)['status'] == 'unknown'


@pytest.mark.parametrize('scope', ['worker', 'owner', 'node'])
def test_control_cannot_take_another_execution_lease(tmp_path, scope):
    repo = CommandRepository(tmp_path / 'commands.db', clock=lambda: 100)
    repo.init()
    original = command()
    repo.enqueue(original)
    repo.claim_for_node('control-node', 'worker', owner_id=OWNER)
    before = repo.get(original.command_id)
    kwargs = {'worker_id': 'worker', 'owner_id': OWNER, 'node_id': 'control-node'}
    kwargs[{'worker': 'worker_id', 'owner': 'owner_id', 'node': 'node_id'}[scope]] = 'other'
    with pytest.raises(CommandRepositoryError):
        repo.renew_command(original.command_id, **kwargs)
    assert repo.get(original.command_id) == before


def test_authenticated_control_observes_cancel_without_rewriting_unknown(tmp_path):
    app = _app(tmp_path)
    platform = app.extensions['fleet']['platform_repository']
    platform.upsert_node(OWNER, {'node_id': 'control-node', 'label': 'Control'})
    credential = platform.provision_node_credential(OWNER, 'control-node', secret='x' * 40)['credential']
    repo = app.extensions['fleet']['services']['platform_delivery'].repository
    original = command(expires_at=time.time() + 20)
    repo.enqueue(original)
    repo.claim_for_node('control-node', 'worker', owner_id=OWNER)
    client = app.test_client()
    headers = {'X-Platform-Node-Credential': credential}
    body = {'command_id': original.command_id, 'worker_id': 'worker', 'lease_s': 2}
    response = client.post(CONTROL, json=body, headers=headers)
    assert response.status_code == 200
    assert response.json['control']['continue'] is True
    cancelled = repo.cancel_wait(original.command_id)
    response = client.post(CONTROL, json=body, headers=headers)
    assert response.status_code == 200 and response.json['control']['continue'] is False
    assert repo.get(original.command_id) == cancelled


def test_node_preserves_unconfirmed_process_stop_as_unknown(tmp_path):
    class Unconfirmed(DirectoryBackend):
        def execute(self, command_id, argv, **kwargs):
            return ToolReceipt(command_id, 'unknown', {}, 'process_stop_unconfirmed')

    executor = NodeToolExecutor(Unconfirmed(tmp_path), workspace_id='workspace')
    result = executor(command().as_dict())
    assert result['state'] == 'unknown'
    assert result['error_code'] == 'process_stop_unconfirmed'


def test_timed_control_never_resets_execution_or_receipt_deadlines(tmp_path):
    now = [100.0]
    repo = CommandRepository(tmp_path / 'commands.db', clock=lambda: now[0])
    repo.init()
    original = command(expires_at=117)
    repo.enqueue(original, timing=CommandTiming(105, 2, 10))
    repo.claim_for_node('control-node', 'worker', owner_id=OWNER, lease_s=1)
    original_lease = repo.get(original.command_id)['lease_until']
    timing = repo.get_timing(original.command_id)
    assert timing['execution_deadline'] == 102 and timing['receipt_deadline'] == 112
    now[0] = 101
    response = repo.renew_command(original.command_id, 'worker', owner_id=OWNER,
        node_id='control-node', lease_s=300)
    assert response['continue'] and response['execution_deadline'] == 102
    assert repo.get(original.command_id)['lease_until'] == original_lease
    assert repo.get_timing(original.command_id) == timing
    now[0] = 102
    assert repo.renew_command(original.command_id, 'worker', owner_id=OWNER,
        node_id='control-node')['continue'] is False
    assert repo.get_timing(original.command_id) == timing
    assert repo.record_receipt(original.command_id, 'failed', worker_id='worker',
        result={'error_code': 'cancelled'})['status'] == 'failed'


@pytest.mark.parametrize('duration', [True, '4', 0, -1, float('inf'), float('nan'), 3601])
def test_control_rejects_invalid_duration_without_mutation(tmp_path, duration):
    repo = CommandRepository(tmp_path / 'commands.db', clock=lambda: 100)
    repo.init()
    original = command()
    repo.enqueue(original)
    repo.claim_for_node('control-node', 'worker', owner_id=OWNER)
    before = repo.get(original.command_id)
    with pytest.raises(CommandRepositoryError, match='invalid_command_timing'):
        repo.renew_command(original.command_id, 'worker', owner_id=OWNER,
            node_id='control-node', lease_s=duration)
    assert repo.get(original.command_id) == before


def test_control_write_failure_rolls_back_and_preserves_cause(tmp_path):
    repo = CommandRepository(tmp_path / 'commands.db', clock=lambda: 100)
    repo.init()
    original = command()
    repo.enqueue(original)
    repo.claim_for_node('control-node', 'worker', owner_id=OWNER, lease_s=1)
    before = repo.get(original.command_id)
    with repo._connect() as conn:
        conn.execute("CREATE TRIGGER fail_control BEFORE UPDATE OF lease_until ON platform_commands "
                     "BEGIN SELECT RAISE(ABORT,'control write failed'); END")
    with pytest.raises(CommandRepositoryError) as caught:
        repo.renew_command(original.command_id, 'worker', owner_id=OWNER,
            node_id='control-node', lease_s=3)
    assert isinstance(caught.value.__cause__, sqlite3.IntegrityError)
    assert repo.get(original.command_id) == before


@pytest.mark.parametrize('state', ['succeeded', 'failed', 'unknown'])
def test_control_never_reopens_or_rewrites_terminal_receipts(tmp_path, state):
    repo = CommandRepository(tmp_path / 'commands.db', clock=lambda: 100)
    repo.init()
    original = command()
    repo.enqueue(original)
    repo.claim_for_node('control-node', 'worker', owner_id=OWNER)
    before = repo.record_receipt(original.command_id, state, worker_id='worker', result={'original': True})
    assert repo.renew_command(original.command_id, 'worker', owner_id=OWNER,
        node_id='control-node')['continue'] is False
    assert repo.get(original.command_id) == before


def test_control_observes_run_cancel_inside_its_write_transaction(tmp_path):
    app = _app(tmp_path)
    platform = app.extensions['fleet']['platform_repository']
    platform.upsert_workspace(OWNER, {'workspace_id': 'workspace', 'root_path': str(tmp_path)})
    platform.create_conversation(OWNER, 'control-conversation', title='', workspace_id='workspace')
    platform.append_turn(OWNER, 'control-conversation', 'control-message', 'control-run',
        text='execute', client_token='control-turn', now=100, config_snapshot={'workspace_id': 'workspace'})
    repo = app.extensions['fleet']['services']['platform_delivery'].repository
    original = command(expires_at=time.time() + 20)
    repo.enqueue(original)
    repo.claim_for_node('control-node', 'worker', owner_id=OWNER)
    with platform._connect() as conn:
        conn.execute('UPDATE runs SET cancel_requested=1 WHERE owner_id=? AND run_id=?', (OWNER, 'control-run'))
    control = repo.renew_command(original.command_id, 'worker', owner_id=OWNER, node_id='control-node')
    assert not control['continue'] and control['status'] == 'unknown'
    assert repo.get(original.command_id)['result'] == {'reason': 'execution_cancelled'}


def test_control_does_not_turn_transport_latency_into_extra_lease_time():
    now = [0.0]

    def delayed(_):
        now[0] += 2
        return {'command_id': 'control-command', 'status': 'leased', 'continue': True,
                'execution_deadline': 200.0, 'valid_for_s': 1.0}

    control = CommandControl(command().as_dict(), renew=delayed, clock=lambda: 100 + now[0],
                             monotonic=lambda: now[0], lease_s=1)
    with control:
        assert control.should_cancel(), 'A delayed response cannot grant a fresh local lease'
    assert not control.thread.is_alive()


def test_lost_control_transport_cannot_extend_last_confirmed_lease():
    now = [0.0]
    fail = [False]

    def renew(_):
        if fail[0]:
            raise OSError('disconnected')
        return {'command_id': 'control-command', 'status': 'leased', 'continue': True,
                'execution_deadline': 200.0, 'valid_for_s': 1.0}

    control = CommandControl(command().as_dict(), renew=renew, clock=lambda: 100 + now[0],
                             monotonic=lambda: now[0])
    control._pulse(initial=True)
    fail[0] = True
    now[0] = .5
    control._pulse()
    assert not control.should_cancel()
    now[0] = 1
    assert control.should_cancel()
    assert isinstance(control.failure, OSError)


@pytest.mark.parametrize('change', ['backward_clock', 'initial_transport_error', 'wrong_identity'])
def test_control_cannot_gain_time_or_spawn_after_failed_initial_observation(change):
    now, wall = [0.0], [100.0]

    def renew(_):
        if change == 'initial_transport_error':
            raise OSError('disconnected')
        return {'command_id': 'other' if change == 'wrong_identity' else 'control-command',
                'status': 'leased', 'continue': True, 'execution_deadline': 200.0, 'valid_for_s': 100.0}

    control = CommandControl(command().as_dict(), renew=renew, clock=lambda: wall[0], monotonic=lambda: now[0])
    control._pulse(initial=True)
    if change == 'backward_clock':
        wall[0] = 0
        now[0] = 99
        control._pulse()
        assert not control.should_cancel()
        now[0] = 100
    assert control.should_cancel()


def test_control_rechecks_authenticated_credential_in_renewal_transaction(tmp_path, monkeypatch):
    app = _app(tmp_path)
    platform = app.extensions['fleet']['platform_repository']
    platform.upsert_node(OWNER, {'node_id': 'control-node', 'label': 'Control'})
    credential = platform.provision_node_credential(OWNER, 'control-node', secret='x' * 40)['credential']
    repo = app.extensions['fleet']['services']['platform_delivery'].repository
    original = command(expires_at=time.time() + 20)
    repo.enqueue(original)
    repo.claim_for_node('control-node', 'worker', owner_id=OWNER, lease_s=1)
    before = repo.get(original.command_id)
    renew = repo.renew_command

    def rotated(*args, **kwargs):
        platform.provision_node_credential(OWNER, 'control-node', secret='y' * 40)
        return renew(*args, **kwargs)

    monkeypatch.setattr(repo, 'renew_command', rotated)
    response = app.test_client().post(CONTROL,
        json={'command_id': original.command_id, 'worker_id': 'worker', 'lease_s': 4},
        headers={'X-Platform-Node-Credential': credential})
    assert response.status_code == 409 and response.json['error'] == 'command_credential_revoked'
    assert repo.get(original.command_id) == before


@contextmanager
def server(app):
    http = make_server('127.0.0.1', 0, app, threaded=True)
    thread = Thread(target=http.serve_forever, name='test-command-control-http', daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(http.server_port)
    finally:
        http.shutdown()
        http.server_close()
        thread.join(3)
        assert not thread.is_alive()


@pytest.mark.parametrize('stop', ['hub_cancel', 'node_shutdown', 'execution_deadline', 'lease_renewal', 'hub_disconnect'])
def test_control_stops_the_owned_process_before_its_late_write(tmp_path, monkeypatch, stop):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    ready, late = workspace / 'ready', workspace / 'late'
    script = workspace / 'job.py'
    script.write_text("from pathlib import Path\nimport time\nPath('ready').touch()\n"
                      "time.sleep(1.5)\nPath('late').write_text('executed after stop')\n", encoding='utf-8')
    children = []
    spawn = bounded_process.popen_local

    def captured(*args, **kwargs):
        child = spawn(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(bounded_process, 'popen_local', captured)
    app = _app(tmp_path / 'hub')
    platform = app.extensions['fleet']['platform_repository']
    platform.upsert_node(OWNER, {'node_id': 'control-node', 'label': 'Control'})
    credential = platform.provision_node_credential(OWNER, 'control-node', secret='x' * 40)['credential']
    repo = app.extensions['fleet']['services']['platform_delivery'].repository
    timing = CommandTiming(time.time() + 5, .4, 5) if stop == 'execution_deadline' else None
    original = command(expires_at=timing.hard_deadline if timing else time.time() + 20,
        arguments={'argv': [sys.executable, str(script)], 'timeout_s': 10})
    if stop == 'execution_deadline':
        repo.enqueue(original, timing=timing)
    else:
        repo.enqueue(original)
    disconnected = Event()

    class ControlledTransport(Transport):
        def request_bounded_json(self, *args, **kwargs):
            if disconnected.is_set():
                raise OSError('fixture connection lost')
            return super().request_bounded_json(*args, **kwargs)

    with server(app) as origin:
        runtime = NodeRuntime(NodeRuntimeConfig(node_id='control-node', credential=credential,
            workspace_root=workspace, workspace_id='workspace', journal_path=tmp_path / 'node.db',
            hub_url=origin, public_key=PUBLIC_KEY, capabilities=frozenset({'workspace.exec'}), execution_mode='vm_direct'),
            transport=ControlledTransport(mode='direct', attempts=1, timeout=2, allow_insecure=True))
        results = []
        thread = Thread(target=lambda: results.append(runtime.poll_once(limit=1, lease_s=1)), daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while thread.is_alive() and not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), results
        if stop == 'hub_cancel':
            repo.cancel_wait(original.command_id)
        elif stop == 'node_shutdown':
            runtime.close_all()
        elif stop == 'hub_disconnect':
            disconnected.set()
        thread.join(5)
        runtime.close_all()
        assert not thread.is_alive()
        assert len(children) == 1 and children[0].poll() is not None
        assert late.exists() is (stop == 'lease_renewal'), 'Unexpected side effect after stop/deadline'
        saved = runtime.journal.get(original.command_id)
        if stop == 'lease_renewal':
            assert saved['state'] == 'succeeded'
            assert repo.get(original.command_id)['status'] == 'succeeded'
        else:
            assert saved['state'] == 'failed' and saved['result']['error_code'] == 'cancelled'
        if stop == 'hub_cancel':
            assert repo.get(original.command_id)['status'] == 'unknown'
        assert not any(t.name == 'node-command-control-' + original.command_id for t in threads())


def test_older_hub_retains_local_shutdown_for_controlled_executor(tmp_path):
    """No control-version advertisement must preserve the old HTTP shape."""
    from tools.platform.node_client import NodeClient
    from tools.platform.journal import NodeJournal

    repo = CommandRepository(tmp_path / 'commands.db', clock=lambda: 100)
    repo.init()
    original = command()
    repo.enqueue(original)
    calls = []

    class OldTransport:
        def post_json(self, url, body, headers):
            calls.append(url)
            if url.endswith('/poll'):
                return 200, {'ok': True, 'commands': repo.claim_for_node('control-node', 'worker', owner_id=OWNER)}
            repo.record_receipt(body['command_id'], body['status'], result=body['result'], worker_id='worker')
            return 200, {'ok': True}

    journal = NodeJournal(tmp_path / 'node.db')
    journal.init()
    node = NodeClient(journal, executor=NodeToolExecutor(DirectoryBackend(tmp_path), workspace_id='workspace'),
        node_id='control-node', worker_id='worker', credential='unused', hub_url='https://old.example',
        public_key=PUBLIC_KEY, transport=OldTransport(), clock=lambda: 100,
        should_cancel=lambda: repo.get(original.command_id)['status'] != 'queued')
    assert node.poll_once(limit=1)['receipts'] == 1
    assert journal.get(original.command_id)['result']['error_code'] == 'cancelled'
    assert all(not x.endswith('/control') for x in calls)


def test_stopped_node_does_not_claim_new_commands(tmp_path):
    runtime = NodeRuntime(NodeRuntimeConfig(node_id='control-node', credential='control-node:secret',
        workspace_root=tmp_path, workspace_id='workspace', journal_path=tmp_path / 'node.db',
        hub_url='https://unavailable.example', public_key=PUBLIC_KEY,
        capabilities=frozenset({'workspace.exec'})), transport=object())
    runtime.close_all()
    assert runtime.poll_once() == {'ok': True, 'commands': 0, 'receipts': 0}


def test_backend_cancellation_checks_do_not_send_renewals():
    calls = []

    def renew(_):
        calls.append(1)
        return {'command_id': 'control-command', 'status': 'leased', 'continue': True,
                'execution_deadline': 200.0, 'valid_for_s': 60.0}

    control = CommandControl(command().as_dict(), renew=renew, clock=lambda: 100, monotonic=lambda: 0)
    control._pulse(initial=True)
    for _ in range(1000):
        assert not control.should_cancel()
    assert calls == [1]
