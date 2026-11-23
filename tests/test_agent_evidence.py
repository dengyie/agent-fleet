"""Observable Agent facts use durable events, never family or permission guesses."""
from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest

from fleet_core.agent_evidence import project_agent_evidence
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.session_access import AdoptionAccess, project_session_access
from hub.infrastructure.session_repository import SessionRepository
from tests.test_session_interactions import event

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('family', ['codex', 'claude_code', 'claude', 'pi', 'zcode', 'hermes', 'agy', 'future-agent'])
def test_family_support_does_not_claim_observed_capture_or_installation(family):
    row = {'agent_family': family, 'managed': True}
    before = deepcopy(row)
    evidence = project_agent_evidence(row)
    assert row == before
    assert evidence['installation'] == 'unknown'
    assert evidence['protocol'] == {'name': 'unknown', 'state': 'unknown', 'source': 'none'}
    assert evidence['features']['visible_text']['state'] == 'unknown'
    assert evidence['features']['native_permissions']['state'] == 'unknown'
    assert evidence['features']['hidden_reasoning'] == {'state': 'unsupported', 'source': 'public_contract'}
    assert 'allowed_actions' not in evidence
    assert 'control_capability' not in evidence


def test_evidence_projects_only_bounded_observed_facts_and_preserves_unknown():
    evidence = project_agent_evidence({'agent_family': 'agy', 'runtime_protocol': 'acp',
        'adapter': 'structured_stream', 'model': 'gemini', 'native_title': 'Native title',
        'client_version': '1.5.8', 'capture_quality': 'structured', 'managed': True,
        'observed_features': ['visible_text', 'text_deltas', 'tool_calls', 'native_permissions',
                              'raw_path', '/private/auth-token'],
        'capabilities': ['native_resume', 'model_takeover', 'interactive_questions']})
    assert evidence['protocol'] == {'name': 'acp', 'state': 'observed', 'source': 'session_metadata'}
    assert evidence['features']['text_deltas'] == {'state': 'observed', 'source': 'session_event'}
    assert evidence['features']['native_permissions']['state'] == 'observed'
    assert evidence['features']['model']['source'] == 'session_metadata'
    assert evidence['features']['tool_results']['state'] == 'unknown'
    assert evidence['features']['resume_identity']['source'] == 'continuation_identity'
    assert evidence['capture']['quality'] == 'structured'
    assert evidence['capture']['history'] == 'unknown'
    assert '/private' not in json.dumps(evidence)
    assert 'gemini' not in json.dumps(evidence), 'metadata values are already in the bounded session DTO'


def test_unmanaged_and_revoked_capture_never_promote_authority_or_quality():
    row = {'session_id': 'adopt_fixture', 'machine_id': 'm1', 'agent_family': 'codex',
        'managed': False, 'capture_quality': 'exact', 'runtime_protocol': 'acp',
        'observed_features': ['visible_text', 'text_deltas'], 'capabilities': ['native_resume']}
    dto = project_session_access(row, AdoptionAccess('m1', 'codex', 'revoked', '/private/native'),
                                 ['append_user_turn'], adoption_available=True)
    assert dto['allowed_actions'] == []
    assert dto['agent_evidence']['capture']['quality'] == 'best_effort'
    assert dto['agent_evidence']['features']['text_deltas']['state'] == 'observed'
    assert '/private' not in json.dumps(dto)


def test_malformed_or_secret_shaped_evidence_is_not_stringified():
    evidence = project_agent_evidence({'agent_family': '/private/auth-token',
        'runtime_protocol': {'token': 'secret'}, 'adapter': '/private/raw',
        'model': {'token': 'secret'}, 'native_title': '', 'client_version': 123,
        'observed_features': {'visible_text': True}, 'capabilities': {'native_resume': True}})
    assert evidence['family'] == 'unknown'
    assert evidence['protocol']['name'] == 'unknown'
    assert evidence['capture']['source'] == 'unknown'
    assert all(evidence['features'][name]['state'] == 'unknown'
               for name in ['visible_text', 'model', 'client_version', 'resume_identity'])
    assert 'secret' not in json.dumps(evidence)


def _hub(tmp_path):
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator='reader@example.test',
        ingest_token='evidence-ingest-fixture', session_repositories_enabled=True))
    return app, app.extensions['fleet']['services']['sessions']


