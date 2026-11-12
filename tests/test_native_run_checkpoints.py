"""Real worker restart evidence for durable native progress."""
import pytest

from hub.application.run_worker_service import LocalRunWorkerService
from hub.platform.providers.base import ModelResponse
from tests.test_platform_run_worker import OWNER, _app, _catalog, _queued


class Crash(BaseException):
    pass


@pytest.mark.parametrize('crash_phase', ['receipt', 'ready', 'complete'])
def test_completed_native_progress_survives_new_worker(tmp_path, monkeypatch, crash_phase):
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    run = _queued(app)
    calls = []

    class Provider:
        def complete(self, messages, tools, **kwargs):
            calls.append(messages)
            if len(calls) == 1:
                return ModelResponse(kind='tool_call', tool='workspace.write',
                                     arguments={'path': 'once.txt', 'content': 'first'},
                                     usage={'input_tokens': 3, 'output_tokens': 2})
            return ModelResponse(kind='final', text='complete', usage={'total_tokens': 7})

    worker = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
                                  worker_id='before-crash', provider_factory=lambda _: Provider())
    original = repo.save_runtime_checkpoint

    def crash_after_save(*args, **kwargs):
        result = original(*args, **kwargs)
        checkpoint = args[2]
        if checkpoint['phase'] == crash_phase and (checkpoint['step'] > 1 or crash_phase == 'receipt'):
            raise Crash()
        return result

    monkeypatch.setattr(repo, 'save_runtime_checkpoint', crash_after_save)
    with pytest.raises(Crash):
        worker.run_once(OWNER)
    assert (tmp_path / 'workspace' / 'once.txt').read_text() == 'first'
    # Any replay of the original write would overwrite this external change.
    (tmp_path / 'workspace' / 'once.txt').write_text('preserved')
    monkeypatch.setattr(repo, 'save_runtime_checkpoint', original)
    with repo._connect() as conn:
        conn.execute('UPDATE runs SET lease_expires_at=0 WHERE run_id=?', (run['run_id'],))
    restarted = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
                                     worker_id='after-crash', provider_factory=lambda _: Provider())
    result = restarted.run_once(OWNER)
    assert result['state'] == 'succeeded'
    assert result['result_text'] == 'complete'
    assert len(calls) == 2
    assert calls[1][-1]['role'] == 'tool'
    assert (tmp_path / 'workspace' / 'once.txt').read_text() == 'preserved'
    assert result['usage']['total_tokens'] == 12
    assert result['usage']['provider_requests'] == 2
    public = app.test_client().get(f"/api/platform/v1/runs/{run['run_id']}").get_json()
    assert 'runtime_checkpoint' not in public


@pytest.mark.parametrize('boundary', ['provider', 'tool'])
def test_uncertain_boundary_is_never_replayed(tmp_path, monkeypatch, boundary):
    from hub.platform.tool_broker import ToolBroker
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    run = _queued(app)
    calls = []

    class Provider:
        def complete(self, messages, tools, **kwargs):
            calls.append('provider')
            if boundary == 'provider':
                raise Crash()
            return ModelResponse(kind='tool_call', tool='workspace.write',
                                 arguments={'path': 'once.txt', 'content': 'first'})

    if boundary == 'tool':
        original = ToolBroker.execute

        def crash_after_effect(self, **kwargs):
            original(self, **kwargs)
            raise Crash()

        monkeypatch.setattr(ToolBroker, 'execute', crash_after_effect)
    def worker(name):
        return LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
                                     worker_id=name, provider_factory=lambda _: Provider())
    with pytest.raises(Crash):
        worker('before').run_once(OWNER)
    with repo._connect() as conn:
        conn.execute('UPDATE runs SET lease_expires_at=0 WHERE run_id=?', (run['run_id'],))
    assert worker('after').run_once(OWNER)['state'] == 'unknown'
    assert calls == ['provider']


