"""Terminal runs freeze their request projection so run_events retention never
erases per-request model/status/duration from run history."""
import json
import sqlite3

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.request_metadata import (
    MAX_FROZEN_REQUEST_BYTES, MAX_FROZEN_REQUESTS, freeze_requests, thaw_requests, request_totals,
)
from hub.infrastructure.platform_db import PlatformRepository
from hub.infrastructure.platform_retention import DAY_S, PlatformRetention, RetentionPolicy
from hub.platform.providers.openai_compatible import OpenAICompatibleProvider, TransportResponse

OWNER = 'summary@example.test'
FUTURE = 50_000 * DAY_S  # far past any wall-clock updated_at


def setup(tmp_path):
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER, platform_enabled=True, platform_worker_enabled=True))
    repo = app.extensions['fleet']['platform_repository']
    repo.upsert_model(OWNER, {'profile_id': 'model', 'provider': 'openai_compatible', 'model': 'frozen-model',
        'secret_ref': 'env://FIXTURE', 'provider_config': {'endpoint': 'https://fixture.invalid'}})
    repo.upsert_workspace(OWNER, {'workspace_id': 'home', 'root_path': str(tmp_path / 'work')})
    repo.update_defaults(OWNER, {'model_profile_id': 'model', 'workspace_id': 'home'}, 0)
    client = app.test_client()
    conversation = client.post('/api/platform/v1/conversations', json={}).get_json()['conversation']
    run = client.post(f"/api/platform/v1/conversations/{conversation['conversation_id']}/turns",
        json={'text': 'inspect', 'client_token': 'one'}).get_json()['run']
    return app, repo, client, conversation, run


def _db(tmp_path):
    return next(tmp_path.rglob('platform.db'))


