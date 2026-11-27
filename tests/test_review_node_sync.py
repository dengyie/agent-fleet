"""Review fix #7: native sync must not resend the accepted prefix after a
mid-batch spool rejection (quota boundary).  The hub dedups by sequence only,
so a re-read prefix would be re-spooled with fresh sequences and duplicated.
"""
import json

import pytest

from tools.session import runner_capture
from tools.session.capture.native_jsonl import tailer


def _client(tmp_path):
    from hub.bootstrap import create_app
    from hub.config import FleetConfig
    return create_app(FleetConfig.from_root(
        tmp_path / 'hub', dev_operator='test@local',
        session_repositories_enabled=True,
        runner_credentials={'pxed': 'secret'})).test_client()


def _cfg(tmp_path):
    from tools.runner_config import RunnerConfig
    return RunnerConfig(hub='http://localhost', machine='pxed', credential='secret',
                        runner_id='pxed', projects={}, agents={},
                        cache_dir=tmp_path / 'cache')


def _line(text, kind='user_message'):
    return json.dumps({'type': 'event_msg', 'payload': {'type': kind, 'message': text}}) + '\n'


def test_quota_rejection_mid_batch_resumes_without_duplicates(tmp_path, monkeypatch):
    from tools.session.sync import sync_file
    client = _client(tmp_path)

    def send(cfg, path, body):
        response = client.post(path, json=body, headers={'X-Runner-Credential': 'pxed:secret'})
        return response.status_code, response.get_json()

    native = tmp_path / 'native.jsonl'
    native.write_text(''.join(_line(f'm{i}') for i in range(1, 5)))

    real_line = runner_capture.RunnerCapture.line
    state = {'blocked': True}

    def quota_line(self, line, **kwargs):
        if state['blocked'] and '"m3"' in line:
            raise RuntimeError('source_record_not_persisted')
        return real_line(self, line, **kwargs)

    monkeypatch.setattr(runner_capture.RunnerCapture, 'line', quota_line)
    cfg = _cfg(tmp_path)
    with pytest.raises(RuntimeError):
        sync_file(cfg, 'codex', native, send)
    state['blocked'] = False
    assert sync_file(cfg, 'codex', native, send) == 2   # 只续传 m3、m4
    assert sync_file(cfg, 'codex', native, send) == 0

    sid = client.get('/api/sessions').get_json()['sessions'][0]['session_id']
    events = client.get('/api/sessions/' + sid + '/events').get_json()['events']
    messages = [e['payload']['text'] for e in events if e['kind'] == 'user_message']
    assert messages == ['m1', 'm2', 'm3', 'm4']


def test_read_entries_reports_per_row_end_offsets(tmp_path):
    path = tmp_path / 'x.jsonl'
    a, bad, b = _line('a'), 'not json\n', _line('b')
    path.write_bytes((a + bad + b + '{"partial":').encode('utf-8'))
    entries = tailer(str(path)).read_entries()
    assert [end for _obj, end in entries] == [len(a), len(a) + len(bad) + len(b)]
    reader = tailer(str(path))
    assert len(reader.read()) == 2 and reader.offset == len(a + bad + b)


def test_archive_scan_yields_and_continues_after_slow_file(tmp_path, monkeypatch):
    from tools.session import sync
    root = tmp_path / 'native'
    root.mkdir()
    for name in ('a', 'b', 'c'):
        (root / (name + '.jsonl')).write_text(_line(name))
    now, visited = [0.0], []
    monkeypatch.setattr(sync.time, 'monotonic', lambda: now[0])
    def slow_file(cfg, family, path, send, **kwargs):
        visited.append(path.name)
        now[0] += 20
        raise RuntimeError('upload_pending')
    monkeypatch.setattr(sync, 'sync_file', slow_file)
    worker = sync.NativeArchiveSync(_cfg(tmp_path), interval=15,
                                   sources=lambda: [('codex', root)])
    worker.scan_once()
    assert len(visited) == 1
    worker.scan_once()
    assert len(visited) == 2 and len(set(visited)) == 2
    worker.scan_once()
    assert len(visited) == 3 and len(set(visited)) == 3


