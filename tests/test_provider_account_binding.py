"""Immutable AGY account identity across real task storage and ACP continuation."""
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from hub.application.errors import ApplicationError
from hub.application.task_service import TaskHostPolicy, TaskService
from hub.domain.task import DomainError, public_task, validate_task_input
from hub.infrastructure.task_repository import SqliteTaskRepository
from tools.adapters.agy import AgyAdapter
from tools.adapters.native_resume import build_resume_argv, continuation_options
from tools.spawn_policy import UnsafeInstruction

ACCOUNT = 'pa_' + 'a' * 32
OTHER = 'pa_' + 'b' * 32
OWNER = 'operator@example.test'
OWNER_DIGEST = hashlib.sha256(OWNER.encode()).hexdigest()
REQUEST = {'machine': 'mac-local', 'agent_type': 'agy', 'project': 'fleet',
           'instruction': 'Inspect fixture', 'client_token': 'agy-bound-task',
           'provider_account_id': ACCOUNT}


def repository(tmp_path):
    repo = SqliteTaskRepository(tmp_path / 'tasks.db')
    repo.init()
    return repo


def create(repo, **changes):
    return repo.create_task(**{**REQUEST, 'requested_by': OWNER, **changes})


def test_bound_account_validates_and_is_public_but_legacy_shape_stays_compatible():
    assert validate_task_input(REQUEST)['provider_account_id'] == ACCOUNT
    assert public_task({'task_id': 't1', 'provider_account_id': ACCOUNT}) == {
        'task_id': 't1', 'provider_account_id': ACCOUNT}
    assert public_task({'task_id': 't1', 'provider_account_id': None}) == {'task_id': 't1'}


@pytest.mark.parametrize('account', ['', '../profile', 'pa_' + 'A' * 32, 123, 'pa_a'])
def test_account_binding_rejects_invalid_or_ambiguous_ids(account):
    with pytest.raises(DomainError) as error:
        validate_task_input({**REQUEST, 'provider_account_id': account})
    assert error.value.code == 'invalid_provider_account'


def test_account_binding_is_only_valid_for_agy():
    with pytest.raises(DomainError) as error:
        validate_task_input({**REQUEST, 'agent_type': 'codex'})
    assert error.value.code == 'invalid_provider_account'


def test_same_token_replays_account_and_rejects_changed_binding(tmp_path):
    repo = repository(tmp_path)
    original, made = create(repo)
    assert made and original['provider_account_id'] == ACCOUNT
    restored, made = create(repo)
    assert not made and restored == original
    for changed in (OTHER, None):
        with pytest.raises(DomainError) as error:
            create(repo, provider_account_id=changed)
        assert error.value.code == 'idempotency_conflict'
    leased = repo.lease_task(machine='mac-local', runner_id='fixture',
                            provider_account_bindings=[(ACCOUNT, OWNER)])
    assert leased['provider_account_id'] == ACCOUNT
    assert leased['provider_account_owner'] == OWNER_DIGEST
    assert OWNER not in json.dumps(leased)


def test_direct_repository_rejects_unsafe_account_before_write(tmp_path):
    repo = repository(tmp_path)
    with pytest.raises(DomainError):
        create(repo, provider_account_id='../credentials')
    assert repo.list_tasks() == []


def test_old_database_migration_preserves_unbound_tasks_and_is_repeatable(tmp_path):
    path = tmp_path / 'tasks.db'
    schema = Path(__file__).parent / 'fixtures/task_schema_pre_v4.sql'
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(schema.read_text(encoding='utf-8'))
        conn.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                     ('t-old', 'old-token', 'mac-local', 'codex', 'fleet', 'Keep me', OWNER,
                      'queued', 'attempt-old', '2026-01-01T00:00:00Z', '2099-01-01T00:00:00Z'))
        conn.commit()
    repo = SqliteTaskRepository(path)
    repo.init()
    assert repo.get_task('t-old')['provider_account_id'] is None
    create(repo)
    repo.init()
    assert len(repo.list_tasks()) == 2
    assert repo.get_task('t-old')['instruction'] == 'Keep me'


