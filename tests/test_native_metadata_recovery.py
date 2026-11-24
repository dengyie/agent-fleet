"""Source-backed metadata recovery must not replay native conversation content."""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from tests.test_review_node_sync import _cfg, _client
from tools.session.normalization import normalize_native_row
from tools.session.runner_capture import RunnerCapture, replay_pending, session_prefix
from tools.session.sync import native_identity, sync_file
from tools.supervisor.continuation import ContinuationStore
from tools.supervisor.model import ManagedSessionManifest


def _send(client):
    def send(cfg, path, body):
        response = client.post(path, json=body,
            headers={'X-Runner-Credential': 'pxed:secret'})
        return response.status_code, response.get_json()
    return send


def _codex_rows(cwd, token='native_fixture'):
    return [
        {'type': 'session_meta', 'payload': {'id': token, 'cwd': str(cwd),
            'cli_version': '0.167.0', 'model_provider': 'observed-provider',
            'timestamp': '2026-11-21T06:26:09.229Z'}},
        {'type': 'turn_context', 'payload': {'model': 'observed-model'}},
        {'type': 'custom-title', 'customTitle': 'Original VM title'},
        {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'old question'}},
        {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'old answer'}},
    ]


def _write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows),
                    encoding='utf-8')


def _retain(cfg, family, path, cwd, *, token='native_fixture', attempt='metadata_fixture', home=None):
    cfg.supervisor_manifest_dir = cfg.cache_dir / 'manifests'
    sid = session_prefix(cfg.machine) + attempt
    manifest = ManagedSessionManifest(session_id=sid, machine_id=cfg.machine,
        process_group_id='group_fixture', agent=family, attempt_id=attempt,
        state='terminated', reason='completed', capability_manifest={'resume': True})
    cfg.supervisor_manifest_dir.mkdir(parents=True, exist_ok=True)
    (cfg.supervisor_manifest_dir / (sid + '.json')).write_text(manifest.to_json(), encoding='utf-8')
    entry = SimpleNamespace(manifest=manifest, cwd=str(cwd), exe_path='unused-agent',
        resume_token=token, env={'HOME': str(home or path.parent)}, resume_options=[],
        native_cursor=0, model_config=None, restore_model=None,
        native_file_path=str(path))
    assert ContinuationStore(cfg.supervisor_manifest_dir).save(entry)
    return sid


def _old_capture(cfg, family, send, *, attempt='metadata_fixture'):
    capture = RunnerCapture(cfg, {'attempt_id': attempt, 'agent_type': family,
        'task_id': ''}, send, process_group_id='group_fixture')
    capture.line(json.dumps({'kind': 'user_message', 'payload': {'text': 'old question'}}), strict=True)
    capture.line(json.dumps({'kind': 'assistant_message', 'payload': {'text': 'old answer'}}), strict=True)
    capture.finish(0, resumable=True)
    return capture


def _events(client, sid):
    response = client.get('/api/sessions/' + sid + '/events?include_source=0&limit=500')
    assert response.status_code == 200
    return response.get_json()['events']


def _mark_takeover_recovered(capture):
    data = json.loads(capture.record.read_text(encoding='utf-8'))
    data['model_takeover_version'] = 1
    capture.record.write_text(json.dumps(data), encoding='utf-8')


def _messages(client, sid):
    return [row for row in _events(client, sid)
            if row['kind'] in {'user_message', 'assistant_message'}]


@pytest.mark.parametrize('kind', ['user', 'assistant'])
def test_claude_native_rows_keep_cli_version(kind):
    row = {'type': kind, 'sessionId': 'native_fixture', 'version': '2.1.319',
           'message': {'model': 'observed-model', 'content': 'visible text'}}
    metadata = next(payload for event, payload in normalize_native_row(row)
                    if event == 'session_metadata')
    assert metadata['client_version'] == '2.1.319'
    assert metadata['model'] == 'observed-model'