def test_checkpoint_requires_current_lease_and_stays_private(tmp_path):
    from hub.infrastructure.platform_db import PlatformRepositoryError
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    run = _queued(app)
    claim = repo.claim_run(OWNER, worker_id='worker', now=10, lease_s=5)
    checkpoint = {'version': 1, 'phase': 'ready', 'step': 1,
                  'transcript': [{'role': 'user', 'content': 'private-checkpoint-input'}],
                  'usage': dict(input_tokens=0, output_tokens=0, total_tokens=0, provider_requests=0)}
    options = dict(lease_id=claim['lease_id'], worker_id='worker', now=11)
    with pytest.raises(PlatformRepositoryError, match='lease_mismatch'):
        repo.save_runtime_checkpoint(OWNER, run['run_id'], checkpoint, **{**options, 'worker_id': 'stale'})
    repo.save_runtime_checkpoint(OWNER, run['run_id'], checkpoint, **options)
    assert repo.load_runtime_checkpoint(OWNER, run['run_id'], **options) == checkpoint
    with pytest.raises(PlatformRepositoryError, match='lease_mismatch'):
        repo.save_runtime_checkpoint(OWNER, run['run_id'], checkpoint, **{**options, 'now': 16})
    assert 'private-checkpoint-input' not in str(repo.get_run(OWNER, run['run_id']))


