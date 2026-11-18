"""UI and assistant must observe the same live adoption/control authority."""
import pytest
from pathlib import Path

from hub.bootstrap import create_app
from hub.config import FleetConfig
from tests.test_adoption_service import instance_row, DEFAULT_PID, DEFAULT_STARTED_AT
from tests.test_session_interactions import event, question

OWNER = 'operator@example.test'


@pytest.fixture
def app(tmp_path):
    result = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER,
        frontend_dir=Path(__file__).resolve().parents[1] / 'frontend',
        platform_enabled=True, platform_worker_enabled=True,
        session_repositories_enabled=True, adoption_repositories_enabled=True,
        supervisor_enabled=True, supervisor_signing_raw=bytes(range(32)),
        supervisor_credentials={'m1': 'browser-receipt'},
        append_user_turn_enabled=True))
    result.extensions['fleet']['services']['observe'].ingest({
        'machine': 'm1', 'instances': [instance_row()], 'agents': {}})
    return result


def adopted(app, *, family='codex', captured=True):
    services = app.extensions['fleet']['services']
    services['observe'].ingest({'machine': 'm1', 'instances': [instance_row(agent_family=family, native_file_path="/sessions/native.jsonl")], 'agents': {}})
    row = services['adoptions'].adopt('m1', DEFAULT_PID, DEFAULT_STARTED_AT, OWNER)
    services['adoptions'].adoption_repo.update_status(row.session_id, 'adopted')
    if captured:
        start = event(1, 'session_start', {'agent_family': family, 'adapter': 'hooks',
                                        'interactive_questions': True})
        start['session_id'] = row.session_id
        ask = event(2, 'interaction_request', question())
        ask['session_id'] = row.session_id
        assert services['sessions'].ingest_events([start, ask])['rejected'] == []
    return row


def assistant(app, sid):
    return app.extensions['fleet']['services']['agent_sessions'].execute(
        tool='session.get', arguments={'session_id': sid}, owner_id=OWNER, command_id='inspect')['session']


def test_revoked_session_has_identical_authority_in_http_list_detail_and_assistant(app):
    row = adopted(app)
    app.extensions['fleet']['services']['adoptions'].revoke(row.session_id, OWNER)
    client = app.test_client()
    detail = client.get('/api/sessions/' + row.session_id).json['session']
    listed = client.get('/api/sessions?machine=m1').json['sessions'][0]
    for view in (detail, listed, assistant(app, row.session_id)):
        assert view['adoption_status'] == 'revoked'
        assert view['allowed_actions'] == []
        assert view['control_capability'] == 'unavailable'


def test_adoption_detail_exists_before_any_capture(app):
    row = adopted(app, captured=False)
    response = app.test_client().get('/api/sessions/' + row.session_id)
    assert response.status_code == 200
    detail = response.json['session']
    assert detail['adoption_status'] == 'adopted'
    assert detail['allowed_actions'] == assistant(app, row.session_id)['allowed_actions']


@pytest.mark.parametrize('answer_question', [False, True])
def test_http_cannot_enqueue_to_revoked_adoption(app, answer_question):
    row = adopted(app)
    services = app.extensions['fleet']['services']
    services['adoptions'].revoke(row.session_id, OWNER)
    services['supervisor'].poll('m1')  # Drain lifecycle commands, never execute them.
    payload = {'text': 'continue'}
    if answer_question:
        payload = {'request_id': 'q1', 'client_token': 'answer-1', 'selected': ['a']}
    response = app.test_client().post('/api/sessions/' + row.session_id + '/messages', json=payload)
    assert response.status_code == 409, response.json
    assert services['supervisor'].poll('m1') == []


def test_readoption_keeps_old_session_uncontrollable(app):
    old = adopted(app)
    services = app.extensions['fleet']['services']
    services['adoptions'].revoke(old.session_id, OWNER)
    new = adopted(app, captured=False)
    assert old.session_id != new.session_id
    assert assistant(app, old.session_id)['allowed_actions'] == []
    assert 'append_user_turn' in assistant(app, new.session_id)['allowed_actions']
    old_http = app.test_client().get('/api/sessions/' + old.session_id).json['session']
    assert old_http['control_capability'] == 'unavailable'


