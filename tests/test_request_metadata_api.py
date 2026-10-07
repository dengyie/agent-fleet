"""Real worker, SQLite and private API request history; no browser-derived totals."""
import json
import sqlite3

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.infrastructure.platform_db import PlatformRepository
from tools.platform.providers.openai_compatible import OpenAICompatibleProvider, TransportResponse

OWNER = 'metadata@example.test'
_MISSING = object()


def setup(tmp_path):
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER, platform_enabled=True, platform_worker_enabled=True))
    repo = app.extensions['fleet']['platform_repository']
    repo.upsert_model(OWNER, {'profile_id': 'model', 'provider':'openai_compatible', 'model':'frozen-model',
        'secret_ref':'env://FIXTURE', 'provider_config': {'endpoint':'https://fixture.invalid'}})
    repo.upsert_workspace(OWNER, {'workspace_id':'home', 'root_path':str(tmp_path / 'work')})
    repo.update_defaults(OWNER, {'model_profile_id':'model', 'workspace_id':'home'}, 0)
    client = app.test_client()
    conversation = client.post('/api/platform/v1/conversations', json={}).get_json()['conversation']
    run = client.post(f"/api/platform/v1/conversations/{conversation['conversation_id']}/turns",
        json={'text':'inspect', 'client_token':'one'}).get_json()['run']
    return app, repo, client, conversation, run


def test_requests_survive_restart_and_are_owner_scoped(tmp_path):
    app, repo, client, conversation, run = setup(tmp_path)
    class Transport:
        def request(self, **kwargs):
            return TransportResponse(200, {}, json.dumps({'model':'returned-model','id':'reply-one',
                'choices':[{'message':{'content':'done'}, 'finish_reason':'stop'}],
                'usage':{'prompt_tokens':47,'completion_tokens':14,'cost':.000131}}).encode())
    worker = app.extensions['fleet']['services']['platform_worker']
    worker.provider_factory = lambda profile: OpenAICompatibleProvider(model=profile['model'], endpoint='http://fixture',api_key='never-expose',transport=Transport())
    assert worker.run_once(OWNER)['state'] == 'succeeded'
    repo.upsert_model(OWNER, {'profile_id':'model','model':'changed-model','provider':'deterministic'})
    url = f"/api/platform/v1/runs/{run['run_id']}"
    value = client.get(url).get_json()
    assert value['config']['model'] == 'frozen-model'
    assert value['requests'][0]['model'] == 'returned-model'
    assert value['requests'][0]['requested_model'] == 'frozen-model'
    assert value['requests'][0]['cost']['amount'] == '0.000131'
    assert value['requests'][0]['request_id'] == '1:1:1'
    restarted = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER, platform_enabled=True))
    assert restarted.test_client().get(url).get_json()['requests'] == value['requests']
    other = create_app(FleetConfig.from_root(tmp_path, dev_operator='other@example.test', platform_enabled=True))
    assert other.test_client().get(url).status_code == 404
    assert other.test_client().get(f"/api/platform/v1/conversations/{conversation['conversation_id']}").status_code == 404
    history = client.get(f"/api/platform/v1/conversations/{conversation['conversation_id']}").get_json()['conversation']
    assert history['runs'][0]['requests'] == value['requests']
    assert history['runs'][0]['trigger_message_id'] == history['messages'][0]['message_id']
    assert 'never-expose' not in json.dumps(history)


def test_pending_terminal_unknown_and_projection_queries_are_batched(tmp_path, monkeypatch):
    app, repo, client, conversation, run = setup(tmp_path)
    import time
    for step in range(1, 21):
        repo.append_run_event(OWNER, run['run_id'], 'provider_request_started',
            {'step':step,'attempt':1,'request_index':1,'status':'running','started_at':time.time(),
             'model':'frozen-model','raw_body':'private body'}, now=time.time())
    active = client.get(f"/api/platform/v1/runs/{run['run_id']}").get_json()['requests']
    assert len(active) == 20 and all(x['elapsed_ms'] >= 0 for x in active)
    repo.set_run_state(OWNER, run['run_id'], 'unknown', now=time.time())
    statements = []
    connect = repo._connect
    def traced():
        conn = connect(); conn.set_trace_callback(statements.append); return conn
    monkeypatch.setattr(repo, '_connect', traced)
    history = repo.get_conversation(OWNER, conversation['conversation_id'])
    requests = history['runs'][0]['requests']
    assert all(x['status'] == 'unknown' and x.get('duration_ms') is None for x in requests)
    assert all('raw_body' not in x for x in requests)
    assert len([q for q in statements if 'FROM run_events e' in q]) == 1


@pytest.mark.parametrize("started_at", [
    pytest.param(_MISSING, id="missing"), None, "not-a-timestamp", True, [], 10**1000,
])
def test_active_request_with_invalid_start_time_does_not_break_run_projection(
    tmp_path, started_at,
):
    _, repo, client, _, run = setup(tmp_path)
    payload = {'attempt': 1, 'step': 1, 'request_index': 1, 'status': 'running'}
    if started_at is not _MISSING:
        payload['started_at'] = started_at
    repo.append_run_event(OWNER, run['run_id'], 'provider_request_started', payload, now=1.0)

    response = client.get(f"/api/platform/v1/runs/{run['run_id']}")

    assert response.status_code == 200
    request = response.get_json()['requests'][0]
    assert request['status'] == 'running'
    assert 'started_at' not in request
    assert 'elapsed_ms' not in request


def test_model_pricing_is_validated_and_frozen(tmp_path):
    app, repo, client, _, _ = setup(tmp_path)
    rates = {'currency':'USD','input':'1','output':'6','cache_read':'.1','cache_write':'1.25'}
    model = {'profile_id':'priced','provider':'openai_compatible','model':'price-model', 'provider_config':{'pricing':rates}}
    repo.upsert_model(OWNER, model)
    import pytest
    with pytest.raises(Exception, match='invalid_model'):
        repo.upsert_model(OWNER, {**model,'provider_config':{'pricing':{**rates,'input':'NaN'}}})
    # The same resolver that freezes endpoint/model also freezes pricing.
    conversation = client.post('/api/platform/v1/conversations', json={}).get_json()['conversation']
    run = client.post(f"/api/platform/v1/conversations/{conversation['conversation_id']}/turns",
        json={'text':'inspect','client_token':'priced-turn','overrides':{'model_profile_id':'priced'}}).get_json()['run']
    snapshot = repo.get_run(OWNER, run['run_id'])['config_snapshot']['provider_snapshot']['provider_config']['pricing']
    repo.upsert_model(OWNER, {**model,'provider_config':{'pricing':{**rates,'input':'20'}}})
    assert snapshot['input'] == '1'
    assert repo.get_run(OWNER, run['run_id'])['config_snapshot']['provider_snapshot']['provider_config']['pricing'] == snapshot