def test_future_binding_schema_refuses_without_mutating_database(tmp_path):
    repo = repository(tmp_path)
    create(repo)
    with closing(repo._connect()) as conn:
        conn.execute("UPDATE task_repository_meta SET value='999' WHERE key='provider_account_schema'")
        conn.commit()
        before = '\n'.join(conn.iterdump())
    with pytest.raises(sqlite3.DatabaseError):
        repo.init()
    with closing(repo._connect()) as conn:
        assert '\n'.join(conn.iterdump()) == before


def test_malformed_existing_binding_column_refuses_atomically(tmp_path):
    path = tmp_path / 'tasks.db'
    schema = Path(__file__).parent / 'fixtures/task_schema_pre_v4.sql'
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(schema.read_text(encoding='utf-8'))
        conn.execute('ALTER TABLE tasks ADD COLUMN provider_account_id BLOB')
        conn.commit()
        before = '\n'.join(conn.iterdump())
    with pytest.raises(sqlite3.DatabaseError):
        SqliteTaskRepository(path).init()
    with closing(sqlite3.connect(path)) as conn:
        assert '\n'.join(conn.iterdump()) == before


class ProviderPolicy:
    def __init__(self):
        self.calls = []
        self.error = None

    def assert_ready(self, account_id, machine, actor):
        self.calls.append((account_id, machine, actor))
        if self.error:
            raise self.error
        return {'account_id': account_id, 'state': 'ready'}


def task_service(tmp_path):
    repo = repository(tmp_path)
    observations = SimpleNamespace(read_current=lambda machine: {
        'reachable': True, 'agents': {'agy': {'installed': True,
            'provider_accounts': {'schema_version': 1}}}})
    service = TaskService(repo, observations, None,
                          TaskHostPolicy(project_whitelist={'mac-local': ['fleet']}))
    service.provider_accounts = ProviderPolicy()
    return service


def test_task_admission_checks_original_owner_and_replay_does_not_requeue_when_disabled(tmp_path):
    service = task_service(tmp_path)
    result = service.create(REQUEST, OWNER)
    assert result['created']
    assert service.provider_accounts.calls == [(ACCOUNT, 'mac-local', OWNER)]
    service.provider_accounts.error = ApplicationError('provider_account_disabled', '', 409)
    replay = service.create(REQUEST, OWNER)
    assert not replay['created'] and replay['task'] == result['task']
    with pytest.raises(ApplicationError) as error:
        service.create({**REQUEST, 'client_token': 'new-message'}, OWNER)
    assert error.value.code == 'provider_account_disabled'
    assert len(service.task_repo.list_tasks()) == 1


def test_bound_tasks_fail_closed_without_account_service(tmp_path):
    service = task_service(tmp_path)
    service.provider_accounts = None
    with pytest.raises(ApplicationError) as error:
        service.create(REQUEST, OWNER)
    assert error.value.code == 'provider_accounts_unavailable'
    assert not service.task_repo.list_tasks()


def test_retry_and_continue_revalidate_the_original_account_owner(tmp_path):
    service = task_service(tmp_path)
    task = service.create(REQUEST, OWNER)['task']
    service.cancel(task['task_id'], OWNER)
    service.provider_accounts.calls.clear()
    with pytest.raises(ApplicationError) as error:
        service.retry(task['task_id'], 'other@example.test')
    assert error.value.code == 'forbidden'
    assert not service.provider_accounts.calls
    service.retry(task['task_id'], OWNER)
    assert service.provider_accounts.calls == [(ACCOUNT, 'mac-local', OWNER)]
    service.pause(task['task_id'], OWNER)
    service.provider_accounts.error = ApplicationError('provider_account_disabled', '', 409)
    with pytest.raises(ApplicationError):
        service.continue_task(task['task_id'], OWNER)
    assert service.task_repo.get_task(task['task_id'])['state'] == 'paused'