def test_desktop_sync_reaches_latest_page_without_waiting_for_archive_scan(tmp_path):
    from tools.session.sync import sync_desktop, native_identity
    from tools.session.runner_capture import session_prefix
    from tools.supervisor.desktop_control import DesktopControl
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    cfg.supervisor_enabled = True
    cfg.supervisor_manifest_dir = tmp_path / 'manifests'
    class Bridge:
        def request(self, path):
            if 'cursor=next' in path:
                return {'messages': [{'id': 'a1', 'role': 'assistant', 'text': 'latest reply'}],
                        'nextCursor': None}
            return {'messages': [{'id': 'u1', 'role': 'user', 'text': 'old message'}],
                    'nextCursor': 'next'}
    def send(cfg, path, events):
        r = client.post(path, json=events, headers={'X-Runner-Credential': 'pxed:secret'})
        return r.status_code, r.get_json()
    desktop = DesktopControl(cfg, client=Bridge())
    path = tmp_path / 'model-io-sess_desktop.jsonl'
    row = {'session': {'sessionId': 'sess_desktop', 'title': 'Native desktop title'}, 'status': 'idle'}
    sync_desktop(cfg, desktop, row, path, send)
    sid = session_prefix(cfg.machine) + native_identity('zcode', path)
    def messages():
        events = client.get('/api/sessions/' + sid + '/events').get_json()['events']
        return [e['payload']['text'] for e in events
                if e['kind'] in {'user_message', 'assistant_message'}]
    assert messages() == ['old message', 'latest reply']
    sync_desktop(cfg, desktop, row, path, send)
    assert messages() == ['old message', 'latest reply']
    events = client.get('/api/sessions/' + sid + '/events').get_json()['events']
    assert sum(e['payload'].get('title') == 'Native desktop title' for e in events) == 1


def test_zcode_desktop_tool_capture_from_native_storage(tmp_path, monkeypatch):
    import sqlite3

    from tools.session.runner_capture import session_prefix
    from tools.session.sync import native_identity, sync_desktop
    from tools.supervisor.desktop_control import DesktopControl

    native_id = 'sess_desktop_tools'
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    database = tmp_path / '.zcode' / 'cli' / 'db' / 'db.sqlite'
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE session(id TEXT, directory TEXT, title TEXT)')
        connection.execute('INSERT INTO session VALUES (?, ?, ?)',
                           (native_id, str(tmp_path), 'Tool evidence'))
        connection.execute('CREATE TABLE message(id TEXT, session_id TEXT, data TEXT)')
        connection.execute('CREATE TABLE part(message_id TEXT, session_id TEXT, data TEXT)')
        rows = [('hidden', {'role': 'assistant'},
                 {'type': 'reasoning', 'text': 'PRIVATE_REASONING_SENTINEL'})]
        rows.extend((f'call_{index}', {'role': 'assistant'},
                     {'type': 'tool', 'callID': f'native_call_{index}', 'tool': 'Read',
                      'state': {'status': 'completed',
                                'input': {'file_path': f'file_{index}.txt'},
                                'output': f'native tool result {index}'}})
                    for index in range(257))
        for message_id, message, part in rows:
            connection.execute('INSERT INTO message VALUES (?, ?, ?)',
                               (message_id, native_id, json.dumps(message)))
            connection.execute('INSERT INTO part VALUES (?, ?, ?)',
                               (message_id, native_id, json.dumps(part)))

    cfg, client = _cfg(tmp_path), _client(tmp_path)
    cfg.supervisor_enabled = True
    cfg.supervisor_manifest_dir = tmp_path / 'manifests'

    class DesktopMessages:
        def request(self, path):
            return {'messages': [
                {'id': 'user_1', 'role': 'user', 'text': 'Inspect README.md'},
                {'id': 'assistant_1', 'role': 'assistant', 'text': 'I will inspect it.'},
            ], 'nextCursor': None}

    def send(config, path, events):
        response = client.post(path, json=events,
                               headers={'X-Runner-Credential': 'pxed:secret'})
        return response.status_code, response.get_json()

    path = tmp_path / 'model-io-sess_desktop_tools.jsonl'
    desktop = DesktopControl(cfg, client=DesktopMessages())
    row = {'session': {'sessionId': native_id}, 'status': 'idle'}
    sync_desktop(cfg, desktop, row, path, send)
    session_id = session_prefix(cfg.machine) + native_identity('zcode', path)
    event_url = f'/api/sessions/{session_id}/events?include_source=0&tail=1&limit=1000'
    def all_events():
        page = client.get(event_url).get_json()
        events = list(page['events'])
        while page['has_more']:
            page = client.get(
                f'/api/sessions/{session_id}/events', query_string={
                    'include_source': '0', 'before_cursor': page['before_cursor'], 'limit': 1000,
                }).get_json()
            events = page['events'] + events
        return events

    first_tool_events = [event for event in all_events()
                         if event['kind'] in {'tool_call', 'tool_result'}]
    assert len(first_tool_events) == 510
    assert first_tool_events[0]['payload']['call_id'] == 'native_call_0'
    checkpoint = cfg.cache_dir / 'conversations' / f'{session_id}.desktop-cursor'
    state = json.loads(checkpoint.read_text(encoding='utf-8'))
    assert state['native_cursor'] > 0

    sync_desktop(cfg, desktop, row, path, send)
    events = all_events()
    tool_events = [event for event in events
                   if event['kind'] in {'tool_call', 'tool_result'}]
    assert len(tool_events) == 514
    assert tool_events[-1]['payload']['call_id'] == 'native_call_256'
    serialized = json.dumps(events, ensure_ascii=False)
    assert 'native tool result 0' in serialized
    assert 'native tool result 256' in serialized
    assert 'PRIVATE_REASONING_SENTINEL' not in serialized
    assert str(database) not in serialized

    state = json.loads(checkpoint.read_text(encoding='utf-8'))
    state['native_cursor'] = 0
    checkpoint.write_text(json.dumps(state), encoding='utf-8')
    sync_desktop(cfg, desktop, row, path, send)
    sync_desktop(cfg, desktop, row, path, send)
    replayed_events = all_events()
    assert sum(event['kind'] in {'tool_call', 'tool_result'} for event in replayed_events) == 514


