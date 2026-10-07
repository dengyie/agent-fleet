from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from hub.infrastructure.browser_repository import BrowserRepository, BrowserRepositoryError
from hub.infrastructure.platform_db import PlatformRepository
from support.browser import browser_backend, URL

OWNER = 'owner@example.test'


@pytest.fixture
def approval_store(tmp_path):
    clock = [100.0]
    platform = PlatformRepository(tmp_path / 'platform.db')
    platform.init()
    platform.upsert_workspace(OWNER, {'workspace_id': 'workspace-a', 'root_path': str(tmp_path)})
    for run in ('run-a', 'run-b'):
        platform.create_conversation(OWNER, 'conv-' + run, title='', workspace_id='workspace-a')
        platform.append_turn(OWNER, 'conv-' + run, 'msg-' + run, run, text='test',
                             client_token=run, config_snapshot={'workspace_id': 'workspace-a', 'execution_node_id': 'node-a'}, now=100)
    repo = BrowserRepository(platform.db_path, clock=lambda: clock[0])
    repo.init()
    for sid, run in (('session-aaaaaaaaaa', 'run-a'), ('session-bbbbbbbbbb', 'run-b')):
        repo.create_session(OWNER, workspace_id='workspace-a', run_id=run, node_id='node-a',
                            profile_id='profile-a', session_id=sid)
    return repo, platform, clock


def grant(repo, *, run='run-a', sid='session-aaaaaaaaaa', selector='#go', **kwargs):
    return repo.grant_submit_approval(OWNER, workspace_id='workspace-a', run_id=run,
                                      node_id='node-a', session_id=sid, selector=selector, **kwargs)


def consume(repo, *, now=101, selector='#go'):
    return repo.consume_submit_approval(OWNER, 'run-a', session_id='session-aaaaaaaaaa',
                                        selector=selector, command_id='run-a:step:1', now=now)


def test_approval_ttl_is_exclusive_and_old_rows_do_not_shadow_new(approval_store):
    repo, _, clock = approval_store
    grant(repo)
    clock[0] = 400
    with pytest.raises(BrowserRepositoryError, match='approval_expired'):
        consume(repo, now=400)
    new = grant(repo)
    assert consume(repo, now=401) == new['approval_id']


def test_expired_rows_release_active_quota(approval_store):
    repo, _, clock = approval_store
    for i in range(4):
        grant(repo, selector=f'#field{i}')
    with pytest.raises(BrowserRepositoryError, match='approval_limit'):
        grant(repo)
    clock[0] = 400
    assert grant(repo)['state'] == 'active'


@pytest.mark.parametrize('state,expected', [('consumed', 'approval_consumed'), ('revoked', 'approval_revoked')])
def test_terminal_approval_errors_are_distinct(approval_store, state, expected):
    repo, _, _ = approval_store
    approval = grant(repo)
    if state == 'consumed':
        consume(repo)
    else:
        repo.revoke_submit_approval(OWNER, approval['approval_id'])
    with pytest.raises(BrowserRepositoryError, match=expected):
        consume(repo)


def test_grant_requires_matching_run_and_node(approval_store):
    repo, _, _ = approval_store
    with pytest.raises(BrowserRepositoryError, match='session_not_found'):
        grant(repo, run='run-b')
    with pytest.raises(BrowserRepositoryError, match='session_not_found'):
        repo.grant_submit_approval(OWNER, workspace_id='workspace-a', run_id='run-a', node_id='wrong-node',
                                   session_id='session-aaaaaaaaaa', selector='#go')


def test_session_close_and_run_cancel_expire_only_their_approvals(approval_store):
    repo, platform, clock = approval_store
    first = grant(repo)
    other = grant(repo, run='run-b', sid='session-bbbbbbbbbb')
    repo.close_session(OWNER, 'session-aaaaaaaaaa')
    assert repo.get_submit_approval(OWNER, first['approval_id'])['state'] == 'expired'
    assert repo.get_submit_approval(OWNER, other['approval_id'])['state'] == 'active'
    platform.cancel_run(OWNER, 'run-b', now=101)
    assert repo.get_submit_approval(OWNER, other['approval_id'])['state'] == 'expired'


