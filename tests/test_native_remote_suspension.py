"""Remote waiting must persist progress and return the global worker slot."""
from hub.application.command_delivery_service import CommandDeliveryService
from hub.application.run_worker_service import LocalRunWorkerService
from hub.infrastructure.command_repository import CommandRepository
from hub.platform.providers.base import ModelResponse
from tests.test_platform_run_worker import OWNER, _app, _catalog


def test_remote_command_wait_returns_without_sleeping_or_replaying(tmp_path):
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    repo.upsert_node(OWNER, {'node_id': 'remote', 'label': 'Remote'})
    commands = CommandRepository(repo.db_path)
    commands.init()
    calls, sleeps = [], []

    def no_sleep(seconds):
        sleeps.append(seconds)
        raise AssertionError('A durable remote wait must return the worker slot')

    delivery = CommandDeliveryService(commands, sleep=no_sleep)

    class Provider:
        def complete(self, messages, tools, **kwargs):
            calls.append(messages)
            if messages[-1]['role'] == 'tool':
                return ModelResponse(kind='final', text='done')
            return ModelResponse(kind='tool_call', tool='workspace.write',
                                 arguments={'path': 'remote.txt', 'content': 'once'})

    client = app.test_client()
    conversation = client.post('/api/platform/v1/conversations', json={}).get_json()['conversation']
    run = client.post(f"/api/platform/v1/conversations/{conversation['conversation_id']}/turns", json={
        'text': 'write remotely', 'client_token': 'suspend-1',
        'overrides': {'execution_node_id': 'remote'}}).get_json()['run']
    worker = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
                                  worker_id='worker', provider_factory=lambda _: Provider(),
                                  remote_execution_enabled=True, remote_delivery=delivery)
    result = worker.run_once(OWNER)
    assert result['state'] == 'waiting_node'
    assert sleeps == []
    assert len(calls) == 1
    assert commands.get(run['run_id'] + ':step:1')['status'] == 'queued'
    assert repo.get_run(OWNER, run['run_id'])['lease_expires_at'] is None

    # Waiting retains workspace capacity, while another workspace can run.
    assert repo.claim_worker_slot(OWNER, 'blocked-run', worker_id='blocked', workspace_key=OWNER + ':home',
        max_concurrency=1, max_workspace_concurrency=1, now=__import__('time').time()) is None
    slot = repo.claim_worker_slot('other@example.test', 'other-run', worker_id='other', workspace_key='other:work',
        max_concurrency=1, max_workspace_concurrency=1, now=__import__('time').time())
    assert slot is not None
    repo.release_worker_slot(slot['lease_id'], worker_id='other')

    from fleet_core.backends.directory import DirectoryBackend
    from tools.platform.node_executor import NodeToolExecutor
    claimed = commands.claim_for_node('remote', 'node-worker', owner_id=OWNER)
    assert len(claimed) == 1
    executor = NodeToolExecutor(DirectoryBackend(tmp_path / 'node-workspace'), workspace_id='home')
    receipt = executor(claimed[0])
    assert receipt['state'] == 'succeeded'
    commands.record_receipt(claimed[0]['command_id'], 'succeeded', result=receipt, worker_id='node-worker')
    with repo._connect() as conn:
        conn.execute('UPDATE runs SET runtime_resume_at=0 WHERE run_id=?', (run['run_id'],))
    # The new worker has only the durable checkpoint and existing command.
    restarted = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
        worker_id='restarted', provider_factory=lambda _: Provider(),
        remote_execution_enabled=True, remote_delivery=delivery)
    assert restarted.run_once(OWNER)['state'] == 'succeeded'
    assert len(calls) == 2
    assert calls[-1][-1]['role'] == 'tool'
    assert commands.get(run['run_id'] + ':step:1')['attempt'] == 1
    assert sleeps == []


import pytest


@pytest.mark.parametrize('claimed', [False, True])
def test_cancel_suspended_run_fences_original_remote_command(tmp_path, claimed):
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    repo.upsert_node(OWNER, {'node_id': 'remote', 'label': 'remote'})
    repo.create_conversation(OWNER, 'conversation', title='', workspace_id='home')
    repo.append_turn(OWNER, 'conversation', 'message', 'run-wait', text='write', client_token='wait', now=1,
                     config_snapshot={'model_profile_id': 'local', 'workspace_id': 'home', 'execution_node_id': 'remote'})
    commands = CommandRepository(repo.db_path)
    commands.init()
    delivery = CommandDeliveryService(commands, sleep=lambda _: pytest.fail('unexpected wait'))
    worker = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
                                  worker_id='worker', remote_execution_enabled=True, remote_delivery=delivery)
    assert worker.run_once(OWNER)['state'] == 'waiting_node'
    if claimed:
        assert len(commands.claim_for_node('remote', 'node', owner_id=OWNER)) == 1
    response = app.test_client().post('/api/platform/v1/runs/run-wait/cancel')
    assert response.status_code == 200
    assert commands.claim_for_node('remote', 'after-http-cancel', owner_id=OWNER) == []
    result = worker.run_once(OWNER)
    assert result['state'] == ('unknown' if claimed else 'cancelled')
    assert commands.get('run-wait:step:1')['status'] == ('unknown' if claimed else 'expired')
    assert commands.claim_for_node('remote', 'late', owner_id=OWNER) == []


