"""Administrator tooling must match executable session capabilities."""
import pytest
from hub.application.errors import ApplicationError
from hub.platform.session_tools import SESSION_TOOL_DEFINITIONS
from tests.test_assistant_vm_sessions import app, execute  # noqa: F401


def test_profile_control_exposed_and_idempotent(app):
    service = app.extensions['fleet']['services']['supervisor']
    definitions = {t['name']: t for t in SESSION_TOOL_DEFINITIONS}
    props = definitions['session.control']['parameters']['properties']
    assert 'apply_local_profile' in props['action']['enum']
    args = {'session_id': 's1', 'action': 'apply_local_profile', 'profile_id': 'office'}
    first = execute(app, 'session.control', args)
    assert execute(app, 'session.control', args) == first
    commands = service.poll('m1')
    assert len(commands) == 1
    assert commands[0]['payload'] == {'profile_id': 'office'}
    with pytest.raises(ApplicationError, match='idempotency_conflict'):
        execute(app, 'session.control', {**args, 'profile_id': 'other'})


def test_rejected_command_diagnostics_do_not_require_service_registration(app):
    services = app.extensions['fleet']['services']
    first = execute(app, 'session.send_message', {'session_id': 's1', 'text': 'continue'})
    sup = services['supervisor']
    sup.poll('m1')
    sup.receipt(first['command_id'], 'rejected', 'unsupported_action', machine='m1')
    history = execute(app, 'session.read_logs', {'session_id': 's1'})
    assert history['commands'][0]['command_id'] == first['command_id']
    assert history['commands'][0]['reason'] == 'unsupported_action'
    assert history['commands'][0]['reason_code'] == 'operator_requested'
    assert history['commands'][0]['status'] == 'rejected'
    assert history['audit']
    assert 'signature' not in str(history) and 'continue' not in str(history)


def test_profile_discovery_is_session_machine_and_family_scoped(app):
    services = app.extensions['fleet']['services']
    services['observe'].ingest({'machine': 'm1', 'agents': {}, 'local_profiles': [
        {'profile_id': 'p1', 'family': 'claude_code', 'label': 'Office', 'origin': 'cc_switch', 'current': True},
        {'profile_id': 'p2', 'family': 'codex', 'label': 'Other', 'origin': 'cc_switch', 'current': False}]})
    result = execute(app, 'session.list_profiles', {'session_id': 's1'})
    assert result['machine_id'] == 'm1'
    assert [r['profile_id'] for r in result['profiles']] == ['p1']
    assert result['purpose'] == 'local_provider_selection_not_capture'


def test_zcode_metadata_does_not_advertise_native_delivery(app):
    from tests.test_session_interactions import event
    sessions = app.extensions['fleet']['services']['sessions']
    row = event(1, 'session_start', {'agent_family': 'zcode', 'adapter': 'metadata'})
    row.update(session_id='zcode_metadata', stream_id='zcode_stream')
    sessions.ingest_events([row])
    result = execute(app, 'session.get', {'session_id': 'zcode_metadata'})['session']
    assert 'append_user_turn' not in result['allowed_actions']
    assert result['message_delivery']['reason'] == 'native_resume_unsupported'


@pytest.mark.parametrize('family', ['zcode', 'hermes'])
@pytest.mark.parametrize('owned,capabilities,expected', [
    (False, ['native_resume'], 'native_resume_unsupported'),
    (True, [], 'native_session_unbound'),
    (True, ['native_resume'], 'node_receipt_required'),
])
def test_new_native_families_require_owned_runner_identity(app, family, owned, capabilities, expected):
    from tests.test_session_interactions import event
    sessions = app.extensions['fleet']['services']['sessions']
    sid = ('runner_' if owned else 'metadata_') + family
    start = event(1, 'session_start', {'agent_family': family, 'adapter': 'structured_stream'})
    start.update(session_id=sid, stream_id=sid + '_stream')
    assert not sessions.ingest_events([start])['rejected']
    sessions.session_repo.set_capabilities(sid, capabilities)
    detail = execute(app, 'session.get', {'session_id': sid})['session']
    assert detail['message_delivery']['reason'] == expected
    assert ('append_user_turn' in detail['allowed_actions']) == (expected == 'node_receipt_required')
    if expected == 'node_receipt_required':
        command = execute(app, 'session.send_message', {'session_id': sid, 'text': 'follow'})
        assert command['status'] == 'pending'
    else:
        with pytest.raises(ApplicationError, match=(
                'unsupported_action' if not owned else 'session_action_unavailable')):
            execute(app, 'session.send_message', {'session_id': sid, 'text': 'follow'})
        assert app.extensions['fleet']['services']['supervisor'].poll('m1') == []


