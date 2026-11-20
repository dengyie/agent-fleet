"""Model recovery must change the original native continuation, not a pointer."""
import json

import pytest

from test_native_model_config import finished_claude
from tests.test_assistant_vm_sessions import app, execute  # noqa: F401
from tests.test_platform_run_worker import OWNER
from tests.test_session_interactions import event
from hub.application.errors import ApplicationError

SID = 'runner_model_recovery'


def test_catalog_native_endpoints_are_explicit_and_round_trip():
    from platform_schema import validate_model_profile
    value = validate_model_profile({'profile_id': 'p', 'provider': 'openai_compatible',
        'model': 'm', 'provider_config': {'base_url': 'https://api.example/v1',
        'agent_endpoints': {'anthropic': 'https://api.example', 'responses': 'https://api.example/v1'}}})
    assert value['provider_config']['agent_endpoints']['anthropic'] == 'https://api.example'


@pytest.fixture
def model_app(app):
    services = app.extensions['fleet']['services']
    for seq, kind, payload in [(1, 'session_start', {'agent_family': 'claude_code'}),
            (2, 'session_metadata', {'native_resume': True, 'model_takeover': True}),
            (3, 'session_close', {'reason': 'failed', 'exit_code': 1})]:
        row = event(seq, kind, payload)
        row.update(session_id=SID, event_id=f'model_evt_{seq}', stream_id='model_stream')
        assert not services['sessions'].ingest_events([row])['rejected']
    models = services['session_models']
    ref = models.secrets.store('never-output-this-key')
    models.repository.upsert_model(OWNER, {'profile_id': 'good', 'provider': 'openai_compatible',
        'model': 'working-model', 'secret_ref': ref, 'provider_config': {
            'base_url': 'https://api.example/v1', 'agent_endpoints': {'anthropic': 'https://api.example'}}})
    app.config['SUPERVISOR_CREDENTIALS'] = {'m1': 'machine-key', 'm2': 'other-key'}
    return app


def configure(app, **kw):
    return execute(app, 'session.configure_model',
        {'session_id': SID, 'mode': 'catalog', 'profile_id': 'good', **kw})


def test_catalog_switch_uses_signed_command_and_private_machine_delivery(model_app):
    app = model_app
    models = execute(app, 'session.list_models', {'session_id': SID})
    assert models['available'] and [m['profile_id'] for m in models['models']] == ['good']
    first = configure(app)
    assert configure(app) == first
    sv = app.extensions['fleet']['services']['supervisor']
    wire = sv.poll('m1')
    assert len(wire) == 1 and wire[0]['action'] == 'configure_model'
    assert wire[0]['payload']['owner_id'] == OWNER
    assert 'never-output' not in json.dumps([first, models, wire, sv.session_logs(SID)])
    client = app.test_client()
    response = client.post('/api/supervisor/model-configuration', json={'command_id': first['command_id']},
        headers={'X-Supervisor-Credential': 'm1:machine-key'})
    assert response.status_code == 200, response.json
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.json['configuration']['api_key'] == 'never-output-this-key'
    assert client.post('/api/supervisor/model-configuration', json={'command_id': first['command_id']},
        headers={'X-Supervisor-Credential': 'm2:other-key'}).status_code == 409
    assert client.post('/api/supervisor/model-configuration', json={'command_id': first['command_id']}).status_code == 403
    sv.receipt(first['command_id'], 'succeeded', 'model_configured', machine='m1')
    assert client.post('/api/supervisor/model-configuration', json={'command_id': first['command_id']},
        headers={'X-Supervisor-Credential': 'm1:machine-key'}).status_code == 409


@pytest.mark.parametrize('change', ['model', 'disabled', 'secret', 'endpoint'])
def test_catalog_change_before_delivery_refuses_without_reissuing(model_app, change):
    app = model_app
    first = configure(app)
    services = app.extensions['fleet']['services']
    models = services['session_models']
    row = models.repository.get_model(OWNER, 'good')
    if change == 'model': row['model'] = 'changed'
    if change == 'disabled': row['enabled'] = False
    if change == 'secret': row['secret_ref'] = models.secrets.store('rotated-key')
    if change == 'endpoint': row['provider_config']['agent_endpoints']['anthropic'] = 'https://changed.example'
    models.repository.upsert_model(OWNER, row)
    assert configure(app) == first
    services['supervisor'].poll('m1')
    with pytest.raises(ApplicationError):
        models.resolve('m1', first['command_id'])