def test_real_http_ingestion_persists_observations_across_app_restart(tmp_path):
    app, service = _hub(tmp_path)
    rows = [event(1, 'session_start', {'agent_family': 'agy', 'adapter': 'acp',
        'runtime_protocol': 'acp', 'client_version': '1.5.8'}),
        event(2, 'assistant_message', {'text': 'visible', 'message_id': 'msg_one',
                                     'content_mode': 'delta', 'is_complete': False}),
        event(3, 'tool_call', {'call_id': 'call_one', 'tool_name': 'read', 'arguments': '{}'}),
        event(4, 'tool_result', {'call_id': 'call_one', 'tool_name': 'read', 'result': 'file content'}),
        event(5, 'session_metadata', {'model': 'observed-model', 'title': 'Observed title'})]
    assert service.ingest_events(rows)['rejected'] == []
    # Exact upload retry cannot create a new observation or alter existing facts.
    assert service.ingest_events(rows)['rejected'] == []
    before = app.test_client().get('/api/sessions/s1').get_json()['session']['agent_evidence']
    restarted, _ = _hub(tmp_path)
    client = restarted.test_client()
    detail = client.get('/api/sessions/s1')
    assert detail.status_code == 200
    evidence = detail.get_json()['session']['agent_evidence']
    assert evidence == before
    assert evidence['protocol']['name'] == 'acp'
    for name in ['visible_text', 'text_deltas', 'message_identity', 'tool_calls', 'tool_results', 'model', 'native_title', 'client_version']:
        assert evidence['features'][name]['state'] == 'observed', name
    assert evidence['features']['native_permissions']['state'] == 'unknown'
    assert evidence['features']['usage']['state'] == 'unsupported'
    assert client.get('/api/sessions').get_json()['sessions'][0]['agent_evidence'] == evidence


def test_native_permissions_and_history_gaps_are_observed_independently(tmp_path):
    app, service = _hub(tmp_path)
    ordinary = {'request_id': 'ordinary', 'question_type': 'single', 'prompt': 'Choose',
        'options': [{'id': 'option_1', 'label': 'Yes'}], 'allow_custom': False}
    assert service.ingest_events([event(1, 'interaction_request', ordinary)])['rejected'] == []
    read = lambda: app.test_client().get('/api/sessions/s1').get_json()['session']['agent_evidence']
    first = read()
    assert first['features']['interactive_questions']['state'] == 'observed'
    assert first['features']['native_permissions']['state'] == 'unknown'
    native = {**ordinary, 'request_id': 'native', 'delivery': 'native_permission'}
    assert service.ingest_events([event(2, 'interaction_request', native),
        event(3, 'capture_gap', {'start_sequence': 1, 'end_sequence': 2,
            'quality': 'best_effort', 'reason': 'source_interrupted'})])['rejected'] == []
    latest = read()
    assert latest['features']['native_permissions']['state'] == 'observed'
    assert latest['capture']['history'] == 'gaps_observed'
    assert app.test_client().get('/api/sessions/s1').get_json()['session']['allowed_actions'] == []


def test_equal_capture_quality_notification_is_not_evidence_of_history_gap(tmp_path):
    app, service = _hub(tmp_path)
    assert service.ingest_events([event(1, 'capture_quality_changed',
        {'old_quality': 'structured', 'new_quality': 'structured',
         'reason': 'source_reconfirmed'})])['rejected'] == []
    evidence = app.test_client().get('/api/sessions/s1').get_json()['session']['agent_evidence']
    assert evidence['capture']['history'] == 'unknown'
    assert service.ingest_events([event(2, 'capture_quality_changed',
        {'old_quality': 'structured', 'new_quality': 'best_effort',
         'reason': 'source_degraded'})])['rejected'] == []
    evidence = app.test_client().get('/api/sessions/s1').get_json()['session']['agent_evidence']
    assert evidence['capture']['history'] == 'gaps_observed'


def test_duplicate_event_cannot_inject_facts_and_older_metadata_cannot_replace_protocol(tmp_path):
    app, service = _hub(tmp_path)
    first = event(1, 'assistant_message', {'text': 'accepted snapshot'})
    assert service.ingest_events([first, event(2, 'session_metadata',
        {'runtime_protocol': 'cli_json'}), event(3, 'session_metadata',
        {'runtime_protocol': 'acp'})])['rejected'] == []
    forged = deepcopy(first)
    forged['payload'].update(content_mode='delta', message_id='forged_identity')
    assert service.ingest_events([forged, event(2, 'session_metadata',
        {'runtime_protocol': 'native_transcript'})])['rejected'] == []
    evidence = app.test_client().get('/api/sessions/s1').get_json()['session']['agent_evidence']
    assert evidence['protocol']['name'] == 'acp'
    assert evidence['features']['text_deltas']['state'] == 'unknown'
    assert evidence['features']['message_identity']['state'] == 'unknown'


