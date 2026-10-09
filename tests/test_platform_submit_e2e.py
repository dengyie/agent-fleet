"""Offline owner -> worker -> signed HTTP Node -> pinned form transport contract."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from hub.application.run_worker_service import LocalRunWorkerService
from tools.platform.browser_backend import BrowserBackendError
from tools.platform.browser_transport import BrowserTransportError
from tools.platform.journal import NodeJournal
from tools.platform.node_client import NodeClient
from tools.platform.node_executor import NodeToolExecutor
from tools.platform.providers.base import ModelResponse
from test_platform_node_http_e2e import FlaskTransport
from test_platform_submit_http import build_app, seed, OWNER
from test_platform_browser_transport import transport_factory, _Response
from support.browser import browser_backend, URL


class FormDriver:
    """Injected driver contract: resolve the current form and screen DOM control types."""
    def __init__(self, transport, scenario, dispatched, resume):
        self.transport = transport
        self.scenario = scenario
        self.dispatched = dispatched
        self.resume = resume
        self.posts = 0

    def open(self, url):
        return None

    def submit(self, selector):
        if self.scenario == 'missing_form' or selector != '#go':
            raise BrowserBackendError('backend_failed')
        if self.scenario == 'password_type':
            raise BrowserBackendError('sensitive_field_forbidden')
        if self.scenario == 'file_type':
            raise BrowserBackendError('invalid_arguments')
        fields = {'FIELD_MARKER': 'VALUE_MARKER'}
        if self.scenario == 'sensitive_name':
            fields = {'password': 'VALUE_MARKER'}
        url = 'https://one.example/PATH_MARKER'
        if self.scenario == 'off_origin':
            url = 'https://elsewhere.example/PATH_MARKER'
        try:
            self.transport.submit_form(url, fields)
        except BrowserTransportError as exc:
            raise BrowserBackendError(exc.code) from exc
        self.posts += 1
        if self.scenario == 'revoked_in_flight':
            self.dispatched.set()
            if not self.resume.wait(timeout=5):
                raise RuntimeError('submit fixture release timed out')
        if self.scenario == 'interrupted':
            raise RuntimeError('PRIVATE_DRIVER_MARKER')
        if self.scenario == 'overflow':
            return {'state': 'x' * (16 * 1024 + 1)}
        return {'state': 'submitted'}


@pytest.mark.parametrize('scenario,expected,posts', [
    ('success', 'succeeded', 1), ('repeat', 'failed', 1),
    ('no_approval', 'failed', 0), ('revoked', 'failed', 0),
    ('selector_mismatch', 'failed', 0), ('missing_form', 'failed', 0),
    ('password_type', 'failed', 0), ('sensitive_name', 'failed', 0),
    ('file_type', 'failed', 0),
    ('off_origin', 'failed', 0), ('redirect', 'failed', 1),
    ('revoked_in_flight', 'succeeded', 1),
    ('interrupted', 'unknown', 1), ('overflow', 'failed', 1),
])
def test_submit_end_to_end_contract(tmp_path, transport_factory, caplog, scenario, expected, posts):
    app, public = build_app(tmp_path)
    platform, approvals = seed(app, tmp_path)
    platform.upsert_node(OWNER, {'node_id': 'node-a', 'capabilities': {'browser.session': True, 'browser.submit': True}})
    platform.provision_node_credential(OWNER, 'node-a', secret='n' * 40)
    transport = transport_factory([_Response(302, [('Location', 'https://one.example/next')], b'RESPONSE_MARKER')
                                   if scenario == 'redirect' else _Response(200, (), b'RESPONSE_MARKER')])
    dispatched = Event()
    resume = Event()
    driver = FormDriver(transport, scenario, dispatched, resume)
    backend = browser_backend(lambda: driver, submit_enabled=True)
    sid = backend.execute('browser.open', {'url': URL}, run_id='run-a')['session_id']
    approvals.create_session(OWNER, workspace_id='workspace-a', run_id='run-a', node_id='node-a', profile_id='profile-a', session_id=sid)
    approval = None
    if scenario != 'no_approval':
        response = app.test_client().post('/api/platform/v1/runs/run-a/browser-approvals',
                                         json={'session_id': sid, 'selector': '#go'})
        assert response.status_code == 200
        approval = response.get_json()['approval']
        if scenario == 'revoked':
            response = app.test_client().delete('/api/platform/v1/runs/run-a/browser-approvals/' + approval['approval_id'])
            assert response.status_code == 200
    journal = NodeJournal(tmp_path / 'node.db')
    journal.init()
    http = FlaskTransport(app.test_client())
    node = NodeClient(journal, executor=NodeToolExecutor(None, browser_backend=backend, browser_enabled=True),
                      node_id='node-a', credential='node-a:' + 'n' * 40,
                      hub_url='https://hub.invalid', transport=http, public_key=public, require_signature=True)
    delivery = app.extensions['fleet']['services']['platform_delivery']
    class Bridge:
        def enqueue_submit(self, command, **kwargs):
            return delivery.enqueue_submit(command, **kwargs)
        def wait_for_receipt(self, command_id, **kwargs):
            assert node.poll_once()['ok'] is True
            return delivery.wait_for_receipt(command_id, **kwargs)
    transcripts = []
    definitions = []
    class Provider:
        def complete(self, messages, tools, *, request_observer=None):
            definitions.append(tools)
            transcripts.append(messages)
            attempts = len(transcripts)
            if attempts == 1 or (scenario == 'repeat' and attempts == 2):
                return ModelResponse(kind='tool_call', tool='browser.submit',
                    arguments={'session_id': sid, 'selector': '#other' if scenario == 'selector_mismatch' else '#go'})
            return ModelResponse(kind='final', text='done')
    worker = LocalRunWorkerService(platform, app.extensions['fleet']['services']['run_events'],
        worker_id='submit-worker', provider_factory=lambda profile: Provider(),
        remote_execution_enabled=True, remote_delivery=Bridge(), browser_enabled=True,
        browser_network_enabled=True, browser_submit_enabled=True, submit_approvals=approvals)
    if scenario == 'revoked_in_flight':
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(worker.run_once, OWNER)
            try:
                assert dispatched.wait(timeout=5)
                response = app.test_client().delete(
                    '/api/platform/v1/runs/run-a/browser-approvals/' + approval['approval_id'])
                assert response.status_code == 200
                assert response.get_json()['approval']['state'] == 'consumed'
            finally:
                resume.set()
            result = pending.result(timeout=5)
    else:
        result = worker.run_once(OWNER)
    assert result['run_id'] == 'run-a'
    assert result['state'] == expected
    assert any(item['name'] == 'browser.submit' for item in definitions[0])
    assert len(transport.test_requests) == posts
    command = delivery.repository.get('run-a:step:1')
    events = app.extensions['fleet']['services']['run_events'].list(OWNER, 'run-a')['events']
    metadata = {'events': events, 'journal': journal.get('run-a:step:1'),
                'command_result': command['result'] if command else None,
                'wire_receipts': http.responses,
                'http_responses': http.responses,
                'transcripts': transcripts,
                'logs': caplog.text}
    with sqlite3.connect(approvals.db_path) as connection:
        connection.row_factory = sqlite3.Row
        metadata['artifact_ticket_registry'] = [
            dict(row) for row in connection.execute(
                'SELECT ticket_id, owner_id, workspace_id, run_id, node_id, command_id, '
                'content_type, state, artifact_id, sha256, size FROM browser_artifact_tickets '
                'ORDER BY ticket_id'
            )
        ]
    artifact_store = app.extensions['fleet']['services'].get('platform_artifacts')
    metadata['artifact_registry'] = (
        artifact_store.list(OWNER, 'workspace-a') if artifact_store is not None else []
    )
    encoded = json.dumps(metadata, default=str)
    for marker in ('FIELD_MARKER', 'VALUE_MARKER', 'PATH_MARKER', 'RESPONSE_MARKER', 'PRIVATE_DRIVER_MARKER'):
        assert marker not in encoded
    if command is not None:
        assert command['arguments']['approval_id'] == approval['approval_id']
        assert approval['approval_id'] in json.dumps(command['result'])
        assert approvals.get_submit_approval(OWNER, approval['approval_id'])['state'] == 'consumed'
        assert any(event['payload'].get('approval_id') == approval['approval_id'] for event in events)
    if scenario == 'repeat':
        assert delivery.repository.get('run-a:step:2') is None
        assert any(event['payload'].get('error_code') == 'approval_consumed' for event in events)
    if scenario == 'interrupted':
        assert journal.get('run-a:step:1')['state'] == 'unknown'