def test_pi_numeric_storage_version_is_not_a_cli_version():
    events = normalize_native_row({'type': 'session', 'version': 3,
        'timestamp': '2026-11-21T08:41:04.944Z'})
    assert events == [('session_metadata', {'native_started_at': '2026-11-21T08:41:04.944Z'})]


@pytest.mark.parametrize('family', ['zcode', 'hermes'])
def test_exact_native_database_metadata_is_observed_and_read_only(tmp_path, family):
    from tools.session.native_storage import read_metadata
    path = tmp_path / 'native.db'
    table, directory = ('session', 'directory') if family == 'zcode' else ('sessions', 'cwd')
    with sqlite3.connect(path) as db:
        if family == 'zcode':
            db.execute(f'CREATE TABLE {table}(id TEXT PRIMARY KEY,{directory} TEXT,title TEXT,version TEXT,time_created INTEGER,secret TEXT)')
            db.execute(f'INSERT INTO {table} VALUES(?,?,?,?,?,?)',
                ('native_fixture', str(tmp_path), 'Original VM title', '0.17.1', 1795251727815, 'PRIVATE_SENTINEL'))
        else:
            db.execute(f'CREATE TABLE {table}(id TEXT PRIMARY KEY,{directory} TEXT,title TEXT,model TEXT,started_at REAL,system_prompt TEXT)')
            db.execute(f'INSERT INTO {table} VALUES(?,?,?,?,?,?)',
                ('native_fixture', str(tmp_path), 'Original VM title', 'observed-model', 1795251471.4830949, 'PRIVATE_SENTINEL'))
    before = path.read_bytes()
    metadata = read_metadata(family, path, 'native_fixture', cwd=str(tmp_path))
    assert metadata['title'] == 'Original VM title'
    if family == 'zcode':
        assert metadata['client_version'] == '0.17.1'
        assert metadata['native_started_at'] == '2026-11-21T09:02:07.815Z'
    else:
        assert metadata['model'] == 'observed-model'
        assert metadata['native_started_at'] == '2026-11-21T08:57:51.483Z'
    assert 'PRIVATE_SENTINEL' not in json.dumps(metadata)
    assert str(tmp_path) not in json.dumps(metadata)
    assert read_metadata(family, path, 'another', cwd=str(tmp_path)) == {}
    assert read_metadata(family, path, 'native_fixture', cwd=str(tmp_path / 'wrong')) == {}
    assert path.read_bytes() == before


@pytest.mark.parametrize('family', ['zcode', 'hermes'])
def test_retained_native_database_metadata_recovers_on_original_session(tmp_path, family):
    from tools.session.native_storage import database_path
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    send = _send(client)
    home = tmp_path / 'home'
    cwd = home / 'workspace'
    cwd.mkdir(parents=True)
    native_db = database_path(family, {'HOME': str(home)})
    native_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(native_db) as db:
        if family == 'zcode':
            db.execute('CREATE TABLE session(id TEXT PRIMARY KEY,directory TEXT,title TEXT,version TEXT,time_created INTEGER)')
            db.execute('INSERT INTO session VALUES(?,?,?,?,?)', ('native_fixture', str(cwd),
                'Original VM title', '0.17.1', 1795251727815))
        else:
            db.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY,cwd TEXT,title TEXT,model TEXT,started_at REAL)')
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', ('native_fixture', str(cwd),
                'Original VM title', 'observed-model', 1795251471.4830949))
    before = native_db.read_bytes()
    capture = _old_capture(cfg, family, send)
    _retain(cfg, family, native_db, cwd, home=home)
    messages = _messages(client, capture.session_id)
    replay_pending(cfg, send)
    dto = client.get('/api/sessions/' + capture.session_id).get_json()['session']
    assert dto['native_title'] == 'Original VM title'
    if family == 'zcode':
        assert dto['client_version'] == '0.17.1'
        assert dto['native_started_at'] == '2026-11-21T09:02:07.815Z'
    else:
        assert dto['model'] == 'observed-model'
        assert dto['native_started_at'] == '2026-11-21T08:57:51.483Z'
    assert _messages(client, capture.session_id) == messages
    assert native_db.read_bytes() == before