@pytest.mark.parametrize('headers', [{}, {'X-Agent-Fleet-Token': 'ingest-fixture'},
    {'X-Runner-Credential': 'm1:runner-fixture'}, {'Authorization': 'Bearer node-fixture'}])
def test_evidence_public_reads_require_operator_identity(tmp_path, headers):
    app = create_app(FleetConfig.from_root(tmp_path, ingest_token='ingest-fixture',
        session_repositories_enabled=True))
    services = app.extensions['fleet']['services']
    services['sessions'].ingest_events([event(1, 'session_start', {'agent_family': 'agy',
        'runtime_protocol': 'acp'})])
    for path in ['/api/sessions', '/api/sessions/s1']:
        response = app.test_client().get(path, headers=headers)
        assert response.status_code == 401
        assert 'agent_evidence' not in response.get_data(as_text=True)
    response = app.test_client().get('/api/sessions/s1',
        headers={'Cf-Access-Authenticated-User-Email': 'operator@example.test'})
    assert response.status_code == 200
    assert response.get_json()['session']['agent_evidence']['protocol']['name'] == 'acp'


def test_concurrent_observed_event_unions_do_not_lose_facts(tmp_path):
    repo = SessionRepository(tmp_path / 'sessions.db')
    repo.init()
    repo.upsert_session({'session_id': 'kept', 'machine_id': 'm1'})
    rows = [('assistant_message', {'text': 'visible', 'content_mode': 'delta', 'message_id': 'msg_1'}),
        ('tool_call', {'tool_name': 'read'}), ('tool_result', {'result': 'done'}),
        ('interaction_request', {'delivery': 'native_permission'}), ('capture_gap', {})]
    def observe(item):
        fresh = SessionRepository(repo.db_path)
        fresh.observe_events('kept', [{'kind': item[0], 'payload': item[1]}])
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(observe, rows))
    restarted = SessionRepository(repo.db_path)
    restarted.init()
    assert set(restarted.get_session('kept')['observed_features']) == {
        'visible_text', 'text_deltas', 'message_identity', 'tool_calls', 'tool_results',
        'interactive_questions', 'native_permissions', 'capture_gaps'}


def test_old_database_migration_and_failure_are_atomic(tmp_path, monkeypatch):
    db = tmp_path / 'sessions.db'
    original = SessionRepository(db)
    original.init()
    original.upsert_session({'session_id': 'kept', 'machine_id': 'm1'})
    columns = ['runtime_protocol', 'runtime_protocol_cursor', 'observed_features']
    with sqlite3.connect(db) as conn:
        for name in columns:
            conn.execute(f'ALTER TABLE sessions DROP COLUMN {name}')
        conn.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
    repo = SessionRepository(db)
    migrate = repo._migrate_v6
    def fail(conn):
        migrate(conn)
        raise sqlite3.OperationalError('private migration detail')
    monkeypatch.setattr(repo, '_migrate_v6', fail)
    repo.init()
    assert repo.management_available is False
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '5'
        assert not set(columns) & {row[1] for row in conn.execute('PRAGMA table_info(sessions)')}
    fixed = SessionRepository(db)
    fixed.init()
    assert fixed.management_available is True and fixed.schema_version() == 6
    assert fixed.get_session('kept')['observed_features'] == []
    assert fixed.get_session('kept')['runtime_protocol'] == 'unknown'


def test_damaged_evidence_schema_or_values_do_not_expose_internal_data(tmp_path):
    db = tmp_path / 'sessions.db'
    repo = SessionRepository(db)
    repo.init()
    repo.upsert_session({'session_id': 'kept', 'machine_id': 'm1'})
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE sessions SET observed_features=?,runtime_protocol=? WHERE session_id='kept'",
                     ('["/private/auth-token"]', '/private/native'))
    row = repo.get_session('kept')
    assert row['observed_features'] == [] and row['runtime_protocol'] == 'unknown'
    with sqlite3.connect(db) as conn:
        conn.execute('ALTER TABLE sessions DROP COLUMN observed_features')
        conn.execute('ALTER TABLE sessions ADD COLUMN observed_features INTEGER')
        conn.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
    damaged = SessionRepository(db)
    damaged.init()
    assert damaged.management_available is False
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '5'


@pytest.mark.parametrize('raw', ['not-json', '{}', '["visible_text",' + ' ' * 512 + ']',
    '["visible_text", "text_deltas", "message_identity", "tool_calls", "tool_results", '
    '"interactive_questions", "native_permissions", "capture_gaps", "unknown"]'])
