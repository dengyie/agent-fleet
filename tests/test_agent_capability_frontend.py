"""Public session authority survives the browser contract and presentation."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from threading import Thread

import pytest
from werkzeug.serving import make_server

from hub.bootstrap import create_app
from hub.config import FleetConfig

ROOT = Path(__file__).resolve().parents[1]


def test_public_delivery_contract_is_preserved_and_cannot_grant_authority():
    if not shutil.which('node'):
        pytest.skip('Node is an optional contract verifier')
    script = r"""
const assert = await import('node:assert/strict');
const {parseSession, ContractError} = await import(process.argv[1]);
const base = {session_id:'runner_fixture', machine_id:'m1', agent_family:'hermes',
  managed:true, control_capability:'available', allowed_actions:['append_user_turn']};
const delivery = {available:true, reason:'node_receipt_required', mechanism:'native_session_resume'};
const parsed = parseSession({session:{...base, message_delivery:{...delivery, unknown:'discard'}}}).session;
assert.deepEqual(parsed.message_delivery, delivery);
const revoked = parseSession({session:{...base, adoption_status:'revoked', message_delivery:delivery}}).session;
assert.equal(revoked.message_delivery.available, false);
assert.deepEqual(revoked.allowed_actions, []);
const old = parseSession({session:base}).session;
assert.deepEqual(old.allowed_actions, ['append_user_turn']);
assert.equal(old.message_delivery, undefined);
assert.deepEqual(parseSession({session:{...base,allowed_actions:['configure_model']}}).session.allowed_actions, ['configure_model']);
assert.throws(() => parseSession({session:{...base, message_delivery:{...delivery, available:'true'}}}), ContractError);
assert.throws(() => parseSession({session:{...base, message_delivery:{...delivery, mechanism:'arbitrary_shell'}}}), ContractError);
const provenance = {native_source_bound:true, history:'retained_source_from_checkpoint', provider_profile_changes_capture:false};
assert.deepEqual(parseSession({session:{...base,capture:{...provenance,raw_path:'/private'}}}).session.capture, provenance);
assert.equal(parseSession({session:{...base,control_unavailable_reason:'native_session_unbound'}}).session.control_unavailable_reason, 'native_session_unbound');
"""
    result = subprocess.run(['node', '--input-type=module', '-e', script,
        (ROOT / 'frontend/assets/api/contracts.js').as_uri()], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_presentation_uses_declared_actions_without_family_or_os_policy():
    if not shutil.which('node'):
        pytest.skip('Node is an optional contract verifier')
    script = r"""
const assert = await import('node:assert/strict');
const {sessionControls} = await import(process.argv[1]);
const enabled = {append_user_turn:true, apply_local_profile:true};
for (const family of ['hermes','zcode','codex','claude_code','pi','future-agent']) {
  const base = {session_id:'runner_fixture', agent_family:family,
    control_capability:'available', allowed_actions:['append_user_turn','apply_local_profile']};
  assert.equal(sessionControls(base, enabled).appendUserTurn, true);
  assert.equal(sessionControls(base, {}).appendUserTurn, false);
  assert.equal(sessionControls({...base,allowed_actions:[]},enabled).appendUserTurn,false);
  assert.equal(sessionControls({...base,control_capability:'unavailable'},enabled).appendUserTurn,false);
  const denied = sessionControls({...base,message_delivery:{available:false,reason:'session_busy'}},enabled);
  assert.equal(denied.appendUserTurn,false);
  assert.equal(denied.deliveryLabel,'当前回合仍在执行，请等待完成');
}
assert.equal(sessionControls(null,enabled).canControl,false);
"""
    result = subprocess.run(['node', '--input-type=module', '-e', script,
        (ROOT / 'frontend/assets/ui/session-capabilities.js').as_uri()], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    view = (ROOT / 'frontend/assets/views/session.js').read_text()
    assert "from './machine.js'" not in view
    assert "agent_family === 'hermes'" not in view


def test_hermes_runner_followup_is_visible_and_submits_once(tmp_path):
    if not os.environ.get('FLEET_PLAYWRIGHT_MODULE'):
        pytest.skip('Set FLEET_PLAYWRIGHT_MODULE for Chromium acceptance')
    app = create_app(FleetConfig.from_root(tmp_path, frontend_dir=ROOT / 'frontend',
        dev_operator='browser@example.test', session_repositories_enabled=True,
        supervisor_enabled=True, supervisor_signing_raw=b'a' * 32,
        supervisor_credentials={'m1':'browser-receipt'}, append_user_turn_enabled=True))
    sessions = app.extensions['fleet']['services']['sessions']
    ids = []
    for width in (390, 1440):
        sid = 'runner_hermes_' + str(width)
        ids.append(sid)
        def event(seq, kind, payload):
            return {'schema_version':1, 'event_id':f'hermes-{width}-{seq}', 'session_id':sid,
                'machine_id':'m1', 'process_group_id':'g1', 'attempt_id':'a1', 'stream_id':'st1',
                'sequence':seq, 'kind':kind, 'capture_quality':'structured',
                'emitted_at':'2026-11-21T00:00:00Z', 'payload':payload}
        assert sessions.ingest_events([
            event(1, 'session_start', {'agent_family':'hermes', 'adapter':'structured_stream'}),
            event(2, 'session_metadata', {'native_resume':True}),
            event(3, 'session_close', {'reason':'completed', 'exit_code':0}),
        ])['rejected'] == []
        assert 'append_user_turn' in app.test_client().get('/api/sessions/' + sid).json['session']['allowed_actions']
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(['node', str(ROOT / 'tests/fixtures/frontend/agent_capability_browser.cjs'),
            f'http://127.0.0.1:{server.server_port}', json.dumps(ids)], capture_output=True, text=True, timeout=40)
        assert result.returncode == 0, result.stdout + result.stderr
        commands = app.extensions['fleet']['services']['supervisor'].poll('m1')
        assert [(c['target']['session_id'], c['payload']['text']) for c in commands] == [
            (sid, '继续原生会话 ' + str(width)) for sid, width in zip(ids, (390, 1440))]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