def test_cancel_before_enqueue_wins_even_without_broker_callback(tmp_path):
    from hub.platform.remote_tool_broker import RemoteToolBroker
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    repo.create_conversation(OWNER, 'conversation', title='', workspace_id='home')
    repo.append_turn(OWNER, 'conversation', 'message', 'run-race', text='write', client_token='race', now=1,
                     config_snapshot={'model_profile_id': 'local', 'workspace_id': 'home', 'execution_node_id': 'remote'})
    commands = CommandRepository(repo.db_path)
    commands.init()
    repo.cancel_run(OWNER, 'run-race', now=2)
    broker = RemoteToolBroker(CommandDeliveryService(commands), node_id='remote', resource_id='home', run_id='run-race', defer_wait=True)
    result = broker.execute(command_id='run-race:step:1', tool='workspace.write',
                            arguments={'path': 'x', 'content': 'x'}, owner_id=OWNER, epoch=1)
    assert result.state == 'failed' and result.error_code == 'execution_cancelled'
    assert commands.get('run-race:step:1') is None


def test_wait_reservation_survives_resume_claim_before_slot_admission(tmp_path):
    import time
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    repo.upsert_node(OWNER, {'node_id': 'remote', 'label': 'remote'})
    repo.create_conversation(OWNER, 'conversation', title='', workspace_id='home')
    repo.append_turn(OWNER, 'conversation', 'message', 'run-wait', text='write', client_token='wait', now=1,
                     config_snapshot={'model_profile_id': 'local', 'workspace_id': 'home', 'execution_node_id': 'remote'})
    commands = CommandRepository(repo.db_path)
    commands.init()
    worker = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'], worker_id='worker',
                                  remote_execution_enabled=True, remote_delivery=CommandDeliveryService(commands))
    assert worker.run_once(OWNER)['state'] == 'waiting_node'
    with repo._connect() as conn:
        conn.execute("UPDATE runs SET runtime_resume_at=0 WHERE run_id='run-wait'")
    assert repo.claim_run(OWNER, worker_id='resumer', now=time.time())['state'] == 'running'
    assert repo.claim_worker_slot(OWNER, 'other-run', worker_id='competitor', workspace_key=OWNER + ':home',
        max_concurrency=2, max_workspace_concurrency=1, now=time.time()) is None


@pytest.mark.parametrize('command_state,expected', [
    ('queued', 'waiting_node'), ('unknown', 'unknown'), ('failed', 'failed'),
    ('cancel_queued', 'cancelled'), ('cancel_dispatched', 'unknown'),
])
def test_wait_observation_does_not_reopen_disabled_execution_dependencies(tmp_path, command_state, expected):
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    repo.upsert_node(OWNER, {'node_id': 'remote', 'label': 'remote'})
    repo.create_conversation(OWNER, 'conversation', title='', workspace_id='home')
    repo.append_turn(OWNER, 'conversation', 'message', 'run-wait', text='write', client_token='wait', now=1,
                     config_snapshot={'model_profile_id': 'local', 'workspace_id': 'home', 'execution_node_id': 'remote'})
    commands = CommandRepository(repo.db_path)
    commands.init()
    worker = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'], worker_id='worker',
                                  remote_execution_enabled=True, remote_delivery=CommandDeliveryService(commands))
    assert worker.run_once(OWNER)['state'] == 'waiting_node'
    if command_state in {'unknown', 'failed', 'cancel_dispatched'}:
        commands.claim_for_node('remote', 'node', owner_id=OWNER)
    if command_state in {'unknown', 'failed'}:
        commands.record_receipt('run-wait:step:1', command_state, result={}, worker_id='node')
    if command_state.startswith('cancel_'):
        repo.cancel_run(OWNER, 'run-wait', now=__import__('time').time())
    repo.upsert_workspace(OWNER, {'workspace_id': 'home', 'backend': 'directory',
                                 'root_path': str(tmp_path / 'workspace'), 'enabled': False})
    worker.provider_factory = lambda _: pytest.fail('Observing a command must not create a provider')
    events_before = len(repo.list_run_events(OWNER, 'run-wait')['events'])
    with repo._connect() as conn:
        conn.execute("UPDATE runs SET runtime_resume_at=0 WHERE run_id='run-wait'")
    assert worker.run_once(OWNER)['state'] == expected
    if command_state == 'queued':
        assert commands.get('run-wait:step:1')['status'] == 'queued'
        assert len(repo.list_run_events(OWNER, 'run-wait')['events']) == events_before


