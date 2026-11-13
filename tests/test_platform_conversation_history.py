"""Bounded conversation input/pages over the original durable SQLite lifecycle."""
from __future__ import annotations

import sqlite3

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig

OWNER = 'history@example.test'


def history_app(tmp_path, turns=60):
    app = create_app(FleetConfig.from_root(tmp_path, platform_enabled=True, dev_operator=OWNER))
    repo = app.extensions['fleet']['platform_repository']
    repo.upsert_model(OWNER, {'profile_id': 'fixture', 'provider': 'deterministic', 'model': 'local'})
    repo.upsert_workspace(OWNER, {'workspace_id': 'workspace', 'root_path': str(tmp_path / 'workspace')})
    repo.update_defaults(OWNER, {'model_profile_id': 'fixture', 'workspace_id': 'workspace'}, 0)
    repo.create_conversation(OWNER, 'history', title='Long history', workspace_id='workspace')
    for index in range(turns):
        repo.append_turn(OWNER, 'history', f'msg_{index}', f'run_{index}', text=f'用户{index} ' + '中' * 500,
                         client_token=f'turn-{index}', config_snapshot={}, now=100 + index)
        repo.set_run_state(OWNER, f'run_{index}', 'succeeded', now=200 + index, result_text=f'答复{index} ' + '文' * 1000)
    return app, repo


def test_provider_history_is_utf8_bounded_complete_suffix(tmp_path):
    _, repo = history_app(tmp_path)
    context = repo.get_run_execution_context(OWNER, 'run_58')['messages']
    assert sum(len(item['content'].encode('utf-8')) for item in context) <= 65536
    assert len([item for item in context if item['role'] == 'user']) <= 32
    assert context[-1]['message_id'] == 'msg_58'
    assert all(item['message_id'] not in {'msg_59', 'reply_run_58', 'reply_run_59'} for item in context)
    users = [int(item['message_id'].split('_')[-1]) for item in context if item['role'] == 'user']
    assert users == list(range(users[0], 59)) and users[0] > 0
    assert [item['role'] for item in context] == ['user', 'assistant'] * (len(users) - 1) + ['user']
    assert repo.get_conversation(OWNER, 'history', limit=100)['messages'][0]['content'].endswith('中' * 500)


def test_page_keyset_and_request_projection_are_bounded(tmp_path):
    app, repo = history_app(tmp_path)
    repo.append_run_event(OWNER, 'run_0', 'provider_request_started', {'request_id': 'old', 'started_at': 101}, now=101)
    traced = []
    connect = repo._connect
    def tracked():
        conn = connect()
        conn.set_trace_callback(traced.append)
        return conn
    repo._connect = tracked
    client = app.test_client()
    page = client.get('/api/platform/v1/conversations/history').get_json()['conversation']
    assert [m['message_id'] for m in page['messages'] if m['role'] == 'user'] == [f'msg_{i}' for i in range(40, 60)]
    assert len(page['runs']) == 20 and page['history'] == {'has_more': True, 'before_sequence': 41, 'limit': 20}
    event_queries = [q for q in traced if 'SELECT e.run_id,e.kind,e.payload' in q]
    assert len(event_queries) == 1 and "'run_0'" not in event_queries[0]
    assert ' IN (' in event_queries[0] and 'r.conversation_id=' not in event_queries[0]
    ids = [m['message_id'] for m in page['messages'] if m['role'] == 'user']
    while page['history']['has_more']:
        page = client.get('/api/platform/v1/conversations/history', query_string={'before_sequence': page['history']['before_sequence']}).get_json()['conversation']
        ids = [m['message_id'] for m in page['messages'] if m['role'] == 'user'] + ids
    assert ids == [f'msg_{i}' for i in range(60)]
    assert repo.get_conversation('other@example.test', 'history') is None


