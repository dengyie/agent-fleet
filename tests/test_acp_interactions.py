"""Native permission answers retain question idempotency and use a typed command."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import sqlite3

import pytest

from fleet_core import control
from hub.bootstrap import create_app
from hub.config import FleetConfig
from session_schema import validate_event
from tests.test_session_interactions import event

SID = 'runner_acp_permission'


def native_question():
    return {'request_id': 'request_1', 'question_type': 'single',
            'prompt': 'Allow this tool?', 'options': [
                {'id': 'option_1', 'label': 'Allow once'},
                {'id': 'option_2', 'label': 'Reject once'}],
            'allow_custom': False, 'delivery': 'native_permission'}


def native_event(sequence, kind, payload):
    row = event(sequence, kind, payload)
    row.update(session_id=SID, source='structured_stream')
    return row


@pytest.fixture
def native_permissions(tmp_path):
    if os.name == 'nt':
        pytest.skip('Hub signed-command store requires POSIX ownership; node delivery tests run on Windows')
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator='owner@example.test',
        supervisor_enabled=True, supervisor_signing_raw=bytes(range(32)),
        session_repositories_enabled=True, adoption_repositories_enabled=True,
        append_user_turn_enabled=True))
    services = app.extensions['fleet']['services']
    services['sessions'].ingest_events([
        native_event(1, 'session_start', {'agent_family': 'agy', 'adapter': 'structured_stream',
            'runtime_protocol': 'acp', 'interactive_questions': True}),
        native_event(2, 'session_metadata', {'runtime_protocol': 'acp'}),
        native_event(3, 'interaction_request', native_question()),
        native_event(4, 'agent_state_changed', {'state': 'waiting_for_approval',
            'source': 'structured_stream', 'confidence': 'high', 'reason': 'approval_required'})])
    return app, services


def answer(app, **change):
    data = {'request_id': 'request_1', 'client_token': 'native-answer-once', 'selected': 'option_1'}
    data.update(change)
    with app.test_client() as client:
        return client.post('/api/sessions/' + SID + '/messages', json=data,
            headers={'Cf-Access-Authenticated-User-Email': 'owner@example.test'})


def commands(services):
    with sqlite3.connect(services['supervisor']._command_repository.db_path) as conn:
        return [json.loads(row[0]) for row in conn.execute('SELECT wire_json FROM supervisor_commands')]


def test_native_answer_is_one_signed_native_command_after_response_loss(native_permissions):
    app, services = native_permissions
    accepted = answer(app)
    assert accepted.status_code == 202, accepted.get_json()
    # Simulate the browser losing the successful response and reloading.
    recovered = answer(app)
    assert recovered.status_code == 202
    assert recovered.get_json() == accepted.get_json()
    rows = commands(services)
    assert len(rows) == 1
    assert rows[0]['action'] == 'answer_native_request'
    assert rows[0]['payload'] == {'request_id': 'request_1', 'selected': 'option_1'}
    assert control.verify_payload(bytes(services['supervisor'].public_key()),
        control.payload_bytes(rows[0]), rows[0]['signature'])
    assert 'text' not in rows[0]['payload']


def test_concurrent_native_answer_produces_one_command(native_permissions):
    app, services = native_permissions
    with ThreadPoolExecutor(max_workers=6) as pool:
        replies = list(pool.map(lambda _: answer(app), range(12)))
    assert all(reply.status_code in {202, 409} for reply in replies)
    assert all(reply.status_code == 202 or reply.get_json()['error'] == 'interaction_pending'
               for reply in replies)
    assert len(commands(services)) == 1
    assert answer(app).status_code == 202


@pytest.mark.parametrize('change', [
    {'selected': 'outside'}, {'selected': ['option_1', 'option_2']},
    {'text': 'silently approve'}, {'request_id': 'other'},
])
def test_native_answer_invalid_option_or_request_never_enqueues(native_permissions, change):
    app, services = native_permissions
    response = answer(app, **change)
    assert response.status_code in {400, 409}
    assert commands(services) == []
    assert 'Allow this tool?' not in response.get_data(as_text=True)


def test_native_answer_cannot_be_reused_for_changed_selection(native_permissions):
    app, services = native_permissions
    assert answer(app).status_code == 202
    changed = answer(app, selected='option_2')
    assert changed.status_code == 409
    assert changed.get_json()['error'] == 'interaction_answered'
    assert len(commands(services)) == 1


def test_native_answer_closed_and_disabled_are_rejected(native_permissions):
    app, services = native_permissions
    services['sessions'].ingest_events([native_event(5, 'session_close', {'reason': 'done', 'status': 'closed'})])
    result = answer(app)
    assert result.status_code == 409
    assert commands(services) == []


def test_native_answer_without_control_service_is_bounded(native_permissions):
    app, services = native_permissions
    services['sessions'].supervisor = None
    response = answer(app)
    assert response.status_code == 503
    assert response.get_json()['error'] == 'control_unavailable'
    assert commands(services) == []


def test_native_answer_requires_live_acp_evidence(native_permissions):
    app, services = native_permissions
    services['sessions'].session_repo.update_session_metadata(SID, 5,
        {'runtime_protocol': 'cli_json'})
    response = answer(app)
    assert response.status_code == 409
    assert response.get_json()['error'] == 'session_action_unavailable'
    assert commands(services) == []


def test_native_answer_cannot_bypass_question_reservation_through_control(native_permissions):
    app, services = native_permissions
    with app.test_client() as client:
        response = client.post('/api/sessions/' + SID + '/control', json={
            'action': 'answer_native_request',
            'payload': {'request_id': 'request_1', 'selected': 'option_1'}})
    assert response.status_code == 409
    assert response.get_json()['error'] == 'session_action_unavailable'
    assert commands(services) == []


@pytest.mark.parametrize('payload', [
    {'request_id': '../escape', 'selected': 'option_1'},
    {'request_id': 'request_1', 'selected': ['option_1', 'option_2']},
    {'request_id': 'request_1', 'selected': 'option_1', 'cwd': '/tmp'},
    {'request_id': 'request_1', 'selected': True},
])
def test_signed_native_answer_exact_bounded_payload(payload):
    with pytest.raises(ValueError, match='invalid_payload'):
        control.validated_control_payload('answer_native_request', payload)


def test_native_delivery_marker_is_validated():
    assert validate_event(native_event(1, 'interaction_request', native_question()))['payload']['delivery'] == 'native_permission'
    for bad in ['append', None, [], {}]:
        row = native_question()
        row['delivery'] = bad
        with pytest.raises(ValueError):
            validate_event(native_event(1, 'interaction_request', row))


def test_native_answer_operator_bound_and_feature_gate(native_permissions):
    app, services = native_permissions
    assert answer(app).status_code == 202
    with app.test_client() as client:
        response = client.post('/api/sessions/' + SID + '/messages', json={
            'request_id': 'request_1', 'client_token': 'native-answer-once',
            'selected': 'option_1'}, headers={
                'Cf-Access-Authenticated-User-Email': 'other@example.test'})
    assert response.status_code == 409
    assert response.get_json()['error'] == 'interaction_answered'
    assert len(commands(services)) == 1


def test_native_permission_sqlite_failure_is_bounded(native_permissions, monkeypatch):
    app, services = native_permissions

    def broken(*_):
        raise sqlite3.OperationalError('secret /database/private.sqlite')

    monkeypatch.setattr(services['sessions'].session_repo, 'reserve_interaction', broken)
    response = answer(app)
    assert response.status_code == 503
    assert response.get_json()['error'] == 'interaction_unavailable'
    assert 'private.sqlite' not in response.get_data(as_text=True)
    assert commands(services) == []


@pytest.mark.skipif(os.name == 'nt', reason='Hub signed-command store requires POSIX ownership')
def test_native_answer_action_gate_and_precedence(tmp_path):
    from hub.application.supervisor_service import SupervisorService, SupervisorServiceError
    private, _ = control.generate_ed25519_keypair()
    payload = {'request_id': 'request_1', 'selected': 'option_1'}
    disabled = SupervisorService(signing_key=private)
    assert 'answer_native_request' not in disabled.available_actions()
    with pytest.raises(SupervisorServiceError, match='feature_disabled'):
        disabled.enqueue('m1', SID, None, 'answer_native_request', 'operator_requested', payload=payload)
    active = SupervisorService(signing_key=private, append_user_turn_enabled=True)
    original = active.enqueue('m1', SID, None, 'answer_native_request', 'operator_requested', payload=payload)
    active.enqueue('m1', SID, None, 'quarantine_session', 'operator_requested')
    assert active.receipt_status(original['command_id'])['status'] == 'expired'
    with pytest.raises(SupervisorServiceError, match='resume_superseded'):
        active.enqueue('m1', SID, None, 'answer_native_request', 'operator_requested', payload=payload)


def test_signed_native_answer_dispatch_is_not_a_continuation_and_replays_receipt(tmp_path):
    from tests.test_supervisor_control import _Pipe, _command_payload, _sign
    from tools.supervisor.control_client import ControlClient, NonceStore
    from types import SimpleNamespace
    private, public = control.generate_ed25519_keypair()

    class NativeSupervisor:
        calls = []

        def get(self, _):
            return SimpleNamespace(state='running', attempt_id=None, capability_manifest={})

        def answer_native_request(self, session, request, selected):
            self.calls.append((session, request, selected))
            return 'answered'

        def append_user_turn(self, *_):
            raise AssertionError('native answer must not create an appended turn')

    native = NativeSupervisor()
    body = _command_payload(machine='m1', session=SID, action='answer_native_request',
        nonce='native-permission-once', command_id='cmd_native_permission')
    body['payload'] = {'request_id': 'request_1', 'selected': 'option_1'}
    pipe = _Pipe([_sign(body, private)])
    client = ControlClient(hub_url='https://fixture.invalid', machine_id='m1',
        credential='fixture', hub_public_key=public, public_supervisor=native,
        transport=pipe, nonce_store=NonceStore(path=tmp_path / 'journal'))
    assert client.poll_once()['ok']
    assert client.poll_once()['ok']
    assert native.calls == [(SID, 'request_1', 'option_1')]
    assert all(row['status'] == 'succeeded' and row['reason'] == 'answered' for row in pipe.uploads)


@pytest.mark.parametrize('outcome,status', [
    ('execution_unknown', 'failed'), ('interaction_expired', 'rejected'),
    ('interaction_unavailable', 'rejected'), ('arbitrary secret failure', 'rejected')])
def test_native_delivery_receipt_reflects_actual_worker_outcome(tmp_path, outcome, status):
    from tools.supervisor.control_client import ControlClient
    from types import SimpleNamespace
    native = SimpleNamespace(answer_native_request=lambda *_: outcome)
    _, public = control.generate_ed25519_keypair()
    client = ControlClient(hub_url='https://fixture.invalid', machine_id='m1',
        credential='fixture', hub_public_key=public, public_supervisor=native)
    actual, reason = client._execute_payload('answer_native_request', 'cmd_1', SID,
        {'request_id': 'request_1', 'selected': 'option_1'})
    assert actual == status
    assert 'secret' not in reason


def test_signed_native_answer_reaches_current_owned_mailbox_once(tmp_path):
    import sys
    import threading
    from tools.providers.acp_mailbox import AcpMailbox
    from tools.supervisor.supervisor import Supervisor
    from tools.supervisor.control_client import ControlClient, NonceStore
    from tests.test_supervisor_control import _Pipe, _command_payload, _sign

    supervisor = Supervisor(tmp_path / 'manifests', 'm1')
    manifest = supervisor.launch(agent='agy', command=[sys.executable, '-c',
        'import time;time.sleep(15)'], cwd=str(tmp_path), env_allowlist={}, session_id=SID)
    root = (tmp_path / 'manifests' / 'acp' / SID).resolve()
    supervisor._entries[SID].resume_options = ['--control-dir', str(root)]
    AcpMailbox.begin(root, session_id=SID, pid=supervisor._entries[SID].handle.proc.pid)
    assert AcpMailbox.publish(root, 'request_1', [{'id': 'option_1', 'label': 'Allow once'}])
    delivered = []

    def worker():
        selected = AcpMailbox.wait(root, 'request_1', timeout=5)
        if selected is not None:
            # This ACK represents a successful native response write, which
            # the real worker performs before acknowledging the mailbox.
            delivered.append(selected)
            AcpMailbox.ack(root, 'request_1')

    thread = threading.Thread(target=worker)
    thread.start()
    private, public = control.generate_ed25519_keypair()
    payload = _command_payload(machine='m1', session=SID, action='answer_native_request',
        nonce='owned-native-once', command_id='cmd_owned_permission')
    payload['payload'] = {'request_id': 'request_1', 'selected': 'option_1'}
    transport = _Pipe([_sign(payload, private)])
    client = ControlClient(hub_url='https://fixture.invalid', machine_id='m1', credential='fixture',
        hub_public_key=public, public_supervisor=supervisor, transport=transport,
        nonce_store=NonceStore(path=tmp_path / 'journal'))
    try:
        assert client.poll_once()['ok']
        assert client.poll_once()['ok']
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert delivered == ['option_1']
        assert all(item['status'] == 'succeeded' for item in transport.uploads)
        assert supervisor.get(manifest.session_id).state == 'running'
    finally:
        supervisor.terminate_session(SID, grace_s=.1)
        thread.join(timeout=5)


def test_native_mailbox_refuses_changed_root_before_delivery(tmp_path):
    from tests.test_supervisor_control import _FakeOps
    from tools.supervisor.supervisor import Supervisor
    supervisor = Supervisor(tmp_path / 'manifests', 'm1', ops=_FakeOps())
    supervisor.launch(agent='agy', command=['fixture'], cwd=str(tmp_path), env_allowlist={}, session_id=SID)
    supervisor._entries[SID].resume_options = ['--control-dir', str(tmp_path / 'sibling')]
    assert supervisor.answer_native_request(SID, 'request_1', 'option_1') == 'interaction_unavailable'
    assert not (tmp_path / 'sibling').exists()


def test_native_mailbox_request_expires_without_creating_new_turn(tmp_path):
    import sys
    from tools.providers.acp_mailbox import AcpMailbox
    from tools.supervisor.supervisor import Supervisor
    supervisor = Supervisor(tmp_path / 'manifests', 'm1')
    supervisor.launch(agent='agy', command=[sys.executable, '-c', 'import time;time.sleep(15)'],
        cwd=str(tmp_path), env_allowlist={}, session_id=SID)
    root = (tmp_path / 'manifests' / 'acp' / SID).resolve()
    supervisor._entries[SID].resume_options = ['--control-dir', str(root)]
    AcpMailbox.begin(root, session_id=SID, pid=supervisor._entries[SID].handle.proc.pid)
    try:
        assert supervisor.answer_native_request(SID, 'request_1', 'option_1') == 'interaction_expired'
        assert supervisor.get(SID).state == 'running'
    finally:
        supervisor.terminate_session(SID, grace_s=.1)


def test_native_mailbox_owner_must_match_current_supervised_worker(tmp_path):
    import sys
    from tools.providers.acp_mailbox import AcpMailbox
    from tools.supervisor.supervisor import Supervisor
    supervisor = Supervisor(tmp_path / 'manifests', 'm1')
    supervisor.launch(agent='agy', command=[sys.executable, '-c', 'import time;time.sleep(15)'],
        cwd=str(tmp_path), env_allowlist={}, session_id=SID)
    root = (tmp_path / 'manifests' / 'acp' / SID).resolve()
    supervisor._entries[SID].resume_options = ['--control-dir', str(root)]
    # A live process at the right path is insufficient: this owner belongs
    # to the test process, not the currently supervised worker.
    AcpMailbox.begin(root, session_id=SID)
    assert AcpMailbox.publish(root, 'request_1', [{'id': 'option_1', 'label': 'Allow once'}])
    try:
        assert supervisor.answer_native_request(SID, 'request_1', 'option_1') == 'interaction_unavailable'
        assert not (root / 'answer.json').exists()
        assert AcpMailbox._read(root / 'pending.json')['state'] == 'pending'
    finally:
        supervisor.terminate_session(SID, grace_s=.1)


@pytest.mark.parametrize('managed,family,protocol,enabled', [
    (True, 'agy', 'acp', True), (False, 'agy', 'acp', False),
    (True, 'agy', 'cli_json', False), (True, 'codex', 'acp', False),
])
def test_capture_only_negotiated_owned_acp_enables_native_questions(tmp_path, managed, family, protocol, enabled):
    from tools.session.bridge import SessionBridge
    bridge = SessionBridge({'agent': family, 'structured_stream': True}, {
        'agent_family': family, 'session_id': SID, 'machine_id': 'm1',
        'managed': managed, 'process_group_id': 'grp_fixture' if managed else None,
        'spool_root': tmp_path / 'spool', 'spool_key': bytes(range(32))})
    try:
        bridge.start()
        request = {'type': 'interaction_request', **native_question()}
        assert bridge.ingest_one(request) is False
        bridge.ingest_one({'type': 'session_metadata', 'runtime_protocol': protocol,
                           'interactive_questions': True})
        assert bridge.ingest_one(request) is enabled
        if enabled:
            assert bridge.ingest_one({'type': 'interaction_response',
                'request_id': 'request_1', 'selected': 'option_1'})
            bridge.ingest_one({'type': 'agent_state_changed', 'state': 'running',
                'source': 'structured_stream', 'confidence': 'high', 'reason': 'user_prompt_submitted'})
        rows = bridge.spool.read_after(0, 100, 1 << 20)
        assert rows[0]['payload']['interactive_questions'] is False
        assert len([row for row in rows if row['kind'] == 'interaction_request']) == int(enabled)
        assert len([row for row in rows if row['kind'] == 'interaction_response']) == int(enabled)
        if enabled:
            states = [row for row in rows if row['kind'] == 'agent_state_changed']
            assert len(states) == 1
            assert states[0]['payload']['confidence'] == 'high'
            bridge.ingest_one({'type': 'session_metadata', 'runtime_protocol': 'cli_json'})
            assert bridge.ingest_one(request) is False
    finally:
        bridge.close()


@pytest.mark.skipif(os.name == 'nt', reason='Hub signed-command store requires POSIX ownership')
def test_real_acp_permission_http_capture_signed_answer_and_ack(tmp_path):
    import threading
    from tests.test_agy_acp import worker
    from tests.test_review_node_sync import _cfg
    from tools.providers.acp_mailbox import AcpMailbox
    from tools.session.runner_capture import RunnerCapture, session_prefix
    from tools.supervisor.control_client import ControlClient, NonceStore
    from tools.supervisor.supervisor import Supervisor

    cfg = _cfg(tmp_path)
    cfg.supervisor_manifest_dir = tmp_path / 'manifests'
    app = create_app(FleetConfig.from_root(tmp_path / 'hub', dev_operator='owner@example.test',
        supervisor_enabled=True, supervisor_signing_raw=bytes(range(32)),
        supervisor_credentials={cfg.machine: 'fixture-supervisor'},
        runner_credentials={cfg.machine: cfg.credential}, session_repositories_enabled=True,
        append_user_turn_enabled=True))

    def send(_cfg, path, body):
        with app.test_client() as client:
            response = client.post(path, json=body,
                headers={'X-Runner-Credential': cfg.machine + ':' + cfg.credential})
        assert response.status_code == 200, response.get_json()
        return response.status_code, response.get_json()

    argv, trace = worker(tmp_path, 'permission')
    session = session_prefix(cfg.machine) + 'permission-attempt'
    supervisor = Supervisor(cfg.supervisor_manifest_dir, cfg.machine)
    manifest = supervisor.launch(agent='agy', command=argv, cwd=str(tmp_path), env_allowlist={},
        session_id=session, attempt_id='permission-attempt')
    capture = RunnerCapture(cfg, {'attempt_id': 'permission-attempt', 'agent_type': 'agy',
        'task_id': 't-permission', 'instruction': 'Run fixture tests'}, send,
        process_group_id=manifest.process_group_id)
    request_ready, answered = threading.Event(), threading.Event()
    failures, results, rows = [], [], []

    def on_line(line):
        rows.append(json.loads(line))
        assert capture.line(line, strict=True)
        capture.flush()
        if rows[-1].get('type') == 'agent_state_changed' and rows[-1].get('state') == 'waiting_for_approval':
            request_ready.set()
        if rows[-1].get('type') == 'interaction_response':
            answered.set()

    def wait_worker():
        try:
            results.append(supervisor.wait_session(session, timeout_s=8, on_line=on_line))
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=wait_worker)
    thread.start()
    try:
        assert request_ready.wait(4), (rows, failures)
        with app.test_client() as client:
            detail = client.get('/api/sessions/' + session).get_json()['session']
            assert 'answer_native_request' in detail['allowed_actions']
            assert detail['current_state'] == 'waiting_for_approval'
            response = client.get('/api/sessions/' + session + '/events?include_source=0')
            question = next(row['payload'] for row in response.get_json()['events']
                            if row['kind'] == 'interaction_request')
            assert question['delivery'] == 'native_permission'
            payload = {'request_id': question['request_id'], 'client_token': 'real-native-once',
                       'selected': question['options'][0]['id']}
            admitted = client.post('/api/sessions/' + session + '/messages', json=payload)
            assert admitted.status_code == 202, admitted.get_json()
            replay = client.post('/api/sessions/' + session + '/messages', json=payload)
            assert replay.get_json() == admitted.get_json()
        services = app.extensions['fleet']['services']
        queued = commands(services)
        assert len(queued) == 1 and queued[0]['action'] == 'answer_native_request'
        assert queued[0]['payload'] == {'request_id': question['request_id'], 'selected': 'option_0'}

        def transport(path, body, headers=None):
            with app.test_client() as client:
                result = client.post(path, json=body, headers=headers)
                return result.status_code, result.get_json()

        # A separately restored Supervisor represents the polling control
        # process, proving that its private root/identity survives restart.
        restored = Supervisor(cfg.supervisor_manifest_dir, cfg.machine)
        control_client = ControlClient(hub_url='https://fixture.invalid', machine_id=cfg.machine,
            credential='fixture-supervisor', hub_public_key=services['supervisor'].public_key(),
            public_supervisor=restored, transport=transport,
            nonce_store=NonceStore(path=tmp_path / 'journal'))
        assert control_client.poll_once()['ok']
        assert answered.wait(3), (rows, failures)
        assert control_client.poll_once()['ok']
        thread.join(timeout=4)
        assert not thread.is_alive() and not failures
        assert len(results) == 1 and results[0].exit_code == 0
        capture.flush()
        requests = [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()]
        native_answers = [row for row in requests if row.get('id') == 'native_permission_1']
        assert len(native_answers) == 1
        assert native_answers[0]['result']['outcome'] == {'outcome': 'selected', 'optionId': 'yes'}
        with app.test_client() as client:
            receipt = client.get('/api/supervisor/receipts/' + admitted.get_json()['command_id'])
            assert receipt.get_json()['receipt']['status'] == 'succeeded'
            assert receipt.get_json()['receipt']['reason'] == 'answered'
            events = client.get('/api/sessions/' + session + '/events?include_source=0').get_json()['events']
            assert sum(row['kind'] == 'interaction_response' for row in events) == 1
            assert [row['payload']['text'] for row in events if row['kind'] == 'user_message'] == ['Run fixture tests']
            assert any(row['payload'].get('text') == 'permission resolved' for row in events)
            states = [row['payload']['state'] for row in events if row['kind'] == 'agent_state_changed']
            waiting_index = states.index('waiting_for_approval')
            assert states[waiting_index + 1] == 'running'
            assert states[-1] == 'completed'
            assert client.get('/api/sessions/' + session).get_json()['session']['current_state'] == 'completed'
        pending = AcpMailbox._read(cfg.supervisor_manifest_dir / 'acp' / session / 'pending.json')
        assert pending['state'] == 'delivered'
        assert 'native_permission_1' not in json.dumps(events)
        assert len(commands(services)) == 1
    finally:
        if thread.is_alive():
            supervisor.terminate_session(session, grace_s=.2)
        thread.join(timeout=3)
        capture.bridge.close()


def test_native_answer_late_ack_never_claims_answer_or_running(monkeypatch):
    import threading
    from types import SimpleNamespace
    from tools.providers.acp_mailbox import AcpMailbox
    from tools.providers.agy_runtime import AgyRuntime
    runtime = AgyRuntime.__new__(AgyRuntime)
    runtime.session_id, runtime.control_dir = 'native_fixture', '/fixture'
    runtime.invocation, runtime.permissions = 'invocation', {}
    runtime._lock, runtime.should_abort = threading.RLock(), lambda: False
    rows = []
    errors = []
    runtime.transport = SimpleNamespace(_fail=errors.append)
    runtime.emit = rows.append
    monkeypatch.setattr(AcpMailbox, 'publish', lambda *_: True)
    monkeypatch.setattr(AcpMailbox, 'wait', lambda *_, **__: 'option_0')
    monkeypatch.setattr(AcpMailbox, 'ack', lambda *_: False)
    response = runtime._client_request({'method': 'session/request_permission', 'id': 'native_raw',
        'params': {'sessionId': runtime.session_id, 'toolCall': {'title': 'Allow fixture?'},
                   'options': [{'optionId': 'native_yes', 'name': 'Allow once'}]}})
    response['_on_delivered']()
    assert not any(row['type'] == 'interaction_response' for row in rows)
    assert not any(row.get('state') == 'running' for row in rows)
    assert rows[-1]['state'] == 'unknown'
    assert errors == ['permission_delivery_unknown']
    assert not runtime.permissions
    assert 'native_raw' not in json.dumps(rows)