def test_unknown_agent_family_cannot_advertise_followup_support(app):
    sessions = app.extensions['fleet']['services']['sessions']
    sessions.session_repo.upsert_session({'session_id': 'unknown_agent', 'machine_id': 'm1',
        'process_group_id': 'g1', 'managed': True, 'capture_quality': 'structured'})
    detail = execute(app, 'session.get', {'session_id': 'unknown_agent'})['session']
    assert 'append_user_turn' not in detail['allowed_actions']
    with pytest.raises(ApplicationError, match='unsupported_action'):
        execute(app, 'session.send_message', {'session_id': 'unknown_agent', 'text': 'continue'})


def test_enabled_supervisor_defaults_to_administrator_message_and_profile_controls(tmp_path):
    from hub.config import FleetConfig
    config = FleetConfig.from_root(tmp_path, supervisor_enabled=True)
    assert config.append_user_turn_enabled and config.apply_local_profile_enabled
    off = FleetConfig.from_root(tmp_path, supervisor_enabled=True,
        append_user_turn_enabled=False, apply_local_profile_enabled=False)
    assert not off.append_user_turn_enabled and not off.apply_local_profile_enabled


def test_logs_paginate_only_target_session_and_revalidate_admin(app):
    sup = app.extensions['fleet']['services']['supervisor']
    ids = []
    for i in range(3):
        ids.append(execute(app, 'session.control', {'session_id': 's1', 'action': 'pause_session'},
                           command_id=f'step-{i}')['command_id'])
    sup.enqueue('m1', 'other', None, 'pause_session', 'operator_requested')
    page = execute(app, 'session.read_logs', {'session_id': 's1', 'limit': 2})
    assert [r['command_id'] for r in page['commands']] == ids[::-1][:2]
    rest = execute(app, 'session.read_logs', {'session_id': 's1', 'limit': 2, 'before_cursor': page['next_cursor']})
    assert [r['command_id'] for r in rest['commands']] == ids[:1]
    assert rest['has_more'] is False
    service = app.extensions['fleet']['services']['agent_sessions']
    service.authorize = lambda owner: False
    with pytest.raises(ApplicationError, match='forbidden'):
        execute(app, 'session.read_logs', {'session_id': 's1'})


@pytest.mark.parametrize('args', [
    {'action': 'pause_session', 'profile_id': 'wrong'},
    {'action': 'apply_local_profile'},
])
def test_profile_payload_cannot_be_used_on_process_controls(app, args):
    with pytest.raises(ApplicationError, match='invalid_arguments'):
        execute(app, 'session.control', {'session_id': 's1', **args})


def test_assistant_event_pages_default_to_conversation_and_keep_full_source_opt_in(app):
    from tests.test_session_interactions import event
    sessions = app.extensions['fleet']['services']['sessions']
    for seq in range(3, 7):
        row = event(seq, 'source_record', {'text': 'repeated model request context', 'is_complete': True})
        assert not sessions.ingest_events([row])['rejected']
    assert not sessions.ingest_events([event(7, 'assistant_message', {'text':'new answer', 'is_complete':True})])['rejected']
    result = execute(app, 'session.read_events', {'session_id':'s1','after_cursor':'2','limit':1})
    assert result['events'][0]['kind'] == 'assistant_message'
    assert result['events'][0]['payload']['text'] == 'new answer'
    assert result['next_cursor'] == '7' and not result['has_more']
    source = execute(app, 'session.read_events', {'session_id':'s1','after_cursor':'2','limit':1,'include_source':True})
    assert source['events'][0]['kind']=='source_record' and source['has_more']


def test_existing_unbound_zcode_adoption_does_not_control_entire_desktop(app):
    from tests.test_adoption_service import instance_row, DEFAULT_PID, DEFAULT_STARTED_AT
    services = app.extensions['fleet']['services']
    services['observe'].ingest({'machine':'m1', 'agents':{}, 'instances': [instance_row(
        agent_family='zcode', exe_path='/Applications/ZCode.app/Contents/MacOS/ZCode')]})
    row = services['adoptions'].adopt('m1', DEFAULT_PID, DEFAULT_STARTED_AT, 'owner@example.test')
    services['adoptions'].adoption_repo.update_status(row.session_id, 'adopted')
    detail = execute(app, 'session.get', {'session_id':row.session_id})['session']
    assert detail['adoption_status'] == 'adopted'
    assert detail['allowed_actions'] == []
    with pytest.raises(ApplicationError, match='session_action_unavailable'):
        execute(app, 'session.control', {'session_id':row.session_id, 'action':'terminate_session'})
    assert all(c['action']=='adopt' for c in services['supervisor'].poll('m1'))