def test_owner_binding_and_capability_are_required(model_app):
    services = model_app.extensions['fleet']['services']
    with pytest.raises(ApplicationError, match='model_unavailable'):
        execute(model_app, 'session.configure_model', {'session_id': SID, 'mode': 'catalog', 'profile_id': 'good'}, owner='other')
    from hub.application.session_service import SessionServiceError
    with pytest.raises(SessionServiceError):
        services['sessions'].control(SID, action='configure_model', actor='other',
            payload={'mode': 'original', 'owner_id': OWNER})
    services['sessions'].session_repo.set_capabilities(SID, ['native_resume'])
    assert not execute(model_app, 'session.list_models', {'session_id': SID})['available']
    with pytest.raises(ApplicationError, match='session_action_unavailable'):
        configure(model_app)


def test_model_command_expiry_and_revoke_authorization(model_app):
    services = model_app.extensions['fleet']['services']
    first = configure(model_app)
    sup = services['supervisor']
    sup.poll('m1')
    models = services['session_models']
    models.authorize = lambda _: False
    with pytest.raises(ApplicationError, match='forbidden'):
        models.resolve('m1', first['command_id'])
    models.authorize = None
    now = sup._now_fn()
    sup._inner._now_fn = lambda: now + sup._ttl_s + 1
    from hub.application.supervisor_service import SupervisorServiceError
    with pytest.raises(SupervisorServiceError, match='model_command_unavailable'):
        models.resolve('m1', first['command_id'])


def test_signed_model_command_reaches_node_and_replay_does_not_apply_twice(model_app, tmp_path):
    from tools.supervisor.control_client import ControlClient, NonceStore
    services = model_app.extensions['fleet']['services']
    sup = services['supervisor']
    local, sid = finished_claude(tmp_path, machine='m1', session_id=SID)
    client = model_app.test_client()
    calls = []
    def transport(path, body, headers):
        calls.append(path)
        result = client.post(path, json=body, headers=headers)
        return result.status_code, result.json
    control = ControlClient(hub_url='https://hub.example', machine_id='m1', credential='machine-key',
        hub_public_key=sup.public_key(), public_supervisor=local,
        transport=transport, nonce_store=NonceStore(tmp_path / 'nonces'))
    command = configure(model_app)
    wire = sup.poll('m1')[0]
    control._handle_command(wire)
    assert sup.receipt_status(command['command_id'])['reason'] == 'model_configured'
    assert local._prepare_user_turn(sid, 'continue', local._entries[sid])[-1]['ANTHROPIC_AUTH_TOKEN'] == 'never-output-this-key'
    control._handle_command(wire)
    assert calls.count('/api/supervisor/model-configuration') == 1
    logs = json.dumps(sup.session_logs(SID))
    assert 'never-output-this-key' not in logs


def test_termination_supersedes_a_pending_model_change(model_app):
    sup = model_app.extensions['fleet']['services']['supervisor']
    first = configure(model_app)
    sup.enqueue('m1', SID, None, 'terminate_session', 'operator_requested')
    assert sup.receipt_status(first['command_id'])['reason'] == 'superseded'
    from hub.application.supervisor_service import SupervisorServiceError
    with pytest.raises(SupervisorServiceError):
        sup.model_command('m1', first['command_id'])


@pytest.mark.parametrize('endpoint', ['https://user:pass@example.com', 'https://example.com?key=abc',
                                     'https://example.com/#x', 'file:///tmp/key', 'https://x\nHost: y'])
def test_invalid_protocol_urls_are_rejected(endpoint):
    from platform_schema import validate_model_profile, PlatformValidationError
    with pytest.raises(PlatformValidationError):
        validate_model_profile({'profile_id': 'p', 'provider': 'openai_compatible', 'model': 'm',
            'provider_config': {'agent_endpoints': {'anthropic': endpoint}}})
