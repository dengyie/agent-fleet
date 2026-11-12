"""Workspace admission must not scan completed Run history on every claim."""
import time

import pytest

from hub.application.command_delivery_service import CommandDeliveryService
from hub.application.run_worker_service import LocalRunWorkerService
from hub.infrastructure.command_repository import CommandRepository
from tests.test_platform_run_worker import OWNER, _app, _catalog


@pytest.mark.parametrize('operation', ['slot', 'candidate'])
def test_remote_wait_capacity_work_is_bounded_by_active_waits(tmp_path, monkeypatch, record_property, operation):
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    repo.upsert_node(OWNER, {'node_id': 'remote', 'label': 'remote'})
    repo.create_conversation(OWNER, 'waiting', title='', workspace_id='home')
    repo.append_turn(OWNER, 'waiting', 'waiting-message', 'waiting-run', text='write', client_token='wait', now=1,
                     config_snapshot={'model_profile_id': 'local', 'workspace_id': 'home', 'execution_node_id': 'remote'})
    commands = CommandRepository(repo.db_path)
    commands.init()
    worker = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'], worker_id='worker',
                                  remote_execution_enabled=True, remote_delivery=CommandDeliveryService(commands))
    admission_now = time.time()
    assert worker.run_once(OWNER)['state'] == 'waiting_node'

    repo.create_conversation(OWNER, 'history', title='', workspace_id='home')
    with repo._connect() as conn:
        conn.executemany('INSERT INTO messages(message_id,conversation_id,owner_id,role,content_ref,content,created_at,turn_sequence) '
                         "VALUES(?,'history',?,'user','','old','2026-11-01T00:00:00Z',?)",
                         [(f'old-message-{i}', OWNER, i + 1) for i in range(4000)])
        conn.executemany('INSERT INTO runs(run_id,conversation_id,owner_id,trigger_message_id,state,config_snapshot) '
                         "VALUES(?,'history',?,?,'succeeded','{\"workspace_id\":\"home\"}')",
                         [(f'old-run-{i}', OWNER, f'old-message-{i}') for i in range(4000)])
    repo.upsert_workspace(OWNER, {'workspace_id': 'free', 'root_path': str(tmp_path / 'free')})
    repo.create_conversation(OWNER, 'next', title='', workspace_id='free')
    repo.append_turn(OWNER, 'next', 'next-message', 'next-run', text='next', client_token='next', now=2,
                     config_snapshot={'model_profile_id': 'local', 'workspace_id': 'free'})
    instructions = [0]
    statements = []
    connect = repo._connect

    def tracked_connect():
        conn = connect()

        def progress():
            instructions[0] += 100
            return 0

        conn.set_progress_handler(progress, 100)
        conn.set_trace_callback(lambda sql: statements.append((instructions[0], sql)))
        return conn

    monkeypatch.setattr(repo, '_connect', tracked_connect)
    if operation == 'slot':
        assert repo.claim_worker_slot(OWNER, 'competitor', worker_id='next', workspace_key=OWNER + ':home',
            max_concurrency=1, max_workspace_concurrency=1, now=admission_now) is None
    else:
        claim = repo.claim_run(OWNER, worker_id='next', max_workspace_concurrency=1, now=admission_now)
        assert claim is not None and claim['run_id'] == 'next-run'
    costs = sorted(((end[0] - start[0], start[1]) for start, end in zip(
        statements, statements[1:] + [(instructions[0], '')])), reverse=True)
    record_property('sqlite_vm_instructions', instructions[0])
    assert instructions[0] < 5000, (
        f'Admission executed {instructions[0]} SQLite instructions for one active wait; largest queries: {costs[:2]}')