def test_retained_idle_capture_recovers_metadata_without_message_replay(tmp_path):
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    send = _send(client)
    cwd = tmp_path / 'workspace'
    cwd.mkdir()
    native = tmp_path / 'rollout-native_fixture.jsonl'
    _write_rows(native, _codex_rows(cwd))
    capture = _old_capture(cfg, 'codex', send)
    sid = _retain(cfg, 'codex', native, cwd)
    original = _messages(client, sid)
    source = native.read_bytes()
    # Simulate an existing index created before metadata reconciliation.
    data = json.loads(capture.record.read_text(encoding='utf-8'))
    data.pop('native_metadata', None)
    capture.record.write_text(json.dumps(data), encoding='utf-8')
    replay_pending(cfg, send)
    dto = client.get('/api/sessions/' + sid).get_json()['session']
    assert dto['native_title'] == 'Original VM title'
    assert dto['client_version'] == '0.167.0'
    assert dto['model'] == 'observed-model'
    assert dto['provider'] == 'observed-provider'
    assert _messages(client, sid) == original
    assert native.read_bytes() == source
    before = _events(client, sid)
    replay_pending(cfg, send)
    assert _events(client, sid) == before


def test_archive_at_old_eof_recovers_metadata_without_rewinding_cursor(tmp_path):
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    send = _send(client)
    native = tmp_path / 'archive.jsonl'
    _write_rows(native, _codex_rows(tmp_path))
    attempt = native_identity('codex', native)
    old = RunnerCapture(cfg, {'attempt_id': attempt, 'agent_type': 'codex', 'task_id': ''}, send)
    old.line(json.dumps({'kind': 'user_message', 'payload': {'text': 'old question'}}), strict=True)
    old.flush()
    old.bridge.close()
    checkpoint = old.root / (old.session_id + '.cursor')
    checkpoint.write_text(json.dumps({'offset': native.stat().st_size}), encoding='utf-8')
    original, source_cursor = _messages(client, old.session_id), checkpoint.read_bytes()
    assert sync_file(cfg, 'codex', native, send) == 0
    dto = client.get('/api/sessions/' + old.session_id).get_json()['session']
    assert dto['native_title'] == 'Original VM title'
    assert dto['model'] == 'observed-model'
    assert checkpoint.read_bytes() == source_cursor
    assert _messages(client, old.session_id) == original
    before = _events(client, old.session_id)
    assert sync_file(cfg, 'codex', native, send) == 0
    assert _events(client, old.session_id) == before


def test_historical_metadata_fills_missing_without_replacing_newer_facts(tmp_path):
    from hub.infrastructure.session_repository import SessionRepository
    repo = SessionRepository(tmp_path / 'sessions.db')
    repo.init()
    repo.upsert_session({'session_id': 'fixture', 'machine_id': 'pxed'})
    repo.update_session_metadata('fixture', 1, {'model': 'current-model', 'title': 'Current name'})
    repo.update_session_metadata('fixture', 2, {'model': 'old-model', 'title': 'Old name',
        'client_version': '0.167.0', 'fill_missing': True})
    stored = repo.get_session('fixture')
    assert stored['model'] == 'current-model'
    assert stored['native_title'] == 'Current name'
    assert stored['client_version'] == '0.167.0'
    repo.update_session_metadata('fixture', 3, {'model': 'next-model'})
    assert repo.get_session('fixture')['model'] == 'next-model'