def test_adapter_retains_owned_account_identity_in_exact_continuation(tmp_path):
    adapter = AgyAdapter(command=[sys.executable, '-c', 'pass'])
    adapter.bind_account(tmp_path / 'accounts', ACCOUNT, OWNER_DIGEST)
    argv = adapter.build_argv('First fixture')
    for flag, value in (('--account-root', str((tmp_path / 'accounts').resolve())),
                        ('--account-id', ACCOUNT), ('--account-owner', OWNER_DIGEST)):
        assert argv[argv.index(flag) + 1] == value
    options = continuation_options('agy', argv)
    continued = build_resume_argv('agy', argv[0], 'native-original', 'Next fixture', options)
    assert continued[continued.index('--account-id') + 1] == ACCOUNT
    assert continued[continued.index('--account-owner') + 1] == OWNER_DIGEST
    assert 'First fixture' not in continued
    assert continued[continued.index('--resume-session') + 1] == 'native-original'


def test_adapter_rejects_invalid_account_binding(tmp_path):
    adapter = AgyAdapter()
    with pytest.raises(UnsafeInstruction):
        adapter.bind_account(tmp_path, '../profile', OWNER_DIGEST)


def test_unconfirmed_logout_fence_cannot_be_reenabled_as_ready(tmp_path):
    from tools.providers.agy_accounts import AgyAccounts
    accounts = AgyAccounts(tmp_path / 'accounts')
    accounts.apply(ACCOUNT, 'create', 1, owner_digest=OWNER_DIGEST)
    accounts.apply(ACCOUNT, 'login', 2, owner_digest=OWNER_DIGEST)
    accounts.set_state(ACCOUNT, 2, 'ready')
    # Crash immediately after the durable fence, before native logout/report.
    accounts.apply(ACCOUNT, 'logout', 3, owner_digest=OWNER_DIGEST)
    assert accounts.apply(ACCOUNT, 'enable', 4, owner_digest=OWNER_DIGEST)['state'] == 'signed_out'
    with pytest.raises(ValueError, match='provider_account_not_ready'):
        with accounts.runtime(ACCOUNT, owner_digest=OWNER_DIGEST):
            pass


def test_capture_publishes_only_safe_account_identity(tmp_path):
    from test_review_node_runner_capture import _cfg, _task, _Hub
    from tools.session.runner_capture import RunnerCapture
    capture = RunnerCapture(_cfg(tmp_path), {**_task(), 'agent_type': 'agy',
        'provider_account_id': ACCOUNT, 'provider_account_owner': OWNER_DIGEST}, _Hub(status=503))
    try:
        events = capture.bridge.spool.read_after(0, 100, 262144)
        metadata = next(e for e in events if e['kind'] == 'session_metadata')
        assert metadata['payload'] == {'task_id': _task()['task_id'], 'provider_account_id': ACCOUNT}
        assert OWNER_DIGEST not in json.dumps(events)
        from session_schema import validate_event
        clean = validate_event({**metadata, 'payload': {**metadata['payload'],
            'provider_account_id': '../private/profile'}})
        assert 'provider_account_id' not in clean['payload']
    finally:
        capture.bridge.close()


