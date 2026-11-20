"""Bounded waits for real output from the original remote Agent session."""

import os
import threading
from types import SimpleNamespace

import pytest

from hub.application import agent_session_service as adapter
from hub.application.errors import ApplicationError
from hub.application.session_service import SessionService
from hub.infrastructure.session_repository import SessionRepository
from hub.infrastructure.transcript_repository import TranscriptRepository
from hub.platform.session_tools import SESSION_TOOL_DEFINITIONS
from hub.platform.providers.base import ModelResponse
from tests.test_assistant_vm_sessions import app as vm_app, execute
from tests.test_platform_run_worker import OWNER, _catalog, _queued
from tests.test_session_interactions import event


@pytest.fixture
def app(tmp_path):
    repository = SessionRepository(tmp_path / 'sessions.db')
    repository.init()
    transcripts = TranscriptRepository(tmp_path / 'transcripts', key=bytes(range(32)))
    transcripts.init()
    sessions = SessionService(repository, transcripts)
    sessions.ingest_events([
        event(1, 'session_start', {'agent_family': 'claude', 'adapter': 'hooks'}),
        event(2, 'assistant_message', {'text': 'remote output', 'is_complete': True}),
    ])
    return SimpleNamespace(extensions={'fleet': {'services': {
        'sessions': sessions, 'agent_sessions': adapter.AgentSessionService(sessions),
    }}})


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=100.0, sleeps=[])

    def sleep(seconds):
        state.sleeps.append(seconds)
        state.now += seconds

    monkeypatch.setattr(adapter, 'time', SimpleNamespace(
        monotonic=lambda: state.now, sleep=sleep), raising=False)
    return state


def test_wait_returns_output_ingested_after_the_original_cursor(app, monkeypatch):
    sessions = app.extensions['fleet']['services']['sessions']
    original_read = sessions.session_events
    empty_read = threading.Event()
    stopping = threading.Event()
    failures = []

    def read(*args, **kwargs):
        page = original_read(*args, **kwargs)
        if not page['events']:
            empty_read.set()
        return page

    def publish():
        if empty_read.wait(2) and not stopping.is_set():
            try:
                sessions.ingest_events([event(3, 'assistant_message', {'text': 'late output'})])
            except Exception as exc:
                failures.append(exc)

    monkeypatch.setattr(sessions, 'session_events', read)
    publisher = threading.Thread(target=publish, daemon=True)
    publisher.start()
    try:
        result = execute(app, 'session.read_events', {
            'session_id': 's1', 'after_cursor': '2', 'wait_seconds': 1,
        })
    finally:
        stopping.set()
        empty_read.set()
        publisher.join(2)
    assert not failures and not publisher.is_alive()
    assert [row['payload']['text'] for row in result['events']] == ['late output']
    assert result['next_cursor'] == '3' and result['has_more'] is False


def test_empty_wait_expires_without_advancing_the_cursor(app, clock):
    result = execute(app, 'session.read_events', {
        'session_id': 's1', 'after_cursor': '2', 'wait_seconds': 1,
    })
    assert result == {'events': [], 'next_cursor': '2', 'has_more': False}
    assert clock.now == pytest.approx(101.0)
    assert sum(clock.sleeps) == pytest.approx(1.0)


def test_existing_output_and_pagination_do_not_wait(app, clock):
    result = execute(app, 'session.read_events', {
        'session_id': 's1', 'limit': 1, 'wait_seconds': 30,
    })
    assert len(result['events']) == 1
    assert result['next_cursor'] == '1' and result['has_more'] is True
    assert clock.sleeps == []


def test_default_read_is_immediate_with_no_new_output(app, clock):
    result = execute(app, 'session.read_events', {'session_id': 's1', 'after_cursor': '2'})
    assert result == {'events': [], 'next_cursor': '2', 'has_more': False}
    assert clock.sleeps == []


@pytest.mark.parametrize('wait_seconds', [-1, 31, True, 0.5, '1', None])
def test_wait_rejects_ambiguous_or_unbounded_values(app, wait_seconds):
    with pytest.raises(ApplicationError, match='invalid_arguments'):
        execute(app, 'session.read_events', {'session_id': 's1', 'wait_seconds': wait_seconds})