def test_metadata_only_replay_preserves_capture_quality_and_control_binding(tmp_path):
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    send = _send(client)
    capture = _old_capture(cfg, 'codex', send)
    cwd = tmp_path / 'workspace'
    cwd.mkdir()
    native = tmp_path / 'rollout-native_fixture.jsonl'
    _write_rows(native, _codex_rows(cwd))
    _retain(cfg, 'codex', native, cwd)
    before = client.get('/api/sessions/' + capture.session_id).get_json()['session']
    assert before['capture_quality'] == 'structured'
    replay_pending(cfg, send)
    after = client.get('/api/sessions/' + capture.session_id).get_json()['session']
    assert after['capture_quality'] == before['capture_quality']
    assert after['managed'] == before['managed']
    assert after['process_group_id'] == before['process_group_id']
    assert after['allowed_actions'] == before['allowed_actions']


def test_metadata_http_response_loss_replays_same_event_identity_once(tmp_path):
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    cwd = tmp_path / 'workspace'
    cwd.mkdir()
    native = tmp_path / 'rollout-native_fixture.jsonl'
    _write_rows(native, _codex_rows(cwd))
    capture = _old_capture(cfg, 'codex', _send(client))
    _retain(cfg, 'codex', native, cwd)
    attempts = []

    def lose_first_response(cfg, path, events):
        attempts.append([row['event_id'] for row in events])
        if len(attempts) == 1:
            # Simulate Hub commit followed by a lost HTTP response. The local
            # encrypted spool must keep its original event ids/sequence.
            response = client.post(path, json=events,
                headers={'X-Runner-Credential': 'pxed:secret'})
            assert response.status_code == 200
            return 503, {'error': {'code': 'response_lost'}}
        response = client.post(path, json=events,
            headers={'X-Runner-Credential': 'pxed:secret'})
        return response.status_code, response.get_json()

    replay_pending(cfg, lose_first_response)
    replay_pending(cfg, lose_first_response)
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    events = _events(client, capture.session_id)
    metadata = [row for row in events if row['kind'] == 'session_metadata'
                and row['payload'].get('client_version')]
    assert len(metadata) == 1
    assert metadata[0]['payload']['client_version'] == '0.167.0'


@pytest.mark.parametrize('bad_state', [
    {'version': 1, 'fingerprint': 'wrong', 'digest': '0' * 64},
    {'version': 99, 'fingerprint': '0' * 64, 'digest': '0' * 64},
])
def test_corrupt_or_future_metadata_checkpoint_fails_closed(tmp_path, bad_state):
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    send = _send(client)
    cwd = tmp_path / 'workspace'
    cwd.mkdir()
    native = tmp_path / 'rollout-native_fixture.jsonl'
    _write_rows(native, _codex_rows(cwd))
    capture = _old_capture(cfg, 'codex', send)
    _retain(cfg, 'codex', native, cwd)
    data = json.loads(capture.record.read_text(encoding='utf-8'))
    data['model_takeover_version'] = 1
    data['native_metadata'] = bad_state
    capture.record.write_text(json.dumps(data), encoding='utf-8')
    before = _events(client, capture.session_id)
    replay_pending(cfg, send)
    after = _events(client, capture.session_id)
    assert not any(row['kind'] == 'session_metadata'
                   and row['payload'].get('client_version') == '0.167.0' for row in after)
    assert _messages(client, capture.session_id) == [row for row in before
        if row['kind'] in {'user_message', 'assistant_message'}]


