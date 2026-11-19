"""Native ZCode transcripts and CLI continuation use explicit session identity."""
import json
from tools.probe import discovery
from tools.session.bridge import _shape_native_row

SID = 'sess_2912e01e-1111-4444-aaaa-123456789abc'


def test_desktop_host_is_not_a_single_controllable_conversation():
    assert discovery.classify_family('/Applications/ZCode.app/Contents/MacOS/ZCode', 'ZCode') is None
    assert discovery.classify_family(r'C:\Apps\ZCode\ZCode.exe', r'C:\Apps\ZCode\ZCode.exe') is None
    assert discovery.classify_family('/usr/local/bin/zcode', f'zcode --resume {SID}') == 'zcode'
    assert discovery.classify_family(r'C:\bin\zcode.exe', f'zcode --resume {SID}') == 'zcode'


def test_zcode_explicit_resume_binds_matching_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    root = tmp_path / '.zcode' / 'cli' / 'rollout'
    root.mkdir(parents=True)
    path = root / f'model-io-{SID}.jsonl'
    path.write_text(json.dumps({'type': 'model_io', 'sessionId': SID}) + '\n')
    row = discovery.ProcRow('user', 1, 1, '/usr/local/bin/zcode', f'zcode --resume {SID}')
    assert discovery.native_path_for(row) == str(path)
    unbound = discovery.ProcRow('user', 2, 2, '/usr/local/bin/zcode', 'zcode -p hello')
    assert discovery.native_path_for(unbound) is None
    path.write_text(json.dumps({'type': 'model_io', 'sessionId': 'sess_other'}) + '\n')
    assert discovery.native_path_for(row) is None


def test_zcode_logs_normalize_current_turn_and_tool_results_without_replaying_context():
    row = {'type': 'model_io', 'sessionId': SID, 'request': {'messages': [
        {'role': 'system', 'content': 'private system instruction'},
        {'role': 'user', 'content': 'old message'},
        {'role': 'assistant', 'content': 'old answer'},
        {'role': 'tool', 'toolCallId': 'call1', 'toolName': 'Read', 'content': 'file contents'},
        {'role': 'user', 'content': 'continue'},
    ]}, 'response': {'text': 'current answer', 'toolCalls': [
        {'id': 'call2', 'name': 'Read', 'input': {'path': 'README.md'}}]}}
    events = _shape_native_row(row)
    assert [kind for kind, _ in events] == ['tool_result', 'user_message', 'assistant_message', 'tool_call']
    assert events[0][1]['call_id'] == 'call1'
    assert events[-1][1]['tool_name'] == 'Read'
    assert 'old answer' not in str(events) and 'private system' not in str(events)


def test_default_archive_sync_reads_zcode_history_without_adoption(tmp_path, monkeypatch):
    from tools.session.sync import default_sources, sync_file
    from test_review_node_sync import _client, _cfg
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    root = tmp_path / '.zcode' / 'cli' / 'rollout'
    root.mkdir(parents=True)
    assert ('zcode', root) in default_sources()
    native = root / f'model-io-{SID}.jsonl'
    native.write_text(json.dumps({'type': 'model_io', 'sessionId': SID,
        'request': {'messages': [{'role': 'user', 'content': 'status please'}]},
        'response': {'text': 'working on tests'}}) + '\n')
    client = _client(tmp_path)
    def send(cfg, path, body):
        response = client.post(path, json=body, headers={'X-Runner-Credential': 'pxed:secret'})
        return response.status_code, response.json
    assert sync_file(_cfg(tmp_path), 'zcode', native, send) == 1
    assert sync_file(_cfg(tmp_path), 'zcode', native, send) == 0
    sid = client.get('/api/sessions').json['sessions'][0]['session_id']
    events = client.get('/api/sessions/' + sid + '/events').json['events']
    assert [r['payload']['text'] for r in events if r['kind'] == 'assistant_message'] == ['working on tests']


def test_archive_worker_only_skips_fully_uploaded_unchanged_files(tmp_path):
    from tools.session.sync import NativeArchiveSync
    from test_review_node_sync import _client, _cfg, _line
    root = tmp_path / 'native'
    root.mkdir()
    native = root / 'one.jsonl'
    native.write_text(_line('first'))
    client = _client(tmp_path)
    calls = []
    fail = [True]
    def send(cfg, path, body):
        calls.append(body)
        if fail[0]:
            return 503, {}
        response = client.post(path, json=body, headers={'X-Runner-Credential': 'pxed:secret'})
        return response.status_code, response.json
    worker = NativeArchiveSync(_cfg(tmp_path), sources=lambda: [('codex', root)], send=send)
    worker.scan_once()
    assert not worker.completed
    fail[0] = False
    worker.scan_once()
    assert worker.completed
    before = len(calls)
    worker.scan_once()
    assert len(calls) == before
    with native.open('a') as stream:
        stream.write(_line('second'))
    worker.scan_once()
    assert len(calls) > before
    worker.close()
    with native.open('a') as stream:
        stream.write(_line('third'))
    before = len(calls)
    worker.scan_once()
    assert len(calls) == before
    sid = client.get('/api/sessions').json['sessions'][0]['session_id']
    events = client.get('/api/sessions/' + sid + '/events').json['events']
    assert [e['payload']['text'] for e in events if e['kind'] == 'user_message'] == ['first', 'second']