@pytest.mark.parametrize('kind', ['tool_call', 'final'])
def test_saved_response_does_not_repeat_model_or_charge_usage(tmp_path, monkeypatch, kind):
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    run = _queued(app)
    calls = []

    class Provider:
        def complete(self, messages, tools, **kwargs):
            calls.append(1)
            if kind == 'tool_call' and len(calls) == 1:
                return ModelResponse(kind=kind, tool='workspace.write',
                                     arguments={'path': 'once.txt', 'content': 'once'},
                                     usage={'total_tokens': 9})
            return ModelResponse(kind='final', text='done', usage={'total_tokens': 5})

    def worker(name):
        return LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
                                     worker_id=name, provider_factory=lambda _: Provider())
    original = repo.save_runtime_checkpoint

    def crash(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[2]['phase'] == 'response':
            raise Crash()
        return result

    monkeypatch.setattr(repo, 'save_runtime_checkpoint', crash)
    with pytest.raises(Crash):
        worker('before').run_once(OWNER)
    monkeypatch.setattr(repo, 'save_runtime_checkpoint', original)
    with repo._connect() as conn:
        conn.execute('UPDATE runs SET lease_expires_at=0 WHERE run_id=?', (run['run_id'],))
    result = worker('after').run_once(OWNER)
    assert result['state'] == 'succeeded'
    assert len(calls) == (2 if kind == 'tool_call' else 1)
    assert result['usage']['total_tokens'] == (14 if kind == 'tool_call' else 5)


def test_restored_response_preserves_real_usage_ledger(tmp_path, monkeypatch):
    from hub.infrastructure.usage_repository import UsageRepository
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    run = _queued(app)
    meter = UsageRepository(repo.db_path)
    meter.init()
    calls = []

    class Provider:
        def complete(self, messages, tools, **kwargs):
            calls.append(1)
            return ModelResponse(kind='final', text='billed-once', usage={'total_tokens': 17})

    def worker(name):
        return LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
                                     worker_id=name, provider_factory=lambda _: Provider(), usage_meter=meter)
    original = repo.save_runtime_checkpoint

    def crash(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[2]['phase'] == 'response':
            raise Crash()
        return result

    monkeypatch.setattr(repo, 'save_runtime_checkpoint', crash)
    with pytest.raises(Crash):
        worker('before').run_once(OWNER)
    monkeypatch.setattr(repo, 'save_runtime_checkpoint', original)
    with repo._connect() as conn:
        conn.execute('UPDATE runs SET lease_expires_at=0 WHERE run_id=?', (run['run_id'],))
    assert worker('after').run_once(OWNER)['state'] == 'succeeded'
    assert calls == [1]
    assert meter.get_run_usage(OWNER, run['run_id'])['total_tokens'] == 17
    with repo._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM platform_usage_events WHERE run_id=?', (run['run_id'],)).fetchone()[0] == 1


@pytest.mark.parametrize('phase,expected', [('ready', 'cancelled'), ('complete', 'succeeded')])
def test_recovered_cancel_does_not_erase_persisted_completion(tmp_path, phase, expected):
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    run = _queued(app)
    claim = repo.claim_run(OWNER, worker_id='before', now=1, lease_s=5)
    usage = dict(input_tokens=0, output_tokens=0, total_tokens=0, provider_requests=0)
    checkpoint = {'version': 1, 'phase': phase, 'step': 1, 'transcript': [], 'usage': usage}
    if phase == 'complete':
        checkpoint['result'] = dict(state='succeeded', text='done', steps=1, estimated_tokens=0, usage=usage)
    repo.save_runtime_checkpoint(OWNER, run['run_id'], checkpoint,
                                 lease_id=claim['lease_id'], worker_id='before', now=2)
    with repo._connect() as conn:
        conn.execute("UPDATE runs SET state='cancelling',cancel_requested=1,lease_expires_at=0 WHERE run_id=?", (run['run_id'],))
    worker = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'], worker_id='after')
    assert worker.run_once(OWNER)['state'] == expected


def test_saved_unknown_receipt_wins_recovered_cancel(tmp_path):
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    run = _queued(app)
    claim = repo.claim_run(OWNER, worker_id='before', now=1, lease_s=5)
    checkpoint = {'version': 1, 'phase': 'receipt', 'step': 1, 'transcript': [],
                  'usage': dict(input_tokens=0, output_tokens=0, total_tokens=0, provider_requests=1),
                  'response': dict(kind='tool_call', tool='workspace.write', arguments={'path': 'x', 'content': 'x'}),
                  'receipt': dict(command_id=run['run_id'] + ':step:1', state='unknown', result={}, error_code='receipt_unknown')}
    repo.save_runtime_checkpoint(OWNER, run['run_id'], checkpoint,
                                 lease_id=claim['lease_id'], worker_id='before', now=2)
    with repo._connect() as conn:
        conn.execute("UPDATE runs SET state='cancelling',cancel_requested=1,lease_expires_at=0 WHERE run_id=?", (run['run_id'],))
    worker = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'], worker_id='after')
    assert worker.run_once(OWNER)['state'] == 'unknown'
    assert not (tmp_path / 'workspace' / 'x').exists()


def test_os_process_exit_resumes_persisted_tool_receipt(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    script = r'''
import os, sys
from pathlib import Path
from tests.test_platform_run_worker import _app, _catalog, _queued, OWNER
from hub.application.run_worker_service import LocalRunWorkerService
from hub.platform.providers.base import ModelResponse
root = Path(sys.argv[1])
app = _app(root)
repo = _catalog(app, root)
run = _queued(app)
(root / 'run-id').write_text(run['run_id'])
class Provider:
    def complete(self, *args, **kwargs):
        return ModelResponse(kind='tool_call', tool='workspace.write',
                             arguments={'path':'once.txt','content':'written-once'})
save = repo.save_runtime_checkpoint

def exit_after_receipt(*args, **kwargs):
    save(*args, **kwargs)
    if args[2]['phase'] == 'receipt':
        os._exit(73)
repo.save_runtime_checkpoint = exit_after_receipt
LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
                      worker_id='crashed-process', provider_factory=lambda _: Provider()).run_once(OWNER)
'''
    proc = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                          cwd=Path(__file__).resolve().parents[1],
                          env={**os.environ, 'PYTHONPATH': '.'}, timeout=20, capture_output=True, text=True)
    assert proc.returncode == 73, proc.stderr
    app = _app(tmp_path)
    repo = app.extensions['fleet']['platform_repository']
    run_id = (tmp_path / 'run-id').read_text()
    target = tmp_path / 'workspace' / 'once.txt'
    assert target.read_text() == 'written-once'
    target.write_text('preserved-after-crash')
    with repo._connect() as conn:
        conn.execute('UPDATE runs SET lease_expires_at=0 WHERE run_id=?', (run_id,))
    seen = []

    class Provider:
        def complete(self, messages, tools, **kwargs):
            seen.append(messages)
            return ModelResponse(kind='final', text='resumed')

    worker = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
                                  worker_id='new-process', provider_factory=lambda _: Provider())
    assert worker.run_once(OWNER)['state'] == 'succeeded'
    assert target.read_text() == 'preserved-after-crash'
    assert len(seen) == 1 and seen[0][-1]['role'] == 'tool'


