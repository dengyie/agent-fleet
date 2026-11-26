"""Browser account management against real public HTTP/SQLite and signed queue.

Only node OAuth settlement is scripted; no real Google authorization/model calls.
"""
import os
from pathlib import Path
import subprocess

import pytest
from flask import jsonify, request

from fleet_core.control import generate_ed25519_keypair
from hub.bootstrap import create_app
from support.hub_env import HubStores
from tests.test_provider_accounts import AUTH_URL
from tests.test_workflow_browser import _browser_acceptance

ROOT = Path(__file__).resolve().parents[1]


def test_provider_account_browser_contract_and_task_recovery():
    result = subprocess.run(['node', '--input-type=module'], cwd=ROOT, capture_output=True, text=True, input=r"""
import assert from 'node:assert/strict';
import {parseProviderAccounts, authorizationUrl} from './frontend/assets/api/provider-account-contracts.js';
import {parseTask, parseSessionEvents} from './frontend/assets/api/contracts.js';
import {createTaskJournal} from './frontend/assets/views/agent-chat/service.js';
const id = 'pa_'+'a'.repeat(32), command='cmd_'+'b'.repeat(32);
const row={account_id:id,machine:'mac',name:'Work',provider:'agy',revision:1,state:'ready',enabled:true,created_at:'2026-11-25T10:00:00Z',updated_at:'2026-11-25T10:00:00Z',credentials:'not-public'};
assert.equal(parseProviderAccounts({ok:true,accounts:[row]},{machine:'mac'}).accounts[0].credentials,undefined);
assert.throws(()=>parseProviderAccounts({ok:true,accounts:[row]},{machine:'other'}));
assert.throws(()=>parseProviderAccounts({ok:true,accounts:[row,row]},{machine:'mac'}));
const op={command_id:command,account_id:id,revision:1,operation:'login',state:'starting',status:'executing',created_at:row.created_at,expires_at:row.updated_at};
assert.equal(parseProviderAccounts({ok:true,operation:op},{accountId:id,command}).operation.status,'executing');
assert.throws(()=>parseProviderAccounts({ok:true,operation:op},{accountId:id,command:'cmd_'+'c'.repeat(32)}));
const url='https://accounts.google.com/o/oauth2/auth?state=fixture&response_type=code&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback';
assert.equal(authorizationUrl(url),url);
for(const bad of ['javascript:alert(1)',url+'&state=other',url.replace('accounts.google.com','evil.test'),url+'#fragment']) assert.equal(authorizationUrl(bad),null);
assert.throws(()=>parseProviderAccounts({ok:true,account:{...row,state:'waiting',authorization_url:'https://evil.test',login_command_id:command,expires_at:1800000000}},{accountId:id}));
assert.equal(parseTask({ok:true,task:{task_id:'t1',provider_account_id:id}}).task.provider_account_id,id);
const data=new Map(), journal=createTaskJournal({getItem:k=>data.get(k),setItem:(k,v)=>data.set(k,v),removeItem:k=>data.delete(k)},'task');
const request={machine:'mac',project:'fleet',agent_type:'agy',instruction:'check',client_token:'task-id',provider_account_id:id};
journal.write(request); assert.deepEqual(journal.read(),request);
journal.write({...request,agent_type:'codex'}); assert.equal(journal.read(),null);
journal.write({...request,provider_account_id:'broken'}); assert.equal(journal.read(),null);
""")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('viewport', ['desktop', 'mobile'])
def test_provider_accounts_settings_and_bound_task_browser(tmp_path, viewport):
    if not os.environ.get('FLEET_PLAYWRIGHT_MODULE'):
        pytest.skip('Set FLEET_PLAYWRIGHT_MODULE for real browser acceptance')
    stores = HubStores(tmp_path)
    private, _ = generate_ed25519_keypair()
    app = create_app(stores.config(frontend_dir=ROOT / 'frontend', dev_operator='browser@example.test',
        platform_enabled=True, supervisor_enabled=True, supervisor_signing_raw=private,
        supervisor_credentials={'mac-local': 'fixture-secret'}, session_encryption_raw=b'k' * 32,
        project_whitelist={'mac-local': ['fleet']}, session_repositories_enabled=True))
    services = app.extensions['fleet']['services']
    services['observe'].ingest({'machine': 'mac-local', 'reachable': True, 'agents': {
        'agy': {'installed': True, 'provider_accounts': {'schema_version': 1}}, 'codex': {'installed': True}}})
    services['observe'].ingest({'machine': 'legacy', 'reachable': True, 'agents': {'agy': {'installed': False}}})

    @app.post('/_fixture/settle-account')
    def settle():
        state = request.json['state']
        wires = services['supervisor'].poll('mac-local')
        for wire in wires:
            payload = wire['payload']
            data = {'command_id': wire['command_id'], 'account_id': payload['account_id'], 'revision': payload['revision'], 'state': state}
            if state == 'waiting':
                import time
                data.update(authorization_url=AUTH_URL, expires_at=time.time() + 290)
            assert services['provider_accounts'].report('mac-local', data)['accepted']
            services['supervisor'].receipt(wire['command_id'], 'succeeded', 'applied')
        return jsonify({'count': len(wires)})

    _browser_acceptance(app, 'provider_accounts_browser.cjs', viewport)