def _sql(tmp_path, query, params=()):
    conn = sqlite3.connect(_db(tmp_path))
    try:
        rows = conn.execute(query, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _request_event(step, status='succeeded', model='frozen-model'):
    return {'attempt': 1, 'step': step, 'request_index': 1, 'status': status,
            'model': model, 'started_at': 1.0, 'duration_ms': 25 * step}


def _run_worker(app):
    class Transport:
        def request(self, **kwargs):
            return TransportResponse(200, {}, json.dumps({'model': 'returned-model', 'id': 'reply-one',
                'choices': [{'message': {'content': 'done'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 47, 'completion_tokens': 14, 'cost': .000131}}).encode())
    worker = app.extensions['fleet']['services']['platform_worker']
    worker.provider_factory = lambda profile: OpenAICompatibleProvider(
        model=profile['model'], endpoint='http://fixture', api_key='never-expose', transport=Transport())
    assert worker.run_once(OWNER)['state'] == 'succeeded'


def test_finish_run_freezes_requests_that_survive_run_event_retention(tmp_path):
    app, repo, client, conversation, run = setup(tmp_path)
    _run_worker(app)
    run_url = f"/api/platform/v1/runs/{run['run_id']}"
    conversation_url = f"/api/platform/v1/conversations/{conversation['conversation_id']}"
    live_run = client.get(run_url).get_json()['requests']
    live_history = client.get(conversation_url).get_json()['conversation']['runs'][0]['requests']
    assert live_run and live_run[0]['model'] == 'returned-model'
    (summary,), = _sql(tmp_path, "SELECT request_summary FROM runs WHERE run_id=?", (run['run_id'],))
    assert thaw_requests(summary) == live_run
    assert 'never-expose' not in summary

    pruned = PlatformRetention(_db(tmp_path))(now=FUTURE)
    assert pruned['run_events'] > 0
    assert _sql(tmp_path, "SELECT COUNT(*) FROM run_events WHERE run_id=?", (run['run_id'],)) == [(0,)]
    assert client.get(run_url).get_json()['requests'] == live_run
    assert client.get(conversation_url).get_json()['conversation']['runs'][0]['requests'] == live_history
    assert repo.get_run(OWNER, run['run_id'])['requests'] == live_run
    assert repo.get_conversation(OWNER, conversation['conversation_id'])['runs'][0]['requests'] == live_history


def test_set_run_state_and_late_request_events_keep_summary_current(tmp_path):
    _, repo, _, conversation, run = setup(tmp_path)
    run_id = run['run_id']
    repo.append_run_event(OWNER, run_id, 'provider_request_started', _request_event(1, status='running'), now=2.0)
    assert _sql(tmp_path, "SELECT request_summary FROM runs WHERE run_id=?", (run_id,)) == [(None,)]
    repo.set_run_state(OWNER, run_id, 'failed', now=3.0)
    (summary,), = _sql(tmp_path, "SELECT request_summary FROM runs WHERE run_id=?", (run_id,))
    assert [r['status'] for r in thaw_requests(summary)] == ['unknown']

    repo.append_run_event(OWNER, run_id, 'provider_request_finished', _request_event(1), now=4.0)
    repo.append_run_event(OWNER, run_id, 'provider_request_finished', _request_event(2), now=5.0)
    live = repo.get_run(OWNER, run_id)['requests']
    (summary,), = _sql(tmp_path, "SELECT request_summary FROM runs WHERE run_id=?", (run_id,))
    assert thaw_requests(summary) == live
    assert [(r['request_id'], r['status'], r['duration_ms']) for r in live] == [
        ('1:1:1', 'succeeded', 25), ('1:2:1', 'succeeded', 50)]

    _sql(tmp_path, "DELETE FROM run_events WHERE run_id=?", (run_id,))
    assert repo.get_run(OWNER, run_id)['requests'] == live
    assert repo.get_conversation(OWNER, conversation['conversation_id'])['runs'][0]['requests'] == live

    repo.set_run_state(OWNER, run_id, 'running', now=6.0)
    assert _sql(tmp_path, "SELECT request_summary FROM runs WHERE run_id=?", (run_id,)) == [(None,)]
    assert repo.get_run(OWNER, run_id)['requests'] == []


def test_cancel_before_claim_freezes_summary(tmp_path):
    _, repo, _, _, run = setup(tmp_path)
    run_id = run['run_id']
    repo.append_run_event(OWNER, run_id, 'provider_request_finished', _request_event(1), now=2.0)
    repo.cancel_run(OWNER, run_id, now=3.0)
    claimed = repo.claim_run(OWNER, worker_id='worker-a', now=4.0)
    assert claimed['cancelled_before_claim'] is True
    live = repo.get_run(OWNER, run_id)['requests']
    (summary,), = _sql(tmp_path, "SELECT request_summary FROM runs WHERE run_id=?", (run_id,))
    assert live and thaw_requests(summary) == live


def test_frozen_summary_is_bounded_and_keeps_newest_requests():
    many = [{'request_id': f'1:{i}:1', 'status': 'succeeded'} for i in range(1, MAX_FROZEN_REQUESTS + 51)]
    frozen = json.loads(freeze_requests(many))
    assert len(frozen['requests']) == MAX_FROZEN_REQUESTS
    assert frozen['omitted'] == 50
    assert frozen['requests'][-1] == many[-1] and frozen['requests'][0] == many[50]

    big = [{'request_id': f'1:{i}:1', 'provider_error': 'x' * 4096} for i in range(1, 41)]
    encoded = freeze_requests(big)
    frozen = json.loads(encoded)
    assert len(encoded.encode('utf-8')) <= MAX_FROZEN_REQUEST_BYTES
    assert 0 < len(frozen['requests']) < len(big)
    assert frozen['omitted'] == len(big) - len(frozen['requests'])
    assert frozen['requests'][-1] == big[-1]
    assert thaw_requests(encoded) == frozen['requests']
    assert thaw_requests(json.dumps({'requests': []})) == []
    assert thaw_requests('not json') == [] and thaw_requests(None) == []


def test_long_run_totals_survive_detail_retention_and_restart(tmp_path):
    _, repo, client, conversation, run = setup(tmp_path)
    for step in range(1, 251):
        event = {**_request_event(step),
                 'usage': {'input_tokens': 100, 'output_tokens': 20,
                           'cache_read_tokens': 40, 'cache_write_tokens': 10,
                           'reasoning_tokens': 5, 'total_tokens': 120},
                 'cost': {'amount': '0.00000123', 'currency': 'USD', 'source': 'configured'}}
        repo.append_run_event(OWNER, run['run_id'], 'provider_request_finished', event, now=step)
    repo.set_run_state(OWNER, run['run_id'], 'succeeded', now=300)
    url = f"/api/platform/v1/runs/{run['run_id']}"
    before = client.get(url).get_json()
    totals = before['request_totals']
    assert totals['count'] == 250
    assert totals['usage']['input_tokens'] == 25000
    assert totals['display_usage']['input_tokens'] == 12500
    assert totals['display_usage']['output_tokens'] == 3750
    assert totals['cost']['amount'] == '0.00030750'
    assert totals['duration_ms'] == sum(25 * step for step in range(1, 251))
    assert 0 < len(before['requests']) <= MAX_FROZEN_REQUESTS
    assert before['requests_omitted'] == 250 - len(before['requests'])
    assert PlatformRetention(_db(tmp_path))(now=FUTURE)['run_events'] > 0
    restarted = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER, platform_enabled=True))
    after = restarted.test_client().get(url).get_json()
    assert after['request_totals'] == totals
    assert after['requests_omitted'] == before['requests_omitted']
    assert after['requests'] == before['requests']
    history = restarted.test_client().get(
        f"/api/platform/v1/conversations/{conversation['conversation_id']}").get_json()
    assert history['conversation']['runs'][0]['request_totals'] == totals


@pytest.mark.parametrize('remaining_events', [0, 1])
def test_legacy_pruned_totals_are_explicitly_partial(tmp_path, remaining_events):
    _, repo, client, _, run = setup(tmp_path)
    repo.set_run_state(OWNER, run['run_id'], 'succeeded', now=3)
    legacy = {'requests': [{**_request_event(4), 'request_id': '1:4:1',
                           'usage': {'input_tokens': 7}}], 'omitted': 3}
    if remaining_events:
        repo.append_run_event(OWNER, run['run_id'], 'provider_request_finished', legacy['requests'][0], now=4)
    _sql(tmp_path, 'UPDATE runs SET request_summary=? WHERE run_id=?',
         (json.dumps(legacy), run['run_id']))
    value = client.get(f"/api/platform/v1/runs/{run['run_id']}").get_json()
    assert value['requests_omitted'] == 3
    assert value['request_totals']['count'] == 4
    assert value['request_totals']['complete'] is False
    assert value['request_totals']['known']['input_tokens'] == 1
    repo.set_run_state(OWNER, run['run_id'], 'succeeded', now=5)
    assert client.get(f"/api/platform/v1/runs/{run['run_id']}").get_json()['request_totals']['count'] == 4


def test_full_totals_keep_missing_accounting_and_retry_outcomes_distinct():
    totals = request_totals([
        {'status': 'failed', 'model': 'selected'},
        {'status': 'succeeded', 'model': 'actual', 'duration_ms': 12,
         'usage': {'input_tokens': 100, 'output_tokens': 20, 'reasoning_tokens': 5},
         'cost': {'amount': '0', 'currency': 'USD', 'source': 'provider'}},
        {'status': 'unknown', 'model': 'actual', 'duration_ms': 13,
         'cost': {'amount': '1e999999999', 'currency': 'USD', 'source': 'provider'}}])
    assert totals['count'] == 3
    assert totals['states'] == {'failed': 1, 'succeeded': 1, 'unknown': 1}
    assert totals['known']['cache_read_tokens'] == 0
    assert totals['known']['input_tokens'] == 1
    assert totals['display_usage']['input_tokens'] == 100
    assert totals['display_usage']['output_tokens'] == 15
    assert totals['cost_known'] == 1 and totals['cost']['amount'] == '0'
    assert totals['duration_known'] == 2 and totals['duration_ms'] == 25
    assert totals['model_count'] == 2


def test_partial_event_pruning_never_replaces_complete_frozen_totals(tmp_path):
    _, repo, _, _, run = setup(tmp_path)
    for step in range(1, 5):
        repo.append_run_event(OWNER, run['run_id'], 'provider_request_finished', _request_event(step), now=step)
    repo.set_run_state(OWNER, run['run_id'], 'succeeded', now=5)
    before = repo.get_run(OWNER, run['run_id'])
    PlatformRetention(_db(tmp_path), RetentionPolicy(batch_size=1, max_batches=1))(now=FUTURE)
    assert _sql(tmp_path, 'SELECT count(*) FROM run_events WHERE run_id=?', (run['run_id'],)) == [(3,)]
    after = repo.get_run(OWNER, run['run_id'])
    assert after['request_totals'] == before['request_totals']
    assert after['requests'] == before['requests']


def test_retention_upgrades_legacy_summary_before_first_partial_delete(tmp_path):
    _, repo, _, _, run = setup(tmp_path)
    for step in range(1, 5):
        repo.append_run_event(OWNER, run['run_id'], 'provider_request_finished', _request_event(step), now=step)
    repo.set_run_state(OWNER, run['run_id'], 'succeeded', now=5)
    legacy = {'requests': [{**_request_event(4), 'request_id': '1:4:1'}], 'omitted': 3}
    _sql(tmp_path, 'UPDATE runs SET request_summary=? WHERE run_id=?', (json.dumps(legacy), run['run_id']))
    PlatformRetention(_db(tmp_path), RetentionPolicy(batch_size=1, max_batches=1))(now=FUTURE)
    after = repo.get_run(OWNER, run['run_id'])
    assert after['request_totals']['count'] == 4
    assert after['request_totals']['complete'] is True
    assert after['request_totals']['duration_ms'] == 250


def test_migration_adds_column_and_retention_backfills_pre_existing_terminal_runs(tmp_path):
    _, repo, _, conversation, run = setup(tmp_path)
    run_id = run['run_id']
    repo.append_run_event(OWNER, run_id, 'provider_request_finished', _request_event(1, model='legacy-model'), now=2.0)
    repo.set_run_state(OWNER, run_id, 'succeeded', now=3.0)
    expected = repo.get_run(OWNER, run_id)['requests']
    # Simulate a database written by a release without runs.request_summary.
    _sql(tmp_path, "ALTER TABLE runs DROP COLUMN request_summary")
    assert 'request_summary' not in {row[1] for row in _sql(tmp_path, "PRAGMA table_info(runs)")}

    migrated = PlatformRepository(_db(tmp_path))
    migrated.init()
    assert 'request_summary' in {row[1] for row in _sql(tmp_path, "PRAGMA table_info(runs)")}
    assert _sql(tmp_path, "SELECT request_summary FROM runs WHERE run_id=?", (run_id,)) == [(None,)]
    assert _sql(tmp_path, "SELECT value FROM meta WHERE key='schema_version'") == [(str(PlatformRepository.SCHEMA_VERSION),)]
    assert migrated.get_run(OWNER, run_id)['requests'] == expected

    assert PlatformRetention(_db(tmp_path))(now=FUTURE)['run_events'] > 0
    assert _sql(tmp_path, "SELECT COUNT(*) FROM run_events WHERE run_id=?", (run_id,)) == [(0,)]
    assert migrated.get_run(OWNER, run_id)['requests'] == expected
    assert migrated.get_conversation(OWNER, conversation['conversation_id'])['runs'][0]['requests'] == expected
    assert expected[0]['model'] == 'legacy-model'