def test_terminal_run_releases_suspended_workspace_reservation(tmp_path):
    import time
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    repo.upsert_node(OWNER, {'node_id': 'remote', 'label': 'remote'})
    repo.create_conversation(OWNER, 'conversation', title='', workspace_id='home')
    repo.append_turn(OWNER, 'conversation', 'message', 'run-wait', text='write', client_token='wait', now=1,
                     config_snapshot={'model_profile_id': 'local', 'workspace_id': 'home', 'execution_node_id': 'remote'})
    commands = CommandRepository(repo.db_path)
    commands.init()
    worker = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'], worker_id='worker',
                                  remote_execution_enabled=True, remote_delivery=CommandDeliveryService(commands))
    assert worker.run_once(OWNER)['state'] == 'waiting_node'
    repo.set_run_state(OWNER, 'run-wait', 'unknown', now=time.time())
    assert repo.claim_worker_slot(OWNER, 'next-run', worker_id='next', workspace_key=OWNER + ':home',
        max_concurrency=1, max_workspace_concurrency=1, now=time.time()) is not None


def test_restored_wait_does_not_keep_workspace_capacity_reserved(tmp_path):
    import time
    from hub.infrastructure.platform_backup import PlatformBackupService
    from hub.infrastructure.platform_db import PlatformRepository
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    repo.upsert_node(OWNER, {'node_id': 'remote', 'label': 'remote'})
    repo.create_conversation(OWNER, 'conversation', title='', workspace_id='home')
    repo.append_turn(OWNER, 'conversation', 'message', 'run-wait', text='write', client_token='wait', now=1,
                     config_snapshot={'model_profile_id': 'local', 'workspace_id': 'home', 'execution_node_id': 'remote'})
    commands = CommandRepository(repo.db_path)
    commands.init()
    worker = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'], worker_id='worker',
                                  remote_execution_enabled=True, remote_delivery=CommandDeliveryService(commands))
    assert worker.run_once(OWNER)['state'] == 'waiting_node'
    artifacts = tmp_path / 'artifacts'
    artifacts.mkdir()
    backup = PlatformBackupService()
    backup.create_backup(repo.db_path, artifacts, tmp_path / 'backup')
    backup.restore_backup(tmp_path / 'backup', tmp_path / 'restored')
    restored = PlatformRepository(tmp_path / 'restored' / 'platform.db')
    restored.init()
    assert restored.get_run(OWNER, 'run-wait')['state'] == 'unknown'
    assert restored.claim_worker_slot(OWNER, 'next-run', worker_id='next', workspace_key=OWNER + ':home',
        max_concurrency=1, max_workspace_concurrency=1, now=time.time()) is not None


def test_observing_submit_receipt_preserves_approval_attribution(tmp_path):
    from fleet_core.platform_command import PlatformCommand
    from hub.platform.remote_tool_broker import RemoteToolBroker
    commands = CommandRepository(tmp_path / 'commands.db')
    commands.init()
    command = PlatformCommand.create(command_id='run:step:1', target_node='remote', owner_id=OWNER,
        run_id='run', action='tool.browser.submit', resource_id='home',
        arguments={'session_id': 's' * 24, 'selector': '#go', 'approval_id': 'approved'},
        retry_class='manual_only', expires_at=__import__('time').time() + 60)
    commands.enqueue(command)
    commands.claim_for_node('remote', 'node', owner_id=OWNER)
    commands.record_receipt('run:step:1', 'succeeded', worker_id='node',
        result={'state': 'succeeded', 'result': {'submitted': True}})
    events = []
    receipt = RemoteToolBroker(CommandDeliveryService(commands), node_id='remote', resource_id='home',
        run_id='run', event_sink=lambda kind, payload: events.append(payload)).observe(
            command_id='run:step:1', tool='browser.submit')
    assert receipt.state == 'succeeded'
    assert receipt.result == {'submitted': True, 'approval_id': 'approved'}
    assert events[-1]['approval_id'] == 'approved'
