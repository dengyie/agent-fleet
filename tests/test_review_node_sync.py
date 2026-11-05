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