def load_runner():
    spec = importlib.util.spec_from_file_location('provider_account_runner',
        Path(__file__).parents[1] / 'tools/agent-runner.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('managed', [True, False])
def test_only_managed_runner_declares_account_lease_capability(monkeypatch, managed):
    runner = load_runner()
    sent = []
    monkeypatch.setattr(runner, 'flush_pending', lambda cfg: None)
    def post(cfg, path, body):
        sent.append((path, body))
        return 200, {'ok': True, 'task': None}
    monkeypatch.setattr(runner, 'post_json', post)
    assert not runner.poll_once(SimpleNamespace(runner_id='fixture', managed_enabled=managed))
    assert sent == [('/api/commands/poll', {'runner_id': 'fixture', **(
        {'provider_accounts_schema': 1} if managed else {})})]


def test_unmanaged_runner_rejects_account_before_capture_worktree_or_spawn(tmp_path, monkeypatch):
    runner = load_runner()
    submitted = []
    monkeypatch.setattr(runner, '_submit_result', lambda *args, **kwargs: submitted.append(args))
    monkeypatch.setattr(runner, '_start_capture', lambda *args, **kwargs: pytest.fail('must not capture'))
    monkeypatch.setattr(runner.worktree, 'create_worktree', lambda *args: pytest.fail('must not create'))
    task = {**REQUEST, 'task_id': 't1', 'attempt_id': 'a1', 'nonce': 'n1',
            'provider_account_owner': OWNER_DIGEST}
    result = runner.run_task(SimpleNamespace(projects={'fleet': tmp_path}), task)
    assert result['exit_code'] == 126
    assert submitted[0][3].log_tail == 'provider_accounts_require_managed_runner'


def test_owned_worker_recipe_drops_ambient_google_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'terminal-google-secret')
    monkeypatch.setenv('GOOGLE_APPLICATION_CREDENTIALS', '/terminal/google.json')
    adapter = AgyAdapter(environment={'GEMINI_HOME': '/terminal/profile'})
    assert adapter.child_env()['GEMINI_API_KEY'] == 'terminal-google-secret'
    adapter.bind_account(tmp_path / 'accounts', ACCOUNT, OWNER_DIGEST)
    assert all(key not in adapter.execution_env(tmp_path) for key in
               ('GEMINI_HOME', 'GEMINI_API_KEY', 'GOOGLE_APPLICATION_CREDENTIALS'))


def test_account_column_migration_failure_rolls_back_then_recovers(tmp_path, monkeypatch):
    path = tmp_path / 'tasks.db'
    schema = Path(__file__).parent / 'fixtures/task_schema_pre_v4.sql'
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(schema.read_text(encoding='utf-8'))
        before = '\n'.join(conn.iterdump())
    repo = SqliteTaskRepository(path)
    original = repo._connect

    def unavailable():
        conn = original()
        conn.set_authorizer(lambda action, first, second, *_: sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_INSERT and first == 'task_repository_meta'
            else sqlite3.SQLITE_OK)
        return conn

    monkeypatch.setattr(repo, '_connect', unavailable)
    with pytest.raises(sqlite3.DatabaseError):
        repo.init()
    with closing(sqlite3.connect(path)) as conn:
        assert '\n'.join(conn.iterdump()) == before
    monkeypatch.setattr(repo, '_connect', original)
    repo.init()
    create(repo)


def test_corrupt_binding_value_refuses_without_fallback(tmp_path):
    repo = repository(tmp_path)
    task, _ = create(repo)
    with closing(repo._connect()) as conn:
        conn.execute('UPDATE tasks SET provider_account_id=? WHERE task_id=?',
                     ('../default-profile', task['task_id']))
        conn.commit()
    with pytest.raises(sqlite3.DatabaseError):
        repo.init()
    with closing(repo._connect()) as conn:
        assert conn.execute('SELECT provider_account_id FROM tasks').fetchone()[0] == '../default-profile'


def test_concurrent_account_bound_submissions_persist_one_task(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    repo = repository(tmp_path)
    barrier = Barrier(4)

    def submit(_):
        barrier.wait(timeout=5)
        return create(repo)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, range(4)))
    assert sum(made for _, made in results) == 1
    assert len({row['task_id'] for row, _ in results}) == 1
    assert {row['provider_account_id'] for row, _ in results} == {ACCOUNT}
    assert len(repo.list_tasks()) == 1


