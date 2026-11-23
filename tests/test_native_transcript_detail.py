"""Prevent metadata loss and interleaved message-block replacement."""
from tools.session.normalization import normalize_native_row
import json

from tools.session.runner_capture import RunnerCapture
from tests.test_review_node_sync import _cfg, _client


def test_codex_turn_context_keeps_observed_model_metadata():
    assert normalize_native_row({'type': 'turn_context', 'payload': {
        'model': 'observed-codex', 'cwd': '/private/cwd'}}) == [
        ('session_metadata', {'model': 'observed-codex'})]


def test_pi_explicit_session_info_title_and_message_identity():
    assert normalize_native_row({'type': 'session_info', 'name': 'VM native name'}) == [
        ('session_metadata', {'title': 'VM native name'})]
    events = normalize_native_row({'type': 'message', 'id': 'pi-row-1', 'message': {
        'role': 'assistant', 'content': [{'type': 'text', 'text': 'before'},
            {'type': 'toolCall', 'id': 'c1', 'name': 'read', 'arguments': {}},
            {'type': 'text', 'text': 'after'}]}})
    texts = [p for k, p in events if k == 'assistant_message']
    assert [p['text'] for p in texts] == ['before', 'after']
    assert all(p['message_id'] for p in texts)
    assert texts[0]['message_id'] != texts[1]['message_id']


def test_claude_interleaved_blocks_have_distinct_stable_identities():
    row = {'type': 'assistant', 'message': {'id': 'msg_native', 'content': [
        {'type': 'text', 'text': 'before tool'},
        {'type': 'tool_use', 'id': 'tool1', 'name': 'read', 'input': {}},
        {'type': 'text', 'text': 'after tool'}]}}
    events = normalize_native_row(row)
    texts = [p for k, p in events if k == 'assistant_message']
    assert [p['text'] for p in texts] == ['before tool', 'after tool']
    assert texts[0]['message_id'] != texts[1]['message_id']
    assert events == normalize_native_row(row)


def test_claude_block_stream_and_snapshots_share_identity(tmp_path):
    cfg, client = _cfg(tmp_path), _client(tmp_path)

    def send(cfg, path, events):
        response = client.post(path, json=events, headers={'X-Runner-Credential': 'pxed:secret'})
        return response.status_code, response.get_json()

    capture = RunnerCapture(cfg, {'attempt_id': 'blocks', 'agent_type': 'claude_code',
                                 'task_id': ''}, send)
    try:
        def stream(**event):
            capture.line(json.dumps({'type': 'stream_event', 'event': event}), strict=True)
        stream(type='message_start', message={'id': 'msg_blocks'})
        for index, text in [(0, 'before'), (2, 'after'), (3, 'adjacent')]:
            stream(type='content_block_delta', index=index, delta={'type': 'text_delta', 'text': text})
            stream(type='content_block_stop', index=index)
        stream(type='content_block_stop', index={})  # malformed vendor record
        stream(type='message_stop')
        capture.line(json.dumps({'type': 'assistant', 'message': {'id': 'msg_blocks', 'content': [
            {'type': 'text', 'text': 'before'}, {'type': 'tool_use', 'id': 'c', 'name': 'read'},
            {'type': 'text', 'text': 'after'}, {'type': 'text', 'text': 'adjacent'}]}}), strict=True)
        capture.flush()
        events = client.get('/api/sessions/' + capture.session_id + '/events?include_source=0').get_json()['events']
        messages = [e['payload'] for e in events if e['kind'] == 'assistant_message']
        snapshots = [p for p in messages if p['content_mode'] == 'snapshot']
        assert len(snapshots) == 3 and len({p['message_id'] for p in snapshots}) == 3
        for snapshot in snapshots:
            deltas = [p for p in messages if p['message_id'] == snapshot['message_id'] and p['content_mode'] == 'delta']
            assert ''.join(p['text'] for p in deltas) == snapshot['text']
            assert deltas[-1]['is_complete'] is True
    finally:
        capture.bridge.close()