def test_checkpoint_write_failure_keeps_metadata_event_recoverable(tmp_path, monkeypatch):
    import tools.runner_source as source_cache
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    send = _send(client)
    cwd = tmp_path / 'workspace'
    cwd.mkdir()
    native = tmp_path / 'rollout-native_fixture.jsonl'
    _write_rows(native, _codex_rows(cwd))
    capture = _old_capture(cfg, 'codex', send)
    _retain(cfg, 'codex', native, cwd)
    _mark_takeover_recovered(capture)
    record = capture.record.read_bytes()
    original = source_cache.atomic_private_json
    failed = [False]

    def fail_record_once(path, value):
        if path == capture.record and not failed[0]:
            failed[0] = True
            raise OSError('synthetic private checkpoint failure')
        return original(path, value)

    monkeypatch.setattr(source_cache, 'atomic_private_json', fail_record_once)
    replay_pending(cfg, send)
    assert failed[0]
    assert capture.record.read_bytes() == record
    metadata_before = [row for row in _events(client, capture.session_id)
                       if row['kind'] == 'session_metadata']
    assert metadata_before
    monkeypatch.setattr(source_cache, 'atomic_private_json', original)
    replay_pending(cfg, send)
    metadata_after = [row for row in _events(client, capture.session_id)
                      if row['kind'] == 'session_metadata']
    stable_ids = [row['event_id'] for row in metadata_after
                  if row['payload'].get('client_version') == '0.167.0']
    # The checkpoint and encrypted spool are separate files. A checkpoint
    # failure may re-observe metadata with a new Hub event; it must not reuse
    # an already compacted sequence or repeat conversation content.
    assert len(stable_ids) == 2
    assert len(set(stable_ids)) == 2
    recovered = RunnerCapture(cfg, json.loads(capture.record.read_text(encoding='utf-8')),
                              send, replay=True)
    try:
        status = recovered.bridge.spool.status()
        assert status['ack_sequence'] == status['next_sequence'] - 1
    finally:
        recovered.bridge.close()


@pytest.mark.parametrize('identity', [
    {'token': 'another_session', 'cwd': 'same'},
    {'token': 'native_fixture', 'cwd': 'wrong'},
])
def test_jsonl_metadata_requires_exact_session_and_workspace(identity, tmp_path):
    from tools.session.native_metadata import read_jsonl_metadata
    cwd = tmp_path / 'workspace'
    cwd.mkdir()
    native = tmp_path / 'rollout-native_fixture.jsonl'
    _write_rows(native, _codex_rows(cwd))
    expected_cwd = cwd if identity['cwd'] == 'same' else tmp_path / 'other'
    if identity['cwd'] == 'wrong':
        expected_cwd.mkdir()
    with pytest.raises(ValueError, match='native_metadata_unbound'):
        read_jsonl_metadata('codex', native, token=identity['token'], cwd=str(expected_cwd))


def test_old_native_database_without_optional_metadata_columns_is_supported(tmp_path):
    from tools.session.native_storage import read_metadata
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE session(id TEXT PRIMARY KEY,directory TEXT)')
        db.execute('INSERT INTO session VALUES(?,?)', ('native_fixture', str(tmp_path)))
    assert read_metadata('zcode', path, 'native_fixture', cwd=str(tmp_path)) == {}


def test_large_jsonl_uses_bounded_head_and_tail_and_never_decodes_hidden_fields(tmp_path):
    from tools.session import native_metadata
    cwd = tmp_path / 'workspace'
    cwd.mkdir()
    native = tmp_path / 'rollout-native_fixture.jsonl'
    rows = _codex_rows(cwd)
    rows.extend({'type': 'event_msg', 'payload': {'type': 'agent_message',
        'message': 'x' * 1000, 'private': 'HIDDEN_SENTINEL'}} for _ in range(2048))
    _write_rows(native, rows)
    original_head, original_tail = native_metadata.HEAD_BYTES, native_metadata.TAIL_BYTES
    try:
        native_metadata.HEAD_BYTES = 4096
        native_metadata.TAIL_BYTES = 4096
        fingerprint, payloads = native_metadata.read_jsonl_metadata('codex', native)
    finally:
        native_metadata.HEAD_BYTES, native_metadata.TAIL_BYTES = original_head, original_tail
    assert len(fingerprint) == 64
    assert any(row.get('title') == 'Original VM title' for row in payloads)
    assert 'HIDDEN_SENTINEL' not in json.dumps(payloads)