@pytest.mark.parametrize('bindings', [(), [(ACCOUNT, 'other@example.test')], [(OTHER, OWNER)]])
def test_delivery_grants_require_the_exact_account_owner_pair(tmp_path, bindings):
    repo = repository(tmp_path)
    task, _ = create(repo)
    original = repo.get_task(task['task_id'])
    with closing(repo._connect()) as conn:
        before = '\n'.join(conn.iterdump())
    assert repo.lease_task(machine='mac-local', runner_id='fixture',
                          provider_account_bindings=bindings) is None
    with closing(repo._connect()) as conn:
        assert '\n'.join(conn.iterdump()) == before
    assert repo.get_task(task['task_id']) == original


def test_delivery_grants_do_not_cross_machine_or_agent_family(tmp_path):
    repo = repository(tmp_path)
    task, _ = create(repo)
    assert repo.lease_task(machine='other', runner_id='fixture',
                          provider_account_bindings=[(ACCOUNT, OWNER)]) is None
    # Damaged storage cannot turn a Codex task into an account-bound AGY lease.
    with closing(repo._connect()) as conn:
        conn.execute("UPDATE tasks SET agent_type='codex' WHERE task_id=?", (task['task_id'],))
    assert repo.lease_task(machine='mac-local', runner_id='fixture',
                          provider_account_bindings=[(ACCOUNT, OWNER)]) is None


def test_delivery_grants_use_connection_local_rows_without_variable_limits(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    for number in range(130):
        create(repo, client_token='blocked-' + str(number), provider_account_id=OTHER)
    allowed, _ = create(repo, client_token='allowed-tail')
    original_connect = repo._connect

    def limited_connect():
        conn = original_connect()
        if hasattr(conn, 'setlimit'):
            conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 32)
        return conn

    monkeypatch.setattr(repo, '_connect', limited_connect)
    bindings = [('pa_' + format(number, '032x'), OWNER) for number in range(1500)] + [(ACCOUNT, OWNER)]
    leased = repo.lease_task(machine='mac-local', runner_id='fixture', provider_account_bindings=bindings)
    assert leased['task_id'] == allowed['task_id']
    with closing(repo._connect()) as conn:
        assert not conn.execute('SELECT name FROM sqlite_temp_master').fetchall()
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='task_delivery_accounts'").fetchall()
        assert conn.execute("SELECT count(*) FROM tasks WHERE state='queued'").fetchone()[0] == 130
        assert conn.execute('SELECT count(*) FROM leases').fetchone()[0] == 1


def test_concurrent_delivery_polls_create_one_bound_lease(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    repo = repository(tmp_path)
    task, _ = create(repo)
    barrier = Barrier(4)

    def poll(number):
        barrier.wait(timeout=5)
        return repo.lease_task(machine='mac-local', runner_id='runner-' + str(number),
                               provider_account_bindings=[(ACCOUNT, OWNER)])

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(poll, range(4)))
    assert [row['task_id'] for row in results if row] == [task['task_id']]
    with closing(repo._connect()) as conn:
        assert conn.execute('SELECT count(*) FROM leases').fetchone()[0] == 1


def test_bound_lease_failure_rolls_back_and_grants_do_not_survive_next_connection(tmp_path):
    repo = repository(tmp_path)
    task, _ = create(repo)
    with closing(repo._connect()) as conn:
        conn.execute("CREATE TRIGGER reject_lease BEFORE INSERT ON leases BEGIN SELECT RAISE(ABORT,'fixture'); END")
        before = '\n'.join(conn.iterdump())
    with pytest.raises(sqlite3.IntegrityError):
        repo.lease_task(machine='mac-local', runner_id='fixture', provider_account_bindings=[(ACCOUNT, OWNER)])
    with closing(repo._connect()) as conn:
        assert '\n'.join(conn.iterdump()) == before
        conn.execute('DROP TRIGGER reject_lease')
    assert repo.lease_task(machine='mac-local', runner_id='fixture') is None
    assert repo.lease_task(machine='mac-local', runner_id='fixture',
                          provider_account_bindings=[(ACCOUNT, OWNER)])['task_id'] == task['task_id']