def test_http_hermes_followup_is_rejected_before_enqueue(app):
    row = adopted(app, family='hermes')
    services = app.extensions['fleet']['services']
    services['supervisor'].poll('m1')
    response = app.test_client().post('/api/sessions/' + row.session_id + '/messages', json={'text': 'continue'})
    assert response.status_code == 409, response.json
    assert services['supervisor'].poll('m1') == []


@pytest.mark.parametrize('state', ['pending', 'missing', 'unavailable'])
def test_unknown_authority_never_grants_controls(app, state):
    row = adopted(app)
    sessions = app.extensions['fleet']['services']['sessions']
    if state == 'pending':
        # Create the real pending state with its own candidate identity.
        sessions.adoptions.revoke(row.session_id, OWNER)
        row = sessions.adoptions.adopt('m1', DEFAULT_PID, DEFAULT_STARTED_AT, OWNER)
        start = event(1, 'session_start', {'agent_family': 'codex', 'adapter': 'hooks'})
        start['session_id'] = row.session_id
        sessions.ingest_events([start])
    elif state == 'unavailable':
        sessions.adoptions = None
    else:
        start = event(1, 'session_start', {'agent_family': 'codex', 'adapter': 'hooks'})
        start['session_id'] = 'adopt_missing'
        sessions.ingest_events([start])
    sid = 'adopt_missing' if state == 'missing' else row.session_id
    data = app.test_client().get('/api/sessions/' + sid).json['session']
    assert data['adoption_status'] == state
    assert data['allowed_actions'] == []
    response = app.test_client().post('/api/sessions/' + sid + '/messages', json={'text': 'continue'})
    assert response.status_code in (404, 409, 503)


def test_authoritative_family_overrides_stale_capture(app):
    row = adopted(app, family='hermes', captured=False)
    services = app.extensions['fleet']['services']
    start = event(1, 'session_start', {'agent_family': 'codex', 'adapter': 'hooks'})
    start['session_id'] = row.session_id
    services['sessions'].ingest_events([start])
    data = app.test_client().get('/api/sessions/' + row.session_id).json['session']
    assert data['agent_family'] == 'hermes'
    assert 'append_user_turn' not in data['allowed_actions']


def test_adoption_read_failure_cannot_fall_back_to_capture(app, monkeypatch):
    from hub.infrastructure.adoption_repository import AdoptionRepositoryError
    row = adopted(app)
    def fail(*args):
        raise AdoptionRepositoryError('adoption_store')
    monkeypatch.setattr(app.extensions['fleet']['services']['adoptions'].adoption_repo, 'get_many', fail)
    client = app.test_client()
    for path in ('/api/sessions', '/api/sessions/' + row.session_id):
        response = client.get(path)
        assert response.status_code == 503
        assert response.json['error'] == 'adoption_store'


def test_list_authority_lookup_is_batched(app, monkeypatch):
    row = adopted(app)
    services = app.extensions['fleet']['services']
    services['adoptions'].revoke(row.session_id, OWNER)
    adopted(app)
    repo = services['adoptions'].adoption_repo
    calls = []
    original = repo.get_many
    def batch(ids):
        calls.append(ids)
        return original(ids)
    monkeypatch.setattr(repo, 'get_many', batch)
    def per_row(*args):
        pytest.fail('list must not resolve adoption per row')
    monkeypatch.setattr(repo, 'get', per_row)
    response = app.test_client().get('/api/sessions?machine=m1')
    assert response.status_code == 200
    assert len(calls) == 1 and len(calls[0]) == 2


