"""A recovery index is not proof that native session identity was persisted."""
import json
import sqlite3

import pytest

from tests.test_review_node_sync import _cfg, _client
from tests.test_agy_native_history import _native, _post
from tools.session.runner_capture import RunnerCapture, session_prefix


def _partial(cfg, family, attempt='native_partial'):
    root = cfg.cache_dir / 'conversations'
    root.mkdir(parents=True, exist_ok=True)
    record = root / (session_prefix(cfg.machine) + attempt + '.json')
    record.write_text(json.dumps({'attempt_id': attempt, 'agent_type': family,
                                 'task_id': ''}), encoding='utf-8')
    return record


@pytest.mark.parametrize('family', ['agy', 'hermes', 'codex', 'claude_code', 'pi', 'zcode'])
def test_partial_native_initialization_recovers_family_without_replaying_lifecycle(tmp_path, family):
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    _partial(cfg, family)
    for unused in range(2):
        capture = RunnerCapture(cfg, {'attempt_id': 'native_partial', 'agent_type': family,
                                     'task_id': ''}, _post(client), replay=True)
        try:
            capture.flush()
        finally:
            capture.bridge.close()
    response = client.get('/api/sessions/' + capture.session_id)
    assert response.status_code == 200
    assert response.get_json()['session']['agent_family'] == family
    events = client.get('/api/sessions/' + capture.session_id + '/events?include_source=0').get_json()['events']
    assert len(events) == 1
    assert events[0]['kind'] == 'session_metadata'


def test_empty_agy_without_title_recovers_after_partial_initialization(tmp_path):
    from tools.session.agy_archive import sync_agy
    from tools.session.sync import native_identity
    path = _native(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM steps')
    with sqlite3.connect(path.parent.parent / 'conversation_summaries.db') as db:
        db.execute('DELETE FROM conversation_summaries')
    source = path.read_bytes()
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    _partial(cfg, 'agy', native_identity('agy', path))
    assert sync_agy(cfg, path, _post(client))
    rows = client.get('/api/sessions').get_json()['sessions']
    assert len(rows) == 1 and rows[0]['agent_family'] == 'agy'
    assert path.read_bytes() == source


def test_identity_checkpoint_failure_retries_same_public_identity_and_recovers_ack(tmp_path, monkeypatch):
    from tools import runner_source
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    record = _partial(cfg, 'agy')
    real = runner_source.atomic_private_json
    blocked = [True]

    def fail(path, value):
        if path == record and blocked[0]:
            raise OSError('fixture disk failure')
        return real(path, value)

    monkeypatch.setattr(runner_source, 'atomic_private_json', fail)
    task = json.loads(record.read_text(encoding='utf-8'))
    capture = RunnerCapture(cfg, task, _post(client), replay=True, preserve_source=False)
    try:
        with pytest.raises(OSError):
            capture.flush()
    finally:
        capture.bridge.close()
    assert 'native_identity_version' not in json.loads(record.read_text(encoding='utf-8'))
    blocked[0] = False
    recovered = RunnerCapture(cfg, task, _post(client), replay=True, preserve_source=False)
    try:
        recovered.flush()
        state = recovered.bridge.spool.status()
        assert state['ack_sequence'] == state['last_sequence']
        events = client.get('/api/sessions/' + recovered.session_id + '/events?include_source=0').get_json()['events']
        assert len(events) == 1 and events[0]['payload']['agent_family'] == 'agy'
    finally:
        recovered.bridge.close()


def test_rejected_identity_never_marks_record_initialized(tmp_path, monkeypatch):
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    record = _partial(cfg, 'agy')
    task = json.loads(record.read_text(encoding='utf-8'))
    capture = RunnerCapture(cfg, task, _post(client), replay=True, preserve_source=False)
    real = capture.bridge.ingest_one
    monkeypatch.setattr(capture.bridge, 'ingest_one', lambda *a, **kw: False)
    try:
        with pytest.raises(RuntimeError, match='native_identity_not_persisted'):
            capture.flush()
        assert 'native_identity_version' not in json.loads(record.read_text(encoding='utf-8'))
        monkeypatch.setattr(capture.bridge, 'ingest_one', real)
        capture.flush()
        assert json.loads(record.read_text(encoding='utf-8'))['native_identity_version'] == 1
    finally:
        capture.bridge.close()