def test_wait_rechecks_administrator_authorization(app, clock, monkeypatch):
    service = app.extensions['fleet']['services']['agent_sessions']
    active = [True]
    service.authorize = lambda owner: active[0]

    def revoke(seconds):
        clock.now += seconds
        active[0] = False

    monkeypatch.setattr(adapter.time, 'sleep', revoke)
    with pytest.raises(ApplicationError, match='forbidden'):
        execute(app, 'session.read_events', {
            'session_id': 's1', 'after_cursor': '2', 'wait_seconds': 30,
        })


def test_authorization_is_rechecked_before_returning_waited_output(app, monkeypatch):
    services = app.extensions['fleet']['services']
    service = services['agent_sessions']
    active = [True]
    service.authorize = lambda owner: active[0]
    original_read = services['sessions'].session_events

    def revoke_after_read(*args, **kwargs):
        page = original_read(*args, **kwargs)
        active[0] = False
        return page

    monkeypatch.setattr(services['sessions'], 'session_events', revoke_after_read)
    with pytest.raises(ApplicationError, match='forbidden'):
        execute(app, 'session.read_events', {'session_id': 's1'})


def test_tool_contract_exposes_bounded_wait_and_keeps_it_optional():
    definition = next(row for row in SESSION_TOOL_DEFINITIONS if row['name'] == 'session.read_events')
    schema = definition['parameters']
    wait = schema['properties']['wait_seconds']
    assert wait['type'] == 'integer' and wait['minimum'] == 0 and wait['maximum'] == 30
    assert 'wait_seconds' not in schema['required']
    assert 'wait_seconds=30' in definition['description']


@pytest.mark.skipif(os.name != 'posix', reason='Hub Worker integration requires POSIX private storage')
def test_vm_main_assistant_waits_at_hub_and_receives_the_original_agent_result(vm_app, tmp_path, clock, monkeypatch):
    app = vm_app
    repo = _catalog(app, tmp_path)
    repo.upsert_node(OWNER, {'node_id': 'other-workspace-node', 'capabilities': {'workspace.list': True}})
    repo.update_defaults(OWNER, {'model_profile_id': 'local', 'workspace_id': 'home',
                                'execution_node_id': 'other-workspace-node'}, 1)
    run = _queued(app)
    services = app.extensions['fleet']['services']
    calls = []

    def publish_output(seconds):
        clock.now += seconds
        services['sessions'].ingest_events([
            event(3, 'tool_result', {'result': '# Agent Fleet'}),
            event(4, 'assistant_message', {'text': 'late Agent answer'}),
        ])

    monkeypatch.setattr(adapter.time, 'sleep', publish_output)

    class Provider:
        def complete(self, messages, tools, **kwargs):
            calls.append(messages)
            definition = next(tool for tool in tools if tool['name'] == 'session.read_events')
            assert definition['parameters']['properties']['wait_seconds']['maximum'] == 30
            if len(calls) == 1:
                return ModelResponse(kind='tool_call', tool='session.send_message',
                                     arguments={'session_id': 's1', 'text': 'read the project'})
            if len(calls) == 2:
                assert messages[-1]['result']['status'] == 'pending'
                return ModelResponse(kind='tool_call', tool='session.read_events', arguments={
                    'session_id': 's1', 'after_cursor': '2', 'wait_seconds': 1,
                })
            result = messages[-1]['result']
            assert result['next_cursor'] == '4'
            assert result['events'][0]['payload']['result'] == '# Agent Fleet'
            return ModelResponse(kind='final', text=result['events'][1]['payload']['text'])

    worker = services['platform_worker']
    worker.remote_execution_enabled = True
    worker.remote_delivery = object()  # A workspace-node dispatch would fail.
    worker.provider_factory = lambda _: Provider()
    result = worker.run_once(OWNER)
    assert result['state'] == 'succeeded' and result['result_text'] == 'late Agent answer'
    assert len(calls) == 3
    assert len(services['supervisor'].poll('m1')) == 1
    assert repo.get_run(OWNER, run['run_id'])['config_snapshot']['execution_node_id'] == 'other-workspace-node'