def test_damaged_or_oversized_observed_value_is_unknown(tmp_path, raw):
    repo = SessionRepository(tmp_path / 'sessions.db')
    repo.init()
    repo.upsert_session({'session_id': 'kept', 'machine_id': 'm1'})
    with sqlite3.connect(repo.db_path) as conn:
        conn.execute("UPDATE sessions SET observed_features=? WHERE session_id='kept'", (raw,))
    assert repo.get_session('kept')['observed_features'] == []


@pytest.mark.parametrize('quality', [[], {}, 1, True, None, 'made-up'])
def test_invalid_capture_quality_does_not_promote_or_break_projection(quality):
    evidence = project_agent_evidence({'capture_quality': quality, 'managed': True})
    assert evidence['capture']['quality'] == 'best_effort'


def test_observation_write_failure_is_bounded_and_recovered_by_same_upload(tmp_path, monkeypatch):
    app, service = _hub(tmp_path)
    row = event(1, 'assistant_message', {'text': 'visible', 'content_mode': 'delta'})
    observe = service.session_repo.observe_events
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('private path /private/credentials')
    monkeypatch.setattr(service.session_repo, 'observe_events', fail)
    def upload():
        return app.test_client().post('/api/session-events', json=[row],
            headers={'X-Agent-Fleet-Token': 'evidence-ingest-fixture'})
    failed = upload()
    assert failed.status_code == 503
    assert failed.get_json()['error'] == 'session_unavailable'
    assert '/private' not in failed.get_data(as_text=True)
    monkeypatch.setattr(service.session_repo, 'observe_events', observe)
    recovered = upload()
    assert recovered.status_code == 200 and recovered.get_json()['rejected'] == []
    evidence = app.test_client().get('/api/sessions/s1').get_json()['session']['agent_evidence']
    assert evidence['features']['text_deltas']['state'] == 'observed'


def test_browser_evidence_contract_is_bounded_additive_and_never_grants_actions():
    if not shutil.which('node'):
        pytest.skip('Node is an optional contract verifier')
    evidence = project_agent_evidence({'agent_family': 'agy', 'runtime_protocol': 'acp'})
    script = r"""
const assert = await import('node:assert/strict');
const {parseSession, parseSessionEvents, ContractError} = await import(process.argv[1]);
const evidence = JSON.parse(process.argv[2]);
const base = {session_id:'runner_fixture',machine_id:'m1',managed:true,allowed_actions:[],control_capability:'unavailable'};
assert.deepEqual(parseSession({session:{...base,agent_evidence:{...evidence,raw_path:'/private'}}}).session.agent_evidence,evidence);
assert.deepEqual(parseSession({session:{...base,agent_evidence:evidence}}).session.allowed_actions,[]);
assert.equal(parseSession({session:base}).session.agent_evidence,undefined);
assert.throws(()=>parseSession({session:{...base,agent_evidence:{...evidence,installation:'installed'}}}),ContractError);
assert.throws(()=>parseSession({session:{...base,agent_evidence:{...evidence,protocol:{name:'shell',state:'observed',source:'session_metadata'}}}}),ContractError);
assert.throws(()=>parseSession({session:{...base,agent_evidence:{...evidence,features:{...evidence.features,visible_text:{state:'observed',source:'/private'}}}}}),ContractError);
assert.deepEqual(parseSession({session:{...base,allowed_actions:['answer_native_request'],control_capability:'available'}}).session.allowed_actions,['answer_native_request']);
const question = {request_id:'native_1', question_type:'single', prompt:'Allow?', options:[{id:'option_1',label:'Allow once'}], allow_custom:false, delivery:'native_permission'};
const event = {schema_version:1,event_id:'evt_1',session_id:'runner_fixture',machine_id:'m1',sequence:'1',cursor:'1',kind:'interaction_request',capture_quality:'structured',source:'structured_stream',emitted_at:'2026-11-23T00:00:00Z',payload:question};
const page = {events:[event],after_cursor:'0',next_cursor:'1',before_cursor:'1',has_more:false};
assert.equal(parseSessionEvents(page).events[0].payload.delivery,'native_permission');
assert.throws(()=>parseSessionEvents({...page,events:[{...event,payload:{...question,question_type:'multi'}}]}),ContractError);
assert.throws(()=>parseSessionEvents({...page,events:[{...event,payload:{...question,allow_custom:true}}]}),ContractError);
assert.throws(()=>parseSessionEvents({...page,events:[{...event,payload:{...question,delivery:'raw_json_rpc'}}]}),ContractError);
"""
    result = subprocess.run(['node', '--input-type=module', '-e', script,
        (ROOT / 'frontend/assets/api/contracts.js').as_uri(), json.dumps(evidence)],
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
