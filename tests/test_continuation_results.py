"""Per-turn Git, immutable SQLite, authenticated HTTP and durable upload behavior."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from fleet_core.continuation_result import validate_result
from hub.domain.continuation_results import ContinuationResultStoreError
from hub.infrastructure.continuation_result_repository import ContinuationResultRepository
from tools.continuation_results import compare_trees, snapshot_tree, ContinuationResultUnavailable
from tools.supervisor.continuation_result_upload import ContinuationResultUpload


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def project(tmp_path):
    from tools.worktree import create_worktree
    if os.name == 'nt':
        # Hosted Windows C:\ inherits Authenticated Users modify rights.
        # Set up a private workspace case without changing the host ACL.
        from tools.platform.artifact_windows import root_handle
        tmp_path = tmp_path / 'owned'
        with root_handle(tmp_path, create_private=True):
            pass
    root = tmp_path / 'project'
    root.mkdir()
    git(root, 'init')
    git(root, 'config', 'user.name', 'Fixture')
    git(root, 'config', 'user.email', 'fixture@example.test')
    (root / 'app.txt').write_text('original\n', encoding='utf-8')
    git(root, 'add', 'app.txt')
    git(root, 'commit', '-m', 'initial')
    worktree = create_worktree(root, 'task-turn')
    return root, worktree


def result(command_id='cmd_turn_1', session_id='sess_turn_1', task_id='task-turn', **kwargs):
    value = {'command_id': command_id, 'session_id': session_id, 'task_id': task_id,
        'execution_state': 'succeeded', 'collection_state': 'complete', 'collection_reason': '',
        'exit_code': 0, 'duration_s': 1.5, 'finished_at': '2026-11-23T12:00:00Z',
        'diff_stat': ' app.txt | 1 +\n', 'diff_patch': '+new turn\n', 'test_summary': None,
        'diff_truncated': False, 'stat_truncated': False, 'redacted': False}
    return {**value, **kwargs}


def test_dirty_baseline_only_reports_current_turn_and_never_changes_index(project):
    root, worktree = project
    (worktree / 'app.txt').write_text('earlier turn\n', encoding='utf-8')
    git(worktree, 'add', 'app.txt')
    (worktree / 'previous.txt').write_text('prior untracked\n', encoding='utf-8')
    original_index = git(worktree, 'write-tree')
    original_head = git(worktree, 'rev-parse', 'HEAD')
    before = snapshot_tree(root, 'task-turn')
    (worktree / 'app.txt').write_text('earlier turn\nthis turn\n', encoding='utf-8')
    (worktree / 'new.txt').write_text('new output\n', encoding='utf-8')
    (worktree / 'test-results.json').write_text(json.dumps({'framework': 'pytest', 'passed': 3}), encoding='utf-8')
    current = compare_trees(root, 'task-turn', before, snapshot_tree(root, 'task-turn'))
    assert '+this turn' in current['diff_patch']
    assert '+earlier turn' not in current['diff_patch']
    assert 'previous.txt' not in current['diff_patch']
    assert 'new.txt' in current['diff_patch']
    assert current['test_summary'] == {'framework': 'pytest', 'passed': 3}
    assert git(worktree, 'write-tree') == original_index
    assert git(worktree, 'rev-parse', 'HEAD') == original_head
    # Next unchanged turn cannot claim the previous test run.
    after = snapshot_tree(root, 'task-turn')
    unchanged = compare_trees(root, 'task-turn', after, snapshot_tree(root, 'task-turn'))
    assert unchanged['diff_patch'] == '' and unchanged['test_summary'] is None


def test_snapshot_includes_deletions_and_rejects_unsafe_worktree(project, tmp_path):
    root, worktree = project
    before = snapshot_tree(root, 'task-turn')
    # The target is a fixture file in this test's temporary owned worktree.
    (worktree / 'app.txt').unlink()
    delta = compare_trees(root, 'task-turn', before, snapshot_tree(root, 'task-turn'))
    assert '-original' in delta['diff_patch']
    assert 'deleted file mode' in delta['diff_patch']
    if os.name != 'nt':
        (worktree / 'escape.txt').symlink_to(tmp_path / 'outside')
        with pytest.raises(ContinuationResultUnavailable):
            snapshot_tree(root, 'task-turn')


def test_non_git_and_unknown_project_results_are_unavailable(tmp_path):
    with pytest.raises(ContinuationResultUnavailable):
        snapshot_tree(tmp_path, 'task-turn')


def test_repository_atomic_old_store_migration_concurrent_upload_and_conflict(tmp_path):
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        connection.execute("INSERT INTO meta VALUES ('schema_version','5')")
        connection.execute('CREATE TABLE sessions (session_id TEXT PRIMARY KEY)')
        connection.execute("INSERT INTO sessions VALUES ('existing')")
    repo = ContinuationResultRepository(path)
    repo.init()
    with ThreadPoolExecutor(max_workers=8) as pool:
        stored = list(pool.map(lambda _: repo.put('mac', result()), range(8)))
    assert sum(first for _, first in stored) == 1
    assert len({row['cursor'] for row, _ in stored}) == 1
    assert repo.get('sess_turn_1', 'cmd_turn_1')['diff_patch'] == '+new turn\n'
    assert 'diff_patch' not in repo.page('sess_turn_1', limit=10, before=None)['results'][0]
    with pytest.raises(ContinuationResultStoreError, match='continuation_result_conflict'):
        repo.put('mac', result(diff_patch='+different\n'))
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '5'
        assert connection.execute('SELECT * FROM sessions').fetchone() == ('existing',)


@pytest.mark.parametrize('kind', ['future', 'partial', 'corrupt'])
def test_result_schema_unknown_partial_and_corrupt_fail_closed_without_rewrite(tmp_path, kind):
    path = tmp_path / 'results.db'
    if kind == 'corrupt':
        path.write_bytes(b'not sqlite')
    else:
        with sqlite3.connect(path) as connection:
            connection.execute('CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            if kind == 'future':
                connection.execute("INSERT INTO meta VALUES ('continuation_results_schema','2')")
            else:
                connection.execute('CREATE TABLE continuation_results (command_id TEXT)')
    with pytest.raises(ContinuationResultStoreError):
        ContinuationResultRepository(path).init()
    if kind != 'corrupt':
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT value FROM meta WHERE key='continuation_results_schema'").fetchone() == (('2',) if kind == 'future' else None)


def test_result_cursor_pages_remain_stable_during_new_upload(tmp_path):
    repo = ContinuationResultRepository(tmp_path / 'results.db')
    repo.init()
    for n in range(5):
        repo.put('mac', result(command_id=f'cmd_{n}'))
    first = repo.page('sess_turn_1', limit=2, before=None)
    repo.put('mac', result(command_id='cmd_later'))
    rest = repo.page('sess_turn_1', limit=5, before=int(first['before_cursor']))
    ids = [row['command_id'] for row in first['results'] + rest['results']]
    assert ids == ['cmd_4', 'cmd_3', 'cmd_2', 'cmd_1', 'cmd_0']
    with pytest.raises(ContinuationResultStoreError, match='invalid_cursor'):
        repo.page('sess_turn_1', limit=5, before=1000)


def test_result_validation_rejects_overflow_unknown_fields_and_redacts_secrets():
    with pytest.raises(ValueError):
        validate_result(result(diff_patch='x' * 102401))
    with pytest.raises(ValueError):
        validate_result({**result(), 'cwd': '/private/path'})
    with pytest.raises(ValueError):
        validate_result(result(duration_s=float('inf')))
    clean = validate_result(result(diff_patch='+Authorization: Bearer sk-sensitive-fixture-1234567890\n'))
    assert 'sk-sensitive-fixture-1234567890' not in clean['diff_patch']


@pytest.mark.parametrize('change', [
    {'execution_state': []}, {'collection_state': {}}, {'collection_reason': []},
    {'duration_s': 10**10000}, {'duration_s': True}, {'duration_s': -1},
    {'finished_at': '2026-99-99T99:99:99Z'}, {'test_summary': []},
    {'diff_patch': '\ud800'}, {'exit_code': True},
])
def test_result_validation_has_bounded_errors_for_untrusted_shapes(change):
    with pytest.raises(ValueError, match='invalid_continuation_result'):
        validate_result(result(**change))


def test_upload_http_response_loss_replays_saved_data_without_execution(tmp_path):
    accepted, calls = {}, []
    def post(cfg, path, body):
        calls.append(body)
        accepted.setdefault(body['command_id'], body)
        if len(calls) == 1:
            raise OSError('response lost after commit')
        return 200, {'ok': True, 'command_id': body['command_id']}
    cfg = SimpleNamespace(cache_dir=tmp_path)
    uploader = ContinuationResultUpload(cfg, post)
    uploader.save(result())
    uploader.replay()
    restarted = ContinuationResultUpload(cfg, post)
    restarted.replay()
    restarted.replay()
    assert len(calls) == 2 and calls[0] == calls[1]
    assert len(accepted) == 1
    assert 'diff_patch' not in (tmp_path / 'continuation-results/acknowledged/cmd_turn_1.json').read_text()


def test_acknowledged_and_corrupt_spool_entries_cannot_starve_new_uploads(tmp_path):
    sent = []
    def post(cfg, path, body):
        sent.append(body['command_id'])
        return 200, {'command_id': body['command_id']}
    uploader = ContinuationResultUpload(SimpleNamespace(cache_dir=tmp_path), post)
    for n in range(70):
        uploader.save(result(command_id=f'cmd_{n}'))
    for _ in range(3):
        uploader.replay()
    assert len(sent) == 70 and len(set(sent)) == 70
    broken = uploader.root / 'cmd_corrupt.json'
    broken.write_text('invalid json', encoding='utf-8')
    if os.name != 'nt':
        broken.chmod(0o600)
    uploader.replay()
    assert not broken.exists() and (uploader.root / 'invalid/cmd_corrupt.json').read_text() == 'invalid json'
    uploader.save(result(command_id='cmd_final'))
    uploader.replay()
    assert sent[-1] == 'cmd_final'


def test_result_store_rejects_symlink_cache_without_writing_target(tmp_path):
    if os.name == 'nt':
        pytest.skip('Native junction and ADS boundary is covered by Windows kernel probe')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (tmp_path / 'continuation-results').symlink_to(outside, target_is_directory=True)
    uploader = ContinuationResultUpload(SimpleNamespace(cache_dir=tmp_path), lambda *_: None)
    with pytest.raises(ValueError, match='unsafe_result_store'):
        uploader.save(result())
    assert not list(outside.iterdir())


@pytest.fixture
def result_hub(tmp_path):
    if os.name == 'nt':
        pytest.skip('Hub signed-command store requires POSIX ownership; node collection tests run on Windows')
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator='owner@example.test',
        supervisor_enabled=True, supervisor_signing_raw=bytes(range(32)),
        session_repositories_enabled=True, append_user_turn_enabled=True,
        runner_credentials={'mac': 'runner-credential', 'other': 'other-credential'}))
    services = app.extensions['fleet']['services']
    task_repo = services['tasks'].task_repo
    task, _ = task_repo.create_task(machine='mac', project='fixture', agent_type='codex',
                                   instruction='first', requested_by='owner@example.test')
    # The real repository preserves exact-attempt bindings through leases/results.
    sid = 'sess_turn_1'
    services['sessions'].session_repo.upsert_session({'session_id': sid, 'machine_id': 'mac',
        'managed': True, 'process_group_id': 'grp_turn', 'capture_quality': 'structured',
        'agent_family': 'codex', 'attempt_id': task['attempt_id']})
    command = services['supervisor'].enqueue('mac', sid, task['attempt_id'],
        'append_user_turn', 'operator_requested', payload={'text': 'follow'})
    services['supervisor'].poll('mac')
    return app, services, result(command_id=command['command_id'], task_id=task['task_id'])


def test_real_http_runner_binding_operator_reads_and_startup_task_unchanged(result_hub):
    app, services, value = result_hub
    client = app.test_client()
    original = services['tasks'].get(value['task_id'])
    endpoint = '/api/runner-continuation-results'
    assert client.post(endpoint, json=value).status_code == 403
    wrong = client.post(endpoint, json=value, headers={'X-Runner-Credential': 'other:other-credential'})
    assert wrong.status_code == 403
    assert value['command_id'] not in wrong.get_data(as_text=True)
    accepted = client.post(endpoint, json=value, headers={'X-Runner-Credential': 'mac:runner-credential'})
    assert accepted.status_code == 200
    replay = client.post(endpoint, json=value, headers={'X-Runner-Credential': 'mac:runner-credential'})
    assert replay.status_code == 200 and replay.get_json()['stored'] is False
    assert services['tasks'].get(value['task_id']) == original
    page = client.get('/api/sessions/sess_turn_1/results')
    assert page.status_code == 200 and len(page.get_json()['results']) == 1
    assert 'diff_patch' not in page.get_json()['results'][0]
    detail = client.get('/api/sessions/sess_turn_1/results/' + value['command_id'])
    assert detail.status_code == 200 and detail.get_json()['result']['diff_patch'] == value['diff_patch']
    assert client.get('/api/sessions/sess_turn_1/results?before=00').status_code == 400
    # Machine credentials cannot stand in for operator authorization.
    assert client.get('/api/sessions/sess_turn_1/results', headers={'X-Runner-Credential': 'mac:runner-credential'}).status_code == 401


def test_sqlite_failure_and_corrupt_result_are_bounded(result_hub, monkeypatch):
    app, services, value = result_hub
    def unavailable():
        raise sqlite3.DatabaseError('/private/path PASSWORD=secret')
    monkeypatch.setattr(services['continuation_results'].repository, '_connect', unavailable)
    response = app.test_client().get('/api/sessions/sess_turn_1/results')
    assert response.status_code == 503
    assert '/private/path' not in response.get_data(as_text=True)
    assert 'secret' not in response.get_data(as_text=True)


def test_migration_rolls_back_created_tables_when_index_creation_fails(tmp_path):
    path = tmp_path / 'rollback.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        conn.execute("INSERT INTO meta VALUES ('schema_version','5')")
        # An unrelated old object using the index name must fail, atomically.
        conn.execute('CREATE TABLE idx_continuation_command (preserved TEXT)')
        conn.execute("INSERT INTO idx_continuation_command VALUES ('existing')")
    with pytest.raises(ContinuationResultStoreError):
        ContinuationResultRepository(path).init()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='continuation_results'").fetchone() is None
        assert conn.execute('SELECT * FROM meta').fetchall() == [('schema_version', '5')]
        assert conn.execute('SELECT * FROM idx_continuation_command').fetchone() == ('existing',)


def test_corrupt_stored_identity_and_digest_fail_closed(tmp_path):
    path = tmp_path / 'corrupt-row.db'
    repo = ContinuationResultRepository(path)
    repo.init()
    repo.put('mac', result())
    with sqlite3.connect(path) as conn:
        changed = result(session_id='another_session', diff_patch='private prior body')
        conn.execute('UPDATE continuation_results SET result_json=?', (json.dumps(changed),))
    with pytest.raises(ContinuationResultStoreError):
        repo.get('sess_turn_1', 'cmd_turn_1')
    with pytest.raises(ContinuationResultStoreError):
        repo.page('sess_turn_1', limit=10, before=None)
    with pytest.raises(ContinuationResultStoreError):
        repo.put('mac', result())


@pytest.mark.parametrize('change', [
    {'task_id': 'other-task'}, {'session_id': 'unknown_session'},
    {'command_id': 'unknown_command'}, {'duration_s': -1}, {'execution_state': []},
])
def test_http_rejects_invalid_binding_and_untrusted_shape_without_data(result_hub, change):
    app, services, value = result_hub
    response = app.test_client().post('/api/runner-continuation-results', json={**value, **change},
        headers={'X-Runner-Credential': 'mac:runner-credential'})
    assert response.status_code in {400, 403, 404}
    assert 'diff_patch' not in response.get_data(as_text=True)
    assert value['command_id'] not in response.get_data(as_text=True)
    assert services['continuation_results'].repository.page('sess_turn_1', limit=10, before=None)['results'] == []


def test_http_conflict_and_task_database_failure_are_bounded(result_hub, monkeypatch):
    app, services, value = result_hub
    client = app.test_client()
    headers = {'X-Runner-Credential': 'mac:runner-credential'}
    assert client.post('/api/runner-continuation-results', json=value, headers=headers).status_code == 200
    conflict = client.post('/api/runner-continuation-results', json={**value, 'diff_patch': 'new private body'}, headers=headers)
    assert conflict.status_code == 409
    assert value['command_id'] not in conflict.get_data(as_text=True)
    assert 'private body' not in conflict.get_data(as_text=True)
    def fail(_):
        raise sqlite3.DatabaseError('PRIVATE_DB_PASSWORD')
    monkeypatch.setattr(services['tasks'].task_repo, 'get_task_for_attempt', fail)
    failed = client.post('/api/runner-continuation-results', json=value, headers=headers)
    assert failed.status_code == 503 and 'PRIVATE_DB_PASSWORD' not in failed.get_data(as_text=True)


@pytest.mark.parametrize('exit_code', [0, 7])
@pytest.mark.skipif(os.name == 'nt', reason='Hub signed-command store requires POSIX ownership')
def test_real_supervisor_turn_collects_only_dirty_delta_and_preserves_startup(project, tmp_path, monkeypatch, exit_code):
    import sys
    import time
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    from tools.runner_config import RunnerConfig
    from tools.session.runner_capture import RunnerCapture, session_prefix
    from tools.supervisor.supervisor import Supervisor
    from tools.supervisor.continuation_capture import ContinuationCapture
    from tools.supervisor import native_resume, continuation_capture
    from tools.worktree import create_worktree

    root, _ = project
    app = create_app(FleetConfig.from_root(tmp_path / 'hub-integrated', dev_operator='owner@example.test',
        session_repositories_enabled=True, supervisor_enabled=True, supervisor_signing_raw=bytes(range(32)),
        runner_credentials={'mac': 'runner-credential'}, append_user_turn_enabled=True))
    services = app.extensions['fleet']['services']
    task, _ = services['tasks'].task_repo.create_task(machine='mac', project='fixture', agent_type='codex',
        instruction='original startup', requested_by='owner@example.test')
    worktree = create_worktree(root, task['task_id'])
    (worktree / 'app.txt').write_text('original\nprevious dirty work\n', encoding='utf-8')
    client = app.test_client()
    uploaded = []
    def post(cfg, path, body):
        response = client.post(path, json=body, headers={'X-Runner-Credential': 'mac:runner-credential'})
        assert response.status_code == 200, response.get_data(as_text=True)
        if path == '/api/runner-continuation-results':
            uploaded.append(body)
        return response.status_code, response.get_json()
    cfg = RunnerConfig(hub='https://localhost', machine='mac', credential='runner-credential', runner_id='mac',
        projects={'fixture': root}, agents={}, cache_dir=tmp_path / 'cache-integrated')
    sv = Supervisor(tmp_path / 'manifests-integrated', 'mac')
    sid = session_prefix('mac') + task['attempt_id']
    script = tmp_path / 'independent_native_fixture.py'
    script.write_text('print(\'{"type":"thread.started","thread_id":"fixture-native-id"}\')', encoding='utf-8')
    manifest = sv.launch(agent='codex', command=[sys.executable, str(script)],
        cwd=str(worktree), env_allowlist={}, session_id=sid, attempt_id=task['attempt_id'])
    initial = RunnerCapture(cfg, task, post, process_group_id=manifest.process_group_id)
    assert sv.wait_session(sid, timeout_s=5, on_line=initial.line).exit_code == 0
    initial.finish(0, resumable=True)
    initial.bridge.close()
    startup = services['tasks'].get(task['task_id'])
    command = services['supervisor'].enqueue('mac', sid, task['attempt_id'],
        'append_user_turn', 'operator_requested', payload={'text': 'turn'})
    services['supervisor'].poll('mac')
    def argv(family, exe, token, text):
        assert token == 'fixture-native-id'
        code = ('from pathlib import Path; import json; '
            'Path("app.txt").write_text("original\\nprevious dirty work\\nonly current turn\\n"); '
            'print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"done"}})); '
            f'raise SystemExit({exit_code})')
        return [sys.executable, '-c', code]
    monkeypatch.setattr(native_resume, '_native_resume_argv', argv)
    monkeypatch.setattr(continuation_capture, 'post', post)
    controller = ContinuationCapture(cfg, sv)
    assert controller.send(sid, 'turn', command_id=command['command_id']) == 'appended'
    deadline = time.monotonic() + 12
    while (controller.active or controller._replay_worker and controller._replay_worker.is_alive()) and time.monotonic() < deadline:
        time.sleep(.02)
    assert not controller.active and len(uploaded) == 1
    row = client.get(f'/api/sessions/{sid}/results/{command["command_id"]}').get_json()['result']
    assert row['execution_state'] == ('succeeded' if exit_code == 0 else 'failed')
    assert row['collection_state'] == 'complete'
    assert '+only current turn' in row['diff_patch'] and '+previous dirty work' not in row['diff_patch']
    assert services['tasks'].get(task['task_id']) == startup


def test_windows_source_reader_uses_pinned_handle_and_bounded_read(monkeypatch, tmp_path):
    from contextlib import contextmanager
    import io
    from fleet_core.artifact_receive import ArtifactReceiveError
    from tools.platform import artifact_windows
    from tools.worktree import _source_file_windows, WorktreeError
    reads = []
    class Reader(io.BytesIO):
        def read(self, count=-1):
            reads.append(count)
            return super().read(count)
    @contextmanager
    def source(root, name):
        assert root == tmp_path and name == 'nested/source.txt'
        yield Reader(b'hello'), {'pinned': 'identity'}
    monkeypatch.setattr(artifact_windows, 'source_handle', source)
    assert _source_file_windows(tmp_path, b'nested/source.txt', 5) == (b'hello', '100644')
    assert reads == [6]
    with pytest.raises(WorktreeError, match='source_archive_too_large'):
        _source_file_windows(tmp_path, b'nested/source.txt', 4)
    @contextmanager
    def missing(*_):
        try:
            raise FileNotFoundError('private path')
        except FileNotFoundError as exc:
            raise ArtifactReceiveError('unsafe_input_path') from exc
        yield
    monkeypatch.setattr(artifact_windows, 'source_handle', missing)
    assert _source_file_windows(tmp_path, b'missing', 100) is None
    @contextmanager
    def reparse(*_):
        raise ArtifactReceiveError('unsafe_input_path')
        yield
    monkeypatch.setattr(artifact_windows, 'source_handle', reparse)
    with pytest.raises(WorktreeError, match='source_file_boundary_unavailable'):
        _source_file_windows(tmp_path, b'unsafe:ads', 100)


@pytest.mark.parametrize('owner,writer,accepted', [
    ('current-user', 'S-1-3-4', True),
    ('S-1-5-32-544', 'S-1-3-4', True),
    ('S-1-5-18', 'S-1-3-4', True),
    ('current-user', 'current-user', True),
    ('current-user', 'S-1-5-32-544', True),
    ('foreign-user', 'S-1-3-4', False),
    ('foreign-user', 'current-user', False),
    ('current-user', 'S-1-1-0', False),
    ('S-1-5-32-544', 'S-1-5-32-545', False),
])
def test_windows_acl_owner_rights_resolve_only_after_trusted_owner(owner, writer, accepted):
    """Portable policy test; real handle/ACL races run in the NT kernel suite."""
    import ctypes as C
    from fleet_core.artifact_receive import ArtifactReceiveError
    from tools.platform import artifact_windows as win

    ace = win._ACE(0, 0, 16, 0x1f01ff, 0)
    acl = (win.U8 * 8)(2)
    def security_info(_handle, _kind, _flags, owner_out, _group, acl_out, _sacl, descriptor):
        C.cast(owner_out, C.POINTER(win.HANDLE))[0] = 12345
        C.cast(acl_out, C.POINTER(win.HANDLE))[0] = C.addressof(acl)
        C.cast(descriptor, C.POINTER(win.HANDLE))[0] = 12346
        return 0
    def acl_info(_acl, output, _size, _kind):
        C.cast(output, C.POINTER(win._ACLSize)).contents.AceCount = 1
        return 1
    def get_ace(_acl, _index, output):
        C.cast(output, C.POINTER(win.HANDLE))[0] = C.addressof(ace)
        return 1
    def descriptor_control(_descriptor, control, revision):
        C.cast(control, C.POINTER(win.U16))[0] = 0x8004
        C.cast(revision, C.POINTER(win.U32))[0] = 1
        return 1
    native = object.__new__(win._Native)
    native.current_user = lambda: 'current-user'
    native.sid = lambda pointer: owner if pointer == 12345 else writer
    native.kernel = SimpleNamespace(LocalFree=lambda _: None)
    native.security = SimpleNamespace(GetSecurityInfo=security_info, GetAclInformation=acl_info,
        GetAce=get_ace, GetSecurityDescriptorControl=descriptor_control)
    if accepted:
        actual_owner, digest = native.security_identity(1)
        assert actual_owner == owner and len(digest) == 64
    else:
        with pytest.raises(ArtifactReceiveError, match='unsafe_input_path'):
            native.security_identity(1)


def test_frontend_turn_parser_and_service_reject_cross_session_results():
    script = r'''
import assert from 'node:assert/strict';
import {parseContinuationResults} from './frontend/assets/api/continuation-results.js';
import {createNativeChatService} from './frontend/assets/views/agent-chat/service.js';
const row = {command_id:'cmd_x',session_id:'s',task_id:'t',cursor:'1',execution_state:'succeeded',
  collection_state:'complete',collection_reason:'',exit_code:0,duration_s:1,finished_at:'2026-11-23T12:00:00Z',
  diff_stat:'',diff_patch:'+literal',diff_truncated:false,stat_truncated:false,redacted:false,test_summary:null,cwd:'/private'};
const page = parseContinuationResults({session_id:'s',results:[row],has_more:false,before_cursor:'1'});
assert.equal(page.results[0].cwd,undefined); assert.equal(page.results[0].diff_patch,undefined);
assert.equal(parseContinuationResults({session_id:'s',result:row},{detail:true}).result.diff_patch,'+literal');
for (const changed of [{cursor:'0'},{cursor:'01'},{cursor:'9223372036854775808'},{exit_code:2**32},
  {duration_s:Infinity},{collection_reason:'unsafe-opaque'},{diff_stat:'你'.repeat(5120)}]) {
  assert.throws(() => parseContinuationResults({session_id:'s',result:{...row,...changed}},{detail:true}));
}
const signal = new AbortController().signal; let supplied;
const service = createNativeChatService({
  getContinuationResults:async (id,options) => {supplied=options;return page;},
  getContinuationResult:async () => ({session_id:'s',result:{...row,command_id:'different'}}),
});
await service.readContinuationResults('s',{before:'1',signal});
assert.equal(supplied.signal,signal); assert.equal(supplied.before,'1');
await assert.rejects(service.readContinuationResults('another'),/不一致/);
await assert.rejects(service.readContinuationResult('s','cmd_x',signal),/不一致/);
'''
    response = subprocess.run(['node', '--input-type=module'], cwd=Path(__file__).resolve().parents[1],
        input=script, capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert response.returncode == 0, response.stdout + response.stderr


def recovery_controller(project, tmp_path, *, phase='dispatched', state='terminated', cwd=None):
    from tools.supervisor.continuation_capture import ContinuationCapture
    root, worktree = project
    cfg = SimpleNamespace(cache_dir=tmp_path / 'recovery-cache', projects={'fixture': root})
    manifest = SimpleNamespace(state=state, reason='completed', attempt_id='attempt_1', process_group_id='group_1')
    supervisor = SimpleNamespace(get=lambda _: manifest,
        _entries={'sess_turn_1': SimpleNamespace(cwd=str(cwd or worktree))})
    controller = ContinuationCapture(cfg, supervisor)
    controller.results.save_draft({'version': 1, 'command_id': 'cmd_recover', 'session_id': 'sess_turn_1',
        'task_id': 'task-turn', 'attempt_id': 'attempt_1', 'project': 'fixture',
        'process_group_id': 'group_1', 'phase': phase, 'started_at': 1_700_000_000,
        'before_tree': snapshot_tree(root, 'task-turn'), 'collection_reason': ''})
    return controller


@pytest.mark.parametrize('phase,state', [('prepared', 'terminated'), ('dispatched', 'running')])
def test_crash_recovery_never_reexecutes_prepared_or_live_turn(project, tmp_path, phase, state):
    controller = recovery_controller(project, tmp_path, phase=phase, state=state)
    controller._recover_results()
    data = controller.results._read(controller.results.root / 'cmd_recover.json')['result']
    assert data['execution_state'] == 'unknown' and data['exit_code'] == -1
    assert data['collection_state'] == 'unavailable' and data['diff_patch'] == ''
    assert (controller.results.root / 'drafts-finished/cmd_recover.json').exists()


def test_crash_terminal_recovery_has_delta_but_never_invents_exit_status(project, tmp_path):
    controller = recovery_controller(project, tmp_path)
    (project[1] / 'app.txt').write_text('original\nrecovered delta\n', encoding='utf-8')
    controller._recover_results()
    data = controller.results._read(controller.results.root / 'cmd_recover.json')['result']
    assert data['execution_state'] == 'unknown' and data['exit_code'] == -1
    assert data['collection_state'] == 'complete' and '+recovered delta' in data['diff_patch']


def test_crash_recovery_refuses_different_retained_cwd(project, tmp_path):
    controller = recovery_controller(project, tmp_path, cwd=tmp_path / 'different-workspace')
    (project[1] / 'app.txt').write_text('private unrelated change\n', encoding='utf-8')
    controller._recover_results()
    data = controller.results._read(controller.results.root / 'cmd_recover.json')['result']
    assert data['collection_state'] == 'unavailable' and data['diff_patch'] == ''


def test_finished_and_invalid_drafts_are_archived_once_not_retried(project, tmp_path):
    controller = recovery_controller(project, tmp_path)
    root = controller.results.root
    controller.results._atomic(root / 'drafts/cmd_finished.json', {'command_id': 'cmd_finished', 'finished': True})
    controller.results._atomic(root / 'drafts/cmd_invalid.json', {'command_id': 'cmd_invalid', 'version': 99})
    controller._recover_results()
    assert not (root / 'drafts/cmd_finished.json').exists()
    assert not (root / 'drafts/cmd_invalid.json').exists()
    assert (root / 'drafts-finished/cmd_finished.json').exists()
    assert (root / 'invalid-drafts/cmd_invalid.json').exists()


def test_send_recovers_old_drafts_before_admitting_later_turn(project, tmp_path, monkeypatch):
    controller = recovery_controller(project, tmp_path)
    calls = []
    controller.supervisor.continuation_ready = lambda _: False
    original = controller._recover_results
    def recover(**kwargs):
        calls.append('recovered')
        original(**kwargs)
    monkeypatch.setattr(controller, '_recover_results', recover)
    assert controller.send('sess_turn_1', 'later', 'cmd_later') == 'native_session_unbound'
    assert calls == ['recovered']
    assert (controller.results.root / 'cmd_recover.json').exists()


def test_binary_delta_never_contains_reconstructable_encoded_secret(project):
    root, worktree = project
    before = snapshot_tree(root, 'task-turn')
    secret = b'\x00PRIVATE_BINARY_FIXTURE_SECRET\xff\x00'
    (worktree / 'binary.dat').write_bytes(secret)
    value = compare_trees(root, 'task-turn', before, snapshot_tree(root, 'task-turn'))
    assert 'Binary files' in value['diff_patch'] and 'differ' in value['diff_patch']
    assert 'GIT binary patch' not in value['diff_patch'] and 'PRIVATE_BINARY' not in value['diff_patch']
    with pytest.raises(ValueError, match='invalid_continuation_result'):
        validate_result(result(diff_patch='diff --git a/x b/x\nGIT binary patch\nliteral 2\nsecret'))


@pytest.mark.parametrize('failure_status', [409, 503])
def test_failed_pending_upload_batch_cannot_starve_later_results(tmp_path, failure_status):
    sent = []
    accepted = {'command_id': None}
    def post(cfg, path, body):
        sent.append(body['command_id'])
        code = 200 if body['command_id'] == accepted['command_id'] else failure_status
        return code, {'command_id': body['command_id']}
    uploader = ContinuationResultUpload(SimpleNamespace(cache_dir=tmp_path), post)
    for n in range(33):
        uploader.save(result(command_id=f'cmd_retry_{n}'))
    # Pick the actual final filesystem entry, without assuming lexical scandir order.
    with os.scandir(uploader.root) as entries:
        accepted['command_id'] = list(entries)[-1].name[:-5]
    for _ in range(3):
        uploader.replay()
    assert len(set(sent)) == 33
    assert (uploader.root / 'acknowledged' / (accepted['command_id'] + '.json')).exists()
    if failure_status == 409:
        assert len(list((uploader.root / 'rejected').glob('*.json'))) == 32


@pytest.mark.parametrize('dispatched', [False, True])
def test_expired_commands_require_immutable_dispatch_evidence(result_hub, dispatched):
    app, services, value = result_hub
    supervisor = services['supervisor']
    prior = supervisor.receipt_status(value['command_id'])
    if not dispatched:
        supervisor.receipt(value['command_id'], 'succeeded', 'appended', machine='mac')
        queued = supervisor.enqueue('mac', 'sess_turn_1', prior['attempt_id'], 'append_user_turn',
                                    'operator_requested', payload={'text': 'never polled'})
        value = {**value, 'command_id': queued['command_id']}
    supervisor.enqueue('mac', 'sess_turn_1', prior['attempt_id'], 'terminate_session', 'operator_requested')
    assert supervisor.receipt_status(value['command_id'])['status'] == 'expired'
    response = app.test_client().post('/api/runner-continuation-results', json=value,
        headers={'X-Runner-Credential': 'mac:runner-credential'})
    assert response.status_code == (200 if dispatched else 409)
    if not dispatched:
        assert value['command_id'] not in response.get_data(as_text=True)
        assert services['continuation_results'].repository.page('sess_turn_1', limit=10, before=None)['results'] == []


@pytest.mark.skipif(os.name == 'nt', reason='Hub signed-command store requires POSIX ownership')
def test_dispatch_migration_backfills_only_proven_delivery_and_retains_first_claim(tmp_path):
    from hub.application.supervisor_service import SupervisorService
    from hub.infrastructure.supervisor_command_repository import SupervisorCommandRepository
    now = [100.]
    repo = SupervisorCommandRepository(tmp_path / 'dispatch-store/commands.db')
    service = SupervisorService(signing_key=bytes(range(32)), command_repository=repo,
                                now_fn=lambda: now[0], append_user_turn_enabled=True)
    ids = []
    for n in range(4):
        row = service.enqueue('mac', f'session_{n}', None, 'append_user_turn', 'operator_requested', payload={'text': 'fixture'})
        ids.append(row['command_id'])
    with repo._connect() as conn:
        conn.execute('ALTER TABLE supervisor_commands DROP COLUMN dispatched_s')
        conn.execute("DELETE FROM supervisor_meta WHERE key='dispatch_schema'")
        conn.execute("UPDATE supervisor_commands SET status='delivered' WHERE command_id=?", (ids[0],))
        conn.execute("UPDATE supervisor_commands SET status='expired',receipt_status='accepted' WHERE command_id=?", (ids[1],))
        conn.execute("UPDATE supervisor_commands SET status='expired',receipt_status='expired' WHERE command_id=?", (ids[2],))
    repo.init()
    assert [repo.find_command(cid)['dispatched_s'] > 0 for cid in ids] == [True, True, False, False]
    repo.claim_available('mac', 101., 20, 1.)
    original = repo.find_command(ids[3])['dispatched_s']
    repo.claim_available('mac', 103., 20, 1.)
    assert repo.find_command(ids[3])['dispatched_s'] == original == 101.


@pytest.mark.parametrize('kind', ['future', 'missing', 'wrong_type', 'wrong_default', 'bad_value'])
@pytest.mark.skipif(os.name == 'nt', reason='Hub signed-command store requires POSIX ownership')
def test_dispatch_schema_invalid_or_corrupt_fails_closed_atomically(tmp_path, kind):
    from hub.infrastructure.supervisor_command_repository import SupervisorCommandRepository, SupervisorCommandRepositoryError
    repo = SupervisorCommandRepository(tmp_path / 'dispatch-store/commands.db')
    repo.init()
    with repo._connect() as conn:
        if kind == 'future':
            conn.execute("UPDATE supervisor_meta SET value='2' WHERE key='dispatch_schema'")
        elif kind != 'bad_value':
            conn.execute('ALTER TABLE supervisor_commands DROP COLUMN dispatched_s')
            if kind == 'wrong_type':
                conn.execute('ALTER TABLE supervisor_commands ADD COLUMN dispatched_s TEXT NOT NULL DEFAULT 0')
            elif kind == 'wrong_default':
                conn.execute("ALTER TABLE supervisor_commands ADD COLUMN dispatched_s REAL NOT NULL DEFAULT 'corrupt-dispatch-default'")
        else:
            repo.insert(command_id='cmd_bad', machine='mac', session_id='s', attempt_id=None, action='pause_session',
                nonce='nonce_bad', idempotency_key=None, wire={}, issued_s=1, expires_s=10)
            conn.execute('UPDATE supervisor_commands SET dispatched_s=-1')
        before = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='supervisor_commands'").fetchone()[0]
    with pytest.raises(SupervisorCommandRepositoryError, match='command_unavailable'):
        repo.init()
    with repo._connect() as conn:
        assert conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='supervisor_commands'").fetchone()[0] == before


@pytest.mark.parametrize('index,columns,unique', [
    ('idx_continuation_page', 'machine_id', False),
    ('idx_continuation_command', 'command_id', False),
])
def test_result_migration_rejects_wrong_named_indexes(tmp_path, index, columns, unique):
    repo = ContinuationResultRepository(tmp_path / 'wrong-index.db')
    repo.init()
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute(f'DROP INDEX {index}')
        conn.execute(f'CREATE {"UNIQUE " if unique else ""}INDEX {index} ON continuation_results({columns})')
    with pytest.raises(ContinuationResultStoreError):
        repo.init()


def test_target_session_recovery_cannot_be_hidden_after_first_draft_batch(project, tmp_path):
    controller = recovery_controller(project, tmp_path)
    template = controller.results._read(controller.results.root / 'drafts/cmd_recover.json')
    for n in range(34):
        controller.results.save_draft({**template, 'command_id': f'cmd_batch_{n}', 'session_id': f'sess_batch_{n}'})
    with os.scandir(controller.results.root / 'drafts') as entries:
        target = list(entries)[-1].name[:-5]
    draft = controller.results._read(controller.results.root / 'drafts' / (target + '.json'))
    sid = draft['session_id']
    controller.supervisor._entries[sid] = SimpleNamespace(cwd=str(project[1]))
    controller.supervisor.continuation_ready = lambda _: False
    (project[1] / 'app.txt').write_text('original\nprevious turn only\n', encoding='utf-8')
    assert controller.send(sid, 'later', 'cmd_later') == 'native_session_unbound'
    assert not (controller.results.root / 'drafts' / (target + '.json')).exists()
    saved = controller.results._read(controller.results.root / (target + '.json'))['result']
    assert '+previous turn only' in saved['diff_patch']


@pytest.mark.parametrize('field,value', [('attempt_id', 'later_attempt'), ('process_group_id', 'later_group')])
def test_crash_recovery_refuses_later_session_incarnation(project, tmp_path, field, value):
    controller = recovery_controller(project, tmp_path)
    setattr(controller.supervisor.get('sess_turn_1'), field, value)
    (project[1] / 'app.txt').write_text('another incarnation private work\n', encoding='utf-8')
    controller._recover_results()
    saved = controller.results._read(controller.results.root / 'cmd_recover.json')['result']
    assert saved['execution_state'] == 'unknown' and saved['collection_state'] == 'unavailable'
    assert saved['diff_patch'] == ''


def test_draft_disk_failure_before_append_never_launches_or_terminates_agent(project, tmp_path, monkeypatch):
    from tools.supervisor import continuation_capture
    controller = recovery_controller(project, tmp_path)
    controller._recover_results()
    controller.supervisor.continuation_ready = lambda _: True
    calls = []
    controller.supervisor.append_user_turn = lambda *_: calls.append('append')
    controller.supervisor.terminate_session = lambda *a, **k: calls.append('terminate')
    record = controller.cfg.cache_dir / 'conversations/sess_turn_1.json'
    record.parent.mkdir()
    record.write_text(json.dumps({'task_id': 'task-turn', 'attempt_id': 'attempt_1',
        'project': 'fixture', 'resumable': True}), encoding='utf-8')
    monkeypatch.setattr(continuation_capture, 'RunnerCapture', lambda *a, **k:
        SimpleNamespace(bridge=SimpleNamespace(close=lambda: None), stop_upload=lambda: None))
    def disk_full(_):
        raise OSError('fixture disk full')
    monkeypatch.setattr(controller.results, 'save_draft', disk_full)
    with pytest.raises(OSError, match='fixture disk full'):
        controller.send('sess_turn_1', 'not delivered', 'cmd_disk_failure')
    assert calls == [] and not controller._active_commands


def test_summary_uses_immutable_after_tree_not_later_workspace(project):
    root, worktree = project
    before = snapshot_tree(root, 'task-turn')
    report = worktree / 'test-results.json'
    report.write_text(json.dumps({'framework': 'pytest', 'passed': 3}), encoding='utf-8')
    after = snapshot_tree(root, 'task-turn')
    report.write_text(json.dumps({'framework': 'pytest', 'passed': 999}), encoding='utf-8')
    assert compare_trees(root, 'task-turn', before, after)['test_summary'] == {'framework': 'pytest', 'passed': 3}