def test_checkpoint_survives_backup_and_conversation_deletion_removes_it(tmp_path):
    from hub.infrastructure.platform_backup import PlatformBackupService
    from hub.infrastructure.platform_db import PlatformRepository
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    run = _queued(app)
    claim = repo.claim_run(OWNER, worker_id='worker', now=1, lease_s=10)
    checkpoint = {'version': 1, 'phase': 'ready', 'step': 1, 'transcript': [],
                  'usage': dict(input_tokens=0, output_tokens=0, total_tokens=0, provider_requests=0)}
    repo.save_runtime_checkpoint(OWNER, run['run_id'], checkpoint,
                                 lease_id=claim['lease_id'], worker_id='worker', now=2)
    artifacts = tmp_path / 'empty-artifacts'
    artifacts.mkdir()
    service = PlatformBackupService()
    service.create_backup(repo.db_path, artifacts, tmp_path / 'backup')
    service.restore_backup(tmp_path / 'backup', tmp_path / 'restored')
    restored = PlatformRepository(tmp_path / 'restored' / 'platform.db')
    restored.init()
    import json
    with restored._connect() as conn:
        saved = conn.execute('SELECT runtime_checkpoint FROM runs WHERE run_id=?', (run['run_id'],)).fetchone()[0]
    assert json.loads(saved) == checkpoint
    assert restored.get_run(OWNER, run['run_id'])['state'] == 'unknown'
    assert restored.claim_run(OWNER, worker_id='restore-worker', now=100) is None
    repo.finish_run(OWNER, run['run_id'], lease_id=claim['lease_id'], worker_id='worker',
                    state='cancelled', now=3)
    repo.set_conversation_archived(OWNER, run['conversation_id'], archived=True, now=4)
    repo.delete_archived_conversation(OWNER, run['conversation_id'])
    with repo._connect() as conn:
        assert conn.execute('SELECT runtime_checkpoint FROM runs WHERE run_id=?', (run['run_id'],)).fetchone() is None


@pytest.mark.parametrize('raw', ['{', '{"version":true}', '{"version":1,"phase":"complete","step":1,"transcript":[],"usage":{}}'])
def test_corrupt_checkpoint_never_replays_started_run(tmp_path, raw):
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    run = _queued(app)
    claim = repo.claim_run(OWNER, worker_id='before', now=1, lease_s=5)
    repo.append_run_event(OWNER, run['run_id'], 'run_started', {}, now=2,
                          lease_id=claim['lease_id'], worker_id='before')
    with repo._connect() as conn:
        conn.execute('UPDATE runs SET runtime_checkpoint=? WHERE run_id=?', (raw, run['run_id']))
    assert repo.claim_run(OWNER, worker_id='after', now=10)['state'] == 'unknown'


def test_saved_completion_does_not_reopen_provider_or_workspace(tmp_path):
    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    run = _queued(app)
    claim = repo.claim_run(OWNER, worker_id='before', now=1, lease_s=5)
    usage = dict(input_tokens=1, output_tokens=2, total_tokens=3, provider_requests=1)
    checkpoint = {'version': 1, 'phase': 'complete', 'step': 1, 'transcript': [], 'usage': usage,
                  'result': dict(state='succeeded', text='saved', steps=1, estimated_tokens=3, usage=usage)}
    repo.save_runtime_checkpoint(OWNER, run['run_id'], checkpoint,
                                 lease_id=claim['lease_id'], worker_id='before', now=2)
    repo.upsert_workspace(OWNER, {'workspace_id': 'home', 'backend': 'directory',
                                 'root_path': str(tmp_path / 'workspace'), 'enabled': False})
    with repo._connect() as conn:
        conn.execute('UPDATE runs SET lease_expires_at=0 WHERE run_id=?', (run['run_id'],))
    def unavailable(_):
        raise AssertionError('Provider must not be reopened for a completed checkpoint')
    worker = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
                                  worker_id='after', provider_factory=unavailable)
    result = worker.run_once(OWNER)
    assert result['state'] == 'succeeded' and result['result_text'] == 'saved'
    events = repo.list_run_events(OWNER, run['run_id'])['events']
    assert sum(event['kind'] == 'run_finished' for event in events) == 1