def test_expiry_targets_exact_run(approval_store):
    repo, _, _ = approval_store
    first = grant(repo)
    other = grant(repo, run='run-b', sid='session-bbbbbbbbbb')
    repo.expire_submit_approvals(owner_id=OWNER, run_id='run-a')
    assert repo.get_submit_approval(OWNER, first['approval_id'])['state'] == 'expired'
    assert repo.get_submit_approval(OWNER, other['approval_id'])['state'] == 'active'


def test_grant_idempotency_and_selector_mismatch(approval_store):
    repo, _, _ = approval_store
    first = grant(repo, idempotency_key='one')
    assert grant(repo, idempotency_key='one') == first
    with pytest.raises(BrowserRepositoryError, match='approval_idempotency_conflict'):
        grant(repo, idempotency_key='one', selector='#other')
    with pytest.raises(BrowserRepositoryError, match='approval_required'):
        consume(repo, selector='#other')


def submit_command(command_id='run-a:step:1'):
    from hub.domain.platform_command import PlatformCommand
    return PlatformCommand.create(command_id=command_id, owner_id=OWNER, target_node='node-a',
        resource_id='workspace-a', action='tool.browser.submit',
        arguments={'session_id': 'session-aaaaaaaaaa', 'selector': '#go'},
        retry_class='manual_only', expires_at=1000, run_id='run-a')


def delivery_for(repo):
    from hub.application.command_delivery_service import CommandDeliveryService
    from hub.infrastructure.command_repository import CommandRepository
    from hub.domain.control import generate_ed25519_keypair
    private, public = generate_ed25519_keypair()
    commands = CommandRepository(repo.db_path, clock=repo.clock)
    commands.init()
    return CommandDeliveryService(commands, signing_key=private, require_signature=True, clock=repo.clock), public


def test_approval_command_and_outbox_roll_back_together(approval_store):
    repo, _, _ = approval_store
    approval = grant(repo)
    delivery, _ = delivery_for(repo)
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute("CREATE TRIGGER reject_submit BEFORE INSERT ON platform_command_outbox BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(Exception):
        delivery.enqueue_submit(submit_command(), approvals=repo, idempotency_key='run-a:step:1')
    assert repo.get_submit_approval(OWNER, approval['approval_id'])['state'] == 'active'
    assert delivery.repository.get('run-a:step:1') is None
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute('DROP TRIGGER reject_submit')
    row = delivery.enqueue_submit(submit_command(), approvals=repo, idempotency_key='run-a:step:1')
    assert row['arguments']['approval_id'] == approval['approval_id']


def test_atomic_admission_signs_final_arguments_and_is_command_idempotent(approval_store):
    from hub.domain.platform_command import verify_command
    repo, _, _ = approval_store
    approval = grant(repo)
    delivery, public = delivery_for(repo)
    first = delivery.enqueue_submit(submit_command(), approvals=repo, idempotency_key='run-a:step:1')
    assert verify_command(first, public)
    assert first['arguments']['approval_id'] == approval['approval_id']
    another = grant(repo)
    again = delivery.enqueue_submit(submit_command(), approvals=repo, idempotency_key='run-a:step:1')
    assert again['arguments'] == first['arguments']
    assert repo.get_submit_approval(OWNER, another['approval_id'])['state'] == 'active'


def test_concurrent_commands_cannot_consume_same_approval(approval_store):
    repo, _, _ = approval_store
    grant(repo)
    delivery, _ = delivery_for(repo)
    def attempt(i):
        try:
            delivery.enqueue_submit(submit_command(f'run-a:step:{i}'), approvals=repo)
            return 'queued'
        except Exception as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (1, 2)))
    assert sorted(results) == ['approval_consumed', 'queued']


