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
    path.write_text(a + bad + b + '{"partial":')
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
    row = {'session': {'sessionId': 'sess_desktop'}, 'status': 'idle'}
    sync_desktop(cfg, desktop, row, path, send)
    sid = session_prefix(cfg.machine) + native_identity('zcode', path)
    def messages():
        events = client.get('/api/sessions/' + sid + '/events').get_json()['events']
        return [e['payload']['text'] for e in events
                if e['kind'] in {'user_message', 'assistant_message'}]
    assert messages() == ['old message', 'latest reply']
    sync_desktop(cfg, desktop, row, path, send)
    assert messages() == ['old message', 'latest reply']