def test_revocation_between_projection_and_dispatch_rejects_command(app, monkeypatch):
    row = adopted(app)
    services = app.extensions['fleet']['services']
    original = services['adoptions'].control
    def revoke_then_control(sid, **kwargs):
        services['adoptions'].revoke(sid, OWNER)
        return original(sid, **kwargs)
    monkeypatch.setattr(services['adoptions'], 'control', revoke_then_control)
    response = app.test_client().post('/api/sessions/' + row.session_id + '/messages', json={'text': 'continue'})
    assert response.status_code == 409
    assert not any(c['action'] == 'append_user_turn' for c in services['supervisor'].poll('m1'))


def test_adopted_question_keeps_idempotency_and_audit_actor(app, monkeypatch):
    row = adopted(app)
    services = app.extensions['fleet']['services']
    client = app.test_client()
    audited = []
    original_audit = services['adoptions'].transcripts.append_audit
    def audit(actor, action, **kwargs):
        audited.append((actor, action))
        return original_audit(actor, action, **kwargs)
    monkeypatch.setattr(services['adoptions'].transcripts, 'append_audit', audit)
    payload = {'request_id': 'q1', 'client_token': 'answer-1', 'selected': ['a']}
    first = client.post('/api/sessions/' + row.session_id + '/messages', json=payload)
    second = client.post('/api/sessions/' + row.session_id + '/messages', json=payload)
    assert first.status_code == second.status_code == 202
    assert first.json == second.json
    commands = [c for c in services['supervisor'].poll('m1') if c['action'] == 'append_user_turn']
    assert len(commands) == 1
    assert commands[0]['target']['session_id'] == row.session_id
    assert (OWNER, 'control_session') in audited


def test_http_managed_control_uses_session_identity(app):
    services = app.extensions['fleet']['services']
    services['sessions'].ingest_events([event(1, 'session_start', {'agent_family': 'codex', 'adapter': 'hooks'})])
    response = app.test_client().post('/api/sessions/s1/control', json={'action': 'pause_session'})
    assert response.status_code == 202
    commands = services['supervisor'].poll('m1')
    assert len(commands) == 1 and commands[0]['target']['session_id'] == 's1'


def test_http_question_recovers_command_after_adoption_audit_failure(app, monkeypatch):
    row = adopted(app)
    services = app.extensions['fleet']['services']
    def unavailable(*args, **kwargs):
        raise OSError('audit temporarily unavailable')
    monkeypatch.setattr(services['adoptions'].transcripts, 'append_audit', unavailable)
    client = app.test_client()
    payload = {'request_id': 'q1', 'client_token': 'answer-1', 'selected': ['a']}
    first = client.post('/api/sessions/' + row.session_id + '/messages', json=payload)
    second = client.post('/api/sessions/' + row.session_id + '/messages', json=payload)
    assert first.status_code == 503 and second.status_code == 202
    commands = [c for c in services['supervisor'].poll('m1') if c['action'] == 'append_user_turn']
    assert len(commands) == 1
    assert commands[0]['command_id'] == second.json['command_id']


def test_adoption_connection_failure_is_bounded(app, monkeypatch):
    import sqlite3
    row = adopted(app)
    def unavailable():
        raise sqlite3.OperationalError('private path unavailable')
    monkeypatch.setattr(app.extensions['fleet']['services']['adoptions'].adoption_repo, '_connect', unavailable)
    response = app.test_client().get('/api/sessions/' + row.session_id)
    assert response.status_code == 503
    assert response.json['error'] == 'adoption_store'
    assert 'private path' not in response.get_data(as_text=True)


def test_maximum_adoption_page_uses_one_connection(app, monkeypatch):
    row = adopted(app)
    repo = app.extensions['fleet']['services']['adoptions'].adoption_repo
    original_connect = repo._connect
    connections, queries = [], []
    def connect():
        connection = original_connect()
        connection.set_trace_callback(queries.append)
        connections.append(connection)
        return connection
    monkeypatch.setattr(repo, '_connect', connect)
    records = repo.get_many([row.session_id] + [f'adopt_missing_{i}' for i in range(999)])
    assert list(records) == [row.session_id]
    assert len(connections) == 1
    assert len([q for q in queries if q.startswith('SELECT')]) == 2