def test_slow_desktop_history_yields_without_skipping_or_duplicating_messages(tmp_path, monkeypatch):
    from tools.session import sync
    from tools.session.bridge import SessionBridge
    from tools.session.runner_capture import session_prefix
    from tools.supervisor.desktop_control import DesktopControl
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    cfg.supervisor_enabled = True
    cfg.supervisor_manifest_dir = tmp_path / 'manifests'
    now, captured = [0.0], []
    monkeypatch.setattr(sync.time, 'monotonic', lambda: now[0])
    original = SessionBridge.ingest_one
    def slow_disk(self, event, **kwargs):
        result = original(self, event, **kwargs)
        if event.get('kind') == 'assistant_message':
            captured.append(event['payload']['text'])
            now[0] += 0.6
        return result
    monkeypatch.setattr(SessionBridge, 'ingest_one', slow_disk)
    class Bridge:
        def request(self, path):
            return {'messages': [{'id': str(i), 'role': 'assistant', 'text': str(i)}
                                 for i in range(6)], 'nextCursor': None}
    available = [False]
    def send(cfg, path, events):
        if not available[0]:
            return 503, {}
        response = client.post(path, json=events, headers={'X-Runner-Credential': 'pxed:secret'})
        return response.status_code, response.json
    desktop = DesktopControl(cfg, client=Bridge())
    path = tmp_path / 'model-io-sess_slow.jsonl'
    row = {'session': {'sessionId': 'sess_slow'}, 'status': 'idle'}
    assert sync.sync_desktop(cfg, desktop, row, path, send, budget_s=1) is False
    assert captured == ['0', '1']
    # Restart the capture and retry a failed upload from the durable cursor.
    available[0] = True
    for _ in range(4):
        complete = sync.sync_desktop(cfg, desktop, row, path, send, budget_s=1)
        if complete:
            break
    assert complete
    sid = session_prefix(cfg.machine) + sync.native_identity('zcode', path)
    events = client.get('/api/sessions/' + sid + '/events').json['events']
    assert [e['payload']['text'] for e in events if e['kind'] == 'assistant_message'] == list('012345')
    assert captured == list('012345')