def test_old_retry_uses_original_token_after_catalog_disabled(tmp_path):
    app, repo = history_app(tmp_path)
    repo.upsert_model(OWNER, {'profile_id': 'fixture', 'provider': 'deterministic', 'model': 'local', 'enabled': False})
    client = app.test_client()
    body = {'text': '用户0 ' + '中' * 500, 'client_token': 'turn-0'}
    response = client.post('/api/platform/v1/conversations/history/turns', json=body)
    assert response.status_code == 202 and response.get_json()['run']['run_id'] == 'run_0'
    body['text'] = 'changed'
    assert client.post('/api/platform/v1/conversations/history/turns', json=body).status_code == 409


@pytest.mark.parametrize('query', [{'limit': 0}, {'limit': 101}, {'before_sequence': 0}, {'before_sequence': 'bad'}])
def test_invalid_page_cursor_rejected(tmp_path, query):
    app, _ = history_app(tmp_path, 1)
    assert app.test_client().get('/api/platform/v1/conversations/history', query_string=query).status_code == 400


def test_conversation_history_chromium_loads_earlier_and_refreshes(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    from threading import Thread
    import time
    from werkzeug.serving import make_server

    if not os.environ.get('FLEET_PLAYWRIGHT_MODULE'):
        pytest.skip('Set FLEET_PLAYWRIGHT_MODULE for actual Chromium acceptance')
    app, repo = history_app(tmp_path, 45)
    # Serve exactly the current source frontend, never a cached release.
    root = Path(__file__).resolve().parents[1]
    app.config['FRONTEND_DIR'] = root / 'frontend'
    repo.upsert_model(OWNER, {'profile_id': 'fixture', 'provider': 'deterministic', 'model': 'local'})

    def finish():
        with sqlite3.connect(repo.db_path) as connection:
            run_id = connection.execute("SELECT run_id FROM runs WHERE owner_id=? AND state='queued' ORDER BY created_at DESC LIMIT 1", (OWNER,)).fetchone()[0]
        repo.set_run_state(OWNER, run_id, 'succeeded', now=time.time(), result_text='最新完成')
        return {'ok': True}
    app.add_url_rule('/_fixture/finish-history', 'finish_history', finish, methods=['POST'])
    server = make_server('127.0.0.1', 0, app, threaded=True)
    serving = Thread(target=server.serve_forever, daemon=True); serving.start()
    try:
        result = subprocess.run(['node', str(root / 'tests/fixtures/frontend/conversation_history_browser.cjs'),
                                 f'http://127.0.0.1:{server.server_port}'], text=True, capture_output=True, timeout=45)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.shutdown(); server.server_close(); serving.join(5)


def test_history_count_limit_and_trigger_survive_oversized_previous_reply(tmp_path):
    _, repo = history_app(tmp_path, 40)
    with sqlite3.connect(repo.db_path) as connection:
        connection.execute("UPDATE messages SET content='short' WHERE conversation_id='history'")
        connection.execute("UPDATE runs SET result_text='reply' WHERE conversation_id='history'")
    context = repo.get_run_execution_context(OWNER, 'run_38')['messages']
    assert len(context) == 63 and context[0]['message_id'] == 'msg_7'
    with sqlite3.connect(repo.db_path) as connection:
        connection.execute('UPDATE runs SET result_text=? WHERE run_id=?', ('大' * 30000, 'run_37'))
        connection.execute('UPDATE messages SET content=? WHERE message_id=?', ('当' * 10000, 'msg_38'))
    context = repo.get_run_execution_context(OWNER, 'run_38')['messages']
    assert context == [{'message_id': 'msg_38', 'role': 'user', 'content': '当' * 10000}]
    repo.set_run_state(OWNER, 'run_38', 'queued', now=500)
    claim = repo.claim_run(OWNER, worker_id='history-worker', now=501)
    assert claim['run_id'] == 'run_38' and claim['messages'] == context


def test_turn_submission_does_not_materialize_history(tmp_path, monkeypatch):
    app, repo = history_app(tmp_path, 2)
    def full_read(*args, **kwargs):
        raise AssertionError('submission must not materialize a history page')
    monkeypatch.setattr(repo, 'get_conversation', full_read)
    response = app.test_client().post('/api/platform/v1/conversations/history/turns',
                                    json={'text': 'new', 'client_token': 'new-token'})
    assert response.status_code == 202