@pytest.mark.parametrize('cross_run', [False, True])
def test_signed_submit_reaches_driver_and_records_approval(approval_store, tmp_path, cross_run):
    from dataclasses import replace
    from hub.domain.platform_command import args_hash, sign_command, verify_command
    from tools.platform.node_executor import NodeToolExecutor
    from tools.platform.node_client import NodeClient
    from tools.platform.journal import NodeJournal
    repo, _, clock = approval_store
    calls = []
    class Driver:
        def open(self, url):
            return None
        def submit(self, selector):
            calls.append(selector)
            return {'state': 'submitted'}
    backend = browser_backend(Driver, submit_enabled=True)
    sid = backend.execute('browser.open', {'url': URL}, run_id='run-a')['session_id']
    repo.create_session(OWNER, workspace_id='workspace-a', run_id='run-a', node_id='node-a', profile_id='profile-a', session_id=sid)
    approval = grant(repo, sid=sid)
    command = submit_command()
    arguments = {'session_id': sid, 'selector': '#go'}
    command = replace(command, arguments=arguments, args_digest=args_hash(arguments))
    delivery, public = delivery_for(repo)
    row = delivery.enqueue_submit(command, approvals=repo)
    if cross_run:
        row = {**row, 'run_id': 'run-b', 'command_id': 'run-b:step:1'}
        row['signature'] = sign_command(row, delivery.signing_key)
    assert verify_command(row, public)
    journal = NodeJournal(tmp_path / 'node.db')
    journal.init()
    node = NodeClient(journal, executor=NodeToolExecutor(None, browser_backend=backend, browser_enabled=True),
                      clock=lambda: clock[0], node_id='node-a', public_key=public, require_signature=True)
    wire = node.handle(row)
    if cross_run:
        assert wire['status'] == 'failed'
        assert wire['result']['error_code'] == 'session_not_found'
        assert calls == []
        assert repo.get_submit_approval(OWNER, approval['approval_id'])['state'] == 'consumed'
        return
    assert wire['status'] == 'succeeded'
    assert wire['result']['result']['approval_id'] == approval['approval_id']
    assert calls == ['#go']
    assert node.handle(row) == wire
    assert calls == ['#go']


def test_local_tool_broker_cannot_bypass_owner_approval():
    from tools.platform.tool_broker import ToolBroker
    calls = []
    class Leases:
        def validate(self, *args):
            return True
    class Driver:
        def open(self, url):
            return None
        def submit(self, selector):
            calls.append(selector)
            return {'state': 'submitted'}
    backend = browser_backend(Driver, submit_enabled=True)
    sid = backend.execute('browser.open', {'url': URL})['session_id']
    broker = ToolBroker(None, Leases(), resource_id='workspace-a', browser_backend=backend,
                        browser_enabled=True, browser_submit_enabled=True)
    receipt = broker.execute(command_id='run-a:step:1', tool='browser.submit',
                             arguments={'session_id': sid, 'selector': '#go'}, owner_id=OWNER, epoch=1)
    assert receipt.error_code == 'submit_disabled'
    assert calls == []


def test_opaque_session_and_approval_ids_do_not_use_catalog_id_rules(approval_store):
    repo, _, _ = approval_store
    sid = '_opaque-key-session-123456'
    repo.create_session(OWNER, workspace_id='workspace-a', run_id='run-a', node_id='node-a', profile_id='profile-a', session_id=sid)
    assert grant(repo, sid=sid)['session_id'] == sid


def test_run_and_workspace_limits_and_rolling_hour(approval_store):
    repo, _, clock = approval_store
    sessions = [f'session-limit-{i:04}' for i in range(17)]
    for sid in sessions:
        repo.create_session(OWNER, workspace_id='workspace-a', run_id='run-a', node_id='node-a', profile_id='profile-a', session_id=sid)
    for sid in sessions[:16]:
        grant(repo, sid=sid)
    with pytest.raises(BrowserRepositoryError, match='approval_limit'):
        grant(repo, sid=sessions[-1])
    repo.expire_submit_approvals(owner_id=OWNER, run_id='run-a')
    for index in range(48):
        approval = grant(repo, sid=sessions[0], selector=f'#rate{index}')
        repo.revoke_submit_approval(OWNER, approval['approval_id'])
    with pytest.raises(BrowserRepositoryError, match='approval_limit'):
        grant(repo, sid=sessions[0])
    clock[0] = 3700
    assert grant(repo, sid=sessions[0])['state'] == 'active'


def test_unsigned_submit_and_tampered_approval_are_rejected_before_driver(approval_store, tmp_path):
    from tools.platform.node_client import NodeClient
    from tools.platform.journal import NodeJournal
    repo, _, _ = approval_store
    grant(repo)
    delivery, public = delivery_for(repo)
    row = delivery.enqueue_submit(submit_command(), approvals=repo)
    journal = NodeJournal(tmp_path / 'signatures.db')
    journal.init()
    calls = []
    node = NodeClient(journal, executor=lambda cmd: calls.append(cmd), clock=lambda: 101)
    assert node.handle({**row, 'signature': None})['status'] == 'rejected'
    signed_node = NodeClient(journal, executor=lambda cmd: calls.append(cmd), clock=lambda: 101, public_key=public)
    row['arguments']['approval_id'] = 'another-approval-id'
    assert signed_node.handle(row)['status'] == 'rejected'
    assert calls == []