def test_zcode_archive_uses_exact_sibling_store_for_native_name_and_version(tmp_path):
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    send = _send(client)
    native = tmp_path / 'cli/rollout/model-io-native_fixture.jsonl'
    _write_rows(native, [{'type': 'model_io', 'sessionId': 'native_fixture',
        'request': {'model': 'observed-model', 'messages': []},
        'response': {'text': 'existing answer'}}])
    database = tmp_path / 'cli/db/db.sqlite'
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE session(id TEXT PRIMARY KEY,directory TEXT,title TEXT,version TEXT)')
        db.execute('INSERT INTO session VALUES(?,?,?,?)',
                   ('native_fixture', str(tmp_path), 'VM archive title', '0.17.1'))
        db.execute('INSERT INTO session VALUES(?,?,?,?)',
                   ('different', str(tmp_path), 'DO_NOT_BORROW', 'unrelated'))
    sync_file(cfg, 'zcode', native, send)
    sid = session_prefix(cfg.machine) + native_identity('zcode', native)
    dto = client.get('/api/sessions/' + sid).get_json()['session']
    assert dto['native_title'] == 'VM archive title'
    assert dto['client_version'] == '0.17.1'
    assert dto['model'] == 'observed-model'


def test_recovery_does_not_compete_with_a_live_capture_writer(tmp_path):
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    capture = RunnerCapture(cfg, {'attempt_id': 'metadata_fixture', 'agent_type': 'codex',
        'task_id': ''}, _send(client), process_group_id='group_fixture')
    try:
        capture.flush()
        before = capture.bridge.spool.status()
        replay_pending(cfg, _send(client))
        assert capture.bridge.spool.status() == before
    finally:
        capture.bridge.close()


def test_metadata_updates_can_return_to_a_previous_value_without_stalling_sequence(tmp_path):
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    send = _send(client)
    native = tmp_path / 'source.jsonl'
    rows = _codex_rows(tmp_path)
    _write_rows(native, rows)
    sync_file(cfg, 'codex', native, send)
    sid = session_prefix(cfg.machine) + native_identity('codex', native)
    for title in ('Second VM title', 'Original VM title'):
        with native.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'type': 'custom-title', 'customTitle': title}) + '\n')
        sync_file(cfg, 'codex', native, send)
        dto = client.get('/api/sessions/' + sid).get_json()['session']
        assert dto['native_title'] == title
    capture = RunnerCapture(cfg, {'attempt_id': native_identity('codex', native),
        'agent_type': 'codex', 'task_id': ''}, send, replay=True)
    try:
        status = capture.bridge.spool.status()
        assert status['ack_sequence'] == status['next_sequence'] - 1
    finally:
        capture.bridge.close()


def test_unsafe_link_and_malformed_rows_never_become_metadata(tmp_path):
    import os
    from tools.session.native_metadata import read_jsonl_metadata
    path = tmp_path / 'native.jsonl'
    path.write_bytes(b'not-json\n' + b'{"type":"session_meta","payload":{"cli_version":"safe"}}\n'
                     + b'{"type":"session_meta","payload":{"cli_version":"partial"}}')
    _, payloads = read_jsonl_metadata('codex', path)
    assert payloads == [{'client_version': 'safe'}]
    link = tmp_path / 'linked.jsonl'
    try:
        os.symlink(path, link)
    except OSError:
        pytest.skip('Platform does not permit creating a test symlink')
    with pytest.raises(ValueError, match='native_metadata_unsafe'):
        read_jsonl_metadata('codex', link)


def test_fill_missing_metadata_flag_requires_boolean_and_is_not_a_capability():
    from session_schema import validate_event
    from tests.test_session_interactions import event
    row = event(1, 'session_metadata', {'title': 'native', 'fill_missing': True})
    assert validate_event(row)['payload'] == {'title': 'native', 'fill_missing': True}
    for invalid in (1, 'true', None, {}, []):
        with pytest.raises(ValueError):
            validate_event(event(1, 'session_metadata', {'fill_missing': invalid}))


def test_retained_metadata_wrong_family_or_machine_binding_is_rejected(tmp_path):
    from tools.session.native_metadata import read_retained_metadata
    cfg = _cfg(tmp_path)
    native = tmp_path / 'rollout-native_fixture.jsonl'
    _write_rows(native, _codex_rows(tmp_path))
    sid = _retain(cfg, 'codex', native, tmp_path)
    assert read_retained_metadata(cfg, sid, 'claude_code') is None
    cfg.machine = 'other-machine'
    assert read_retained_metadata(cfg, sid, 'codex') is None