def test_desktop_scan_prioritizes_changes_and_continues_unvisited_sessions(tmp_path, monkeypatch):
    from tools.session import sync
    from tools.supervisor.desktop_control import DesktopControl
    cfg = _cfg(tmp_path)
    cfg.supervisor_enabled = True
    cfg.supervisor_manifest_dir = tmp_path / 'manifests'
    rows = {name: {'session': {'sessionId': name}, 'status': 'idle', 'updatedAt': '1'}
            for name in ('sess_a', 'sess_b', 'sess_c')}
    now, visited = [0.0], []
    monkeypatch.setattr(sync.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(DesktopControl, 'bindings', lambda _: rows)
    def slow_desktop(cfg, desktop, row, path, send, **kwargs):
        visited.append(row['session']['sessionId'])
        now[0] += 20  # One slow upload may exceed the scan budget.
    monkeypatch.setattr(sync, 'sync_desktop', slow_desktop)
    worker = sync.NativeArchiveSync(cfg, sources=lambda: [])
    worker.scan_once()
    assert visited == ['sess_a']
    worker.scan_once()
    assert visited == ['sess_a', 'sess_b']
    rows['sess_a']['updatedAt'] = '2'  # A new reply must preempt old history.
    worker.scan_once()
    assert visited == ['sess_a', 'sess_b', 'sess_a']
    worker.scan_once()
    assert visited[-1] == 'sess_c'
    worker.close()
    worker.scan_once()
    assert len(visited) == 4


def test_desktop_empty_assistant_placeholder_is_read_again_after_completion(tmp_path):
    from tools.session.sync import sync_desktop, native_identity
    from tools.session.runner_capture import session_prefix
    from tools.supervisor.desktop_control import DesktopControl
    cfg, client = _cfg(tmp_path), _client(tmp_path)
    cfg.supervisor_enabled = True
    cfg.supervisor_manifest_dir = tmp_path / 'manifests'
    class Bridge:
        text = ''
        def request(self, path):
            return {'messages': [{'id': 'u', 'role': 'user', 'text': 'question'},
                                 {'id': 'a', 'role': 'assistant', 'text': self.text}],
                    'nextCursor': None}
    bridge = Bridge()
    desktop = DesktopControl(cfg, client=bridge)
    path = tmp_path / 'model-io-sess_placeholder.jsonl'
    row = {'session': {'sessionId': 'sess_placeholder'}, 'status': 'idle'}
    def send(cfg, path, events):
        response = client.post(path, json=events, headers={'X-Runner-Credential': 'pxed:secret'})
        return response.status_code, response.json
    sync_desktop(cfg, desktop, row, path, send)
    bridge.text = 'completed answer'
    sync_desktop(cfg, desktop, row, path, send)
    sync_desktop(cfg, desktop, row, path, send)
    sid = session_prefix(cfg.machine) + native_identity('zcode', path)
    events = client.get('/api/sessions/' + sid + '/events').json['events']
    assert [(e['kind'], e['payload']['text']) for e in events
            if e['kind'] in {'user_message', 'assistant_message'}] == [
                ('user_message', 'question'), ('assistant_message', 'completed answer')]


@pytest.mark.parametrize('failure_kind', [
    'session_metadata', 'user_message', 'assistant_message', 'agent_state_changed',
])
@pytest.mark.parametrize('failure_mode', ['spool_error', 'quota'])
def test_desktop_persistence_rejection_preserves_checkpoint_and_retry(
        tmp_path, monkeypatch, failure_kind, failure_mode):
    from tools.session.runner_capture import session_prefix
    from tools.session.spool import LocalSpool, SpoolError
    from tools.session.sync import native_identity, sync_desktop
    from tools.supervisor.desktop_control import DesktopControl

    cfg, client = _cfg(tmp_path), _client(tmp_path)
    cfg.supervisor_enabled = True
    cfg.supervisor_manifest_dir = tmp_path / 'manifests'
    monkeypatch.setenv('ZCODE_DATA_BASE_DIR', str(tmp_path))

    class DesktopMessages:
        def request(self, path):
            return {'messages': [
                {'id': 'u1', 'role': 'user', 'text': 'durable question'},
                {'id': 'a1', 'role': 'assistant', 'text': 'durable answer'},
            ], 'nextCursor': None}

    def send(config, path, events):
        response = client.post(path, json=events,
                               headers={'X-Runner-Credential': 'pxed:secret'})
        return response.status_code, response.get_json()

    blocked = [True]
    original = LocalSpool.append

    def reject(self, event):
        target = event['kind'] == failure_kind
        if failure_kind == 'session_metadata':
            target = target and event['payload'].get('title') == 'Recovery title'
        if blocked[0] and target:
            if failure_mode == 'spool_error':
                raise SpoolError('checkpoint_corrupt')
            maximum = self._max_session_bytes
            self._max_session_bytes = 0
            try:
                return original(self, event)
            finally:
                self._max_session_bytes = maximum
        return original(self, event)

    monkeypatch.setattr(LocalSpool, 'append', reject)
    path = tmp_path / 'model-io-sess_persistence.jsonl'
    desktop = DesktopControl(cfg, client=DesktopMessages())
    row = {'session': {'sessionId': 'sess_persistence', 'title': 'Recovery title'},
           'status': 'idle'}
    sid = session_prefix(cfg.machine) + native_identity('zcode', path)
    checkpoint = cfg.cache_dir / 'conversations' / (sid + '.desktop-cursor')
    with pytest.raises(RuntimeError, match='event_not_persisted'):
        sync_desktop(cfg, desktop, row, path, send)
    state = json.loads(checkpoint.read_text(encoding='utf-8'))
    if failure_kind == 'session_metadata':
        assert state.get('metadata') != {'title': 'Recovery title'}
    elif failure_kind == 'assistant_message':
        assert state['seen'] == ['u1']
    else:
        assert state['seen'] == []

    blocked[0] = False
    assert sync_desktop(cfg, desktop, row, path, send)
    assert sync_desktop(cfg, desktop, row, path, send)
    events = client.get('/api/sessions/' + sid + '/events').get_json()['events']
    assert [(event['kind'], event['payload']['text']) for event in events
            if event['kind'] in {'user_message', 'assistant_message'}] == [
                ('user_message', 'durable question'), ('assistant_message', 'durable answer')]
    assert any(event['kind'] == 'session_metadata'
               and event['payload'].get('title') == 'Recovery title' for event in events)


def test_desktop_persistence_checkpoint_write_loss_deduplicates_on_restart(tmp_path, monkeypatch):
    from pathlib import Path

    from tools.session.runner_capture import session_prefix
    from tools.session.sync import native_identity, sync_desktop
    from tools.supervisor.desktop_control import DesktopControl

    cfg, client = _cfg(tmp_path), _client(tmp_path)
    cfg.supervisor_enabled = True
    cfg.supervisor_manifest_dir = tmp_path / 'manifests'
    monkeypatch.setenv('ZCODE_DATA_BASE_DIR', str(tmp_path))

    class DesktopMessages:
        def request(self, path):
            return {'messages': [{'id': 'a1', 'role': 'assistant', 'text': 'saved once'}],
                    'nextCursor': None}

    def send(config, path, events):
        response = client.post(path, json=events,
                               headers={'X-Runner-Credential': 'pxed:secret'})
        return response.status_code, response.get_json()

    blocked = [True]
    original = Path.write_text

    def reject(self, data, *args, **kwargs):
        if self.suffix == '.desktop-cursor' and blocked[0] and json.loads(data)['seen'] == ['a1']:
            raise OSError('checkpoint write interrupted')
        return original(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', reject)
    path = tmp_path / 'model-io-sess_checkpoint_loss.jsonl'
    desktop = DesktopControl(cfg, client=DesktopMessages())
    row = {'session': {'sessionId': 'sess_checkpoint_loss'}, 'status': 'idle'}
    with pytest.raises(OSError, match='checkpoint write interrupted'):
        sync_desktop(cfg, desktop, row, path, send)
    blocked[0] = False
    assert sync_desktop(cfg, desktop, row, path, send)
    sid = session_prefix(cfg.machine) + native_identity('zcode', path)
    events = client.get('/api/sessions/' + sid + '/events').get_json()['events']
    assert [event['payload']['text'] for event in events
            if event['kind'] == 'assistant_message'] == ['saved once']