def test_restart_keeps_revocation_and_failed_signing_rolls_back(approval_store):
    repo, _, _ = approval_store
    approval = grant(repo)
    delivery, _ = delivery_for(repo)
    delivery.signing_key = b'bad-key'
    with pytest.raises(Exception) as error:
        delivery.enqueue_submit(submit_command(), approvals=repo)
    assert error.value.__cause__ is not None
    assert repo.get_submit_approval(OWNER, approval['approval_id'])['state'] == 'active'
    repo.revoke_submit_approval(OWNER, approval['approval_id'])
    reopened = BrowserRepository(repo.db_path, clock=repo.clock)
    reopened.init()
    with pytest.raises(BrowserRepositoryError, match='approval_revoked'):
        consume(reopened)


@pytest.mark.parametrize('state', ['succeeded', 'failed', 'unknown', 'cancelled'])
def test_every_run_terminal_state_expires_approvals(approval_store, state):
    repo, platform, clock = approval_store
    approval = grant(repo)
    other = grant(repo, run='run-b', sid='session-bbbbbbbbbb')
    claim = platform.claim_run(OWNER, worker_id='terminal-worker', now=100, lease_s=60)
    assert claim['run_id'] == 'run-a'
    platform.finish_run(OWNER, 'run-a', lease_id=claim['lease_id'], worker_id='terminal-worker', state=state, now=101)
    assert repo.get_submit_approval(OWNER, approval['approval_id'])['state'] == 'expired'
    assert repo.get_submit_approval(OWNER, other['approval_id'])['state'] == 'active'


def test_ttl_before_boundary_and_bounded_sweep(approval_store):
    repo, _, clock = approval_store
    approval = grant(repo)
    assert consume(repo, now=399.999) == approval['approval_id']
    next_approval = grant(repo)
    clock[0] = 400
    assert repo.expire_submit_approvals() == 1
    assert repo.get_submit_approval(OWNER, next_approval['approval_id'])['state'] == 'expired'


def test_workspace_rate_limit_is_owner_scoped(approval_store):
    repo, platform, _ = approval_store
    for i in range(64):
        approval = grant(repo, selector=f'#grant{i}')
        repo.revoke_submit_approval(OWNER, approval['approval_id'])
    other = 'other@example.test'
    platform.upsert_workspace(other, {'workspace_id': 'workspace-a', 'root_path': '/tmp/other-workspace'})
    platform.create_conversation(other, 'conv-other', title='', workspace_id='workspace-a')
    platform.append_turn(other, 'conv-other', 'msg-other', 'run-other', text='test', client_token='other', config_snapshot={'workspace_id': 'workspace-a'}, now=100)
    repo.create_session(other, workspace_id='workspace-a', run_id='run-other', node_id='node-a', profile_id='profile-a', session_id='session-other-123456')
    assert repo.grant_submit_approval(other, workspace_id='workspace-a', run_id='run-other', node_id='node-a', session_id='session-other-123456', selector='#go')['state'] == 'active'


def test_writer_fence_rejects_submit_before_consuming_approval(approval_store):
    from hub.application.task_service import ApplicationError
    from hub.infrastructure.execution_window_repository import ExecutionWindowRepository

    repo, _, _ = approval_store
    approval = grant(repo)
    windows = ExecutionWindowRepository(repo.db_path, clock=repo.clock)
    windows.init()
    created = windows.create_window(OWNER, 'run-a')
    window_id = created['window']['window_id']
    windows.redeem_ticket(OWNER, window_id, created['attach_ticket'])
    windows.acquire_writer(OWNER, window_id, 'operator')
    delivery, _ = delivery_for(repo)
    delivery.browser_write_guard = ExecutionWindowRepository.assert_browser_write_allowed

    with pytest.raises(ApplicationError) as blocked:
        delivery.enqueue_submit(
            submit_command(), approvals=repo, idempotency_key='run-a:step:1',
        )
    assert blocked.value.code == 'writer_lease_active'
    assert repo.get_submit_approval(OWNER, approval['approval_id'])['state'] == 'active'
    assert delivery.repository.get('run-a:step:1') is None