def test_ambiguous_retained_native_paths_do_not_choose_a_newest_session(tmp_path):
    from tools.session.native_metadata import read_retained_metadata
    cfg = _cfg(tmp_path)
    home = tmp_path / 'home'
    cwd = home / 'workspace'
    cwd.mkdir(parents=True)
    path = home / '.codex/sessions/2026/11/21/rollout-2026-11-21T06-26-09-native_fixture.jsonl'
    _write_rows(path, _codex_rows(cwd))
    sid = _retain(cfg, 'codex', path, cwd, home=home)
    binding = ContinuationStore(cfg.supervisor_manifest_dir).path(sid)
    data = json.loads(binding.read_text(encoding='utf-8'))
    data['native_file_path'] = None
    binding.write_text(json.dumps(data), encoding='utf-8')
    assert read_retained_metadata(cfg, sid, 'codex')[1]
    duplicate = home / '.codex/archived_sessions/rollout-2026-11-21T06-26-09-native_fixture.jsonl'
    _write_rows(duplicate, _codex_rows(cwd))
    assert read_retained_metadata(cfg, sid, 'codex') is None


def test_codex_metadata_uses_exact_known_thread_store_binding(tmp_path):
    from tools.session.native_metadata import read_retained_metadata
    cfg = _cfg(tmp_path)
    home = tmp_path / 'home'
    cwd = home / 'workspace'
    cwd.mkdir(parents=True)
    path = home / '.codex/sessions/2026/11/21/rollout-2026-native_fixture.jsonl'
    _write_rows(path, _codex_rows(cwd))
    sid = _retain(cfg, 'codex', path, cwd, home=home)
    database = home / '.codex/state_5.sqlite'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,cwd TEXT,title TEXT,name TEXT,model TEXT,model_provider TEXT,cli_version TEXT,created_at_ms INTEGER)')
        db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?)', ('native_fixture', str(path),
            str(cwd), 'Default native title', 'VM renamed thread', 'current-model', 'custom-provider',
            '0.167.0', 1795242369229))
    metadata = read_retained_metadata(cfg, sid, 'codex')[1]
    assert metadata[-1]['title'] == 'VM renamed thread'
    assert metadata[-1]['model'] == 'current-model'
    assert metadata[-1]['provider'] == 'custom-provider'
    with sqlite3.connect(database) as db:
        db.execute('UPDATE threads SET rollout_path=?', (str(tmp_path / 'another.jsonl'),))
    metadata = read_retained_metadata(cfg, sid, 'codex')[1]
    assert all(row.get('title') != 'VM renamed thread' for row in metadata)


def test_first_archive_import_finishes_with_current_native_store_name(tmp_path):
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    native = tmp_path / '.codex/sessions/2026/11/21/rollout-2026-native_fixture.jsonl'
    _write_rows(native, _codex_rows(tmp_path))
    with sqlite3.connect(tmp_path / '.codex/state_5.sqlite') as db:
        db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,cwd TEXT,title TEXT,model TEXT)')
        db.execute('INSERT INTO threads VALUES(?,?,?,?,?)', ('native_fixture', str(native),
                   str(tmp_path), 'Current VM title', 'current-model'))
    sync_file(cfg, 'codex', native, _send(client))
    sid = session_prefix(cfg.machine) + native_identity('codex', native)
    dto = client.get('/api/sessions/' + sid).get_json()['session']
    assert dto['native_title'] == 'Current VM title'
    assert dto['model'] == 'current-model'
    with native.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'type': 'custom-title', 'customTitle': 'Old log title'}) + '\n')
    sync_file(cfg, 'codex', native, _send(client))
    dto = client.get('/api/sessions/' + sid).get_json()['session']
    assert dto['native_title'] == 'Current VM title'
