import pytest
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.control import generate_ed25519_keypair

OWNER = 'owner@example.test'


def build_app(tmp_path, *, submit=True, network=True, browser=True, operator=OWNER):
    private, public = generate_ed25519_keypair()
    app = create_app(FleetConfig.from_root(tmp_path, ingest_token='test-ingest', dev_operator=operator,
        platform_enabled=True, platform_browser_enabled=browser,
        platform_browser_network_enabled=network, platform_browser_submit_enabled=submit,
        platform_command_signing_raw=private))
    return app, public


def seed(app, tmp_path):
    repo = app.extensions['fleet']['platform_repository']
    browser = app.extensions['fleet']['repositories']['browser']
    repo.upsert_workspace(OWNER, {'workspace_id': 'workspace-a', 'root_path': str(tmp_path)})
    for run, sid in [('run-a', 'session-aaaaaaaaaa'), ('run-b', 'session-bbbbbbbbbb')]:
        repo.create_conversation(OWNER, 'conv-' + run, title='', workspace_id='workspace-a')
        repo.append_turn(OWNER, 'conv-' + run, 'msg-' + run, run, text='test', client_token=run,
                         config_snapshot={'workspace_id': 'workspace-a', 'execution_node_id': 'node-a'}, now=100)
        browser.create_session(OWNER, workspace_id='workspace-a', run_id=run, node_id='node-a', profile_id='profile-a', session_id=sid)
    return repo, browser


def test_owner_grant_exact_body_idempotency_and_scoped_revoke(tmp_path):
    app, _ = build_app(tmp_path)
    _, repo = seed(app, tmp_path)
    client = app.test_client()
    path = '/api/platform/v1/runs/run-a/browser-approvals'
    body = {'session_id': 'session-aaaaaaaaaa', 'selector': '#go'}
    response = client.post(path, json=body, headers={'Idempotency-Key': 'grant-1'})
    assert response.status_code == 200
    approval = response.get_json()['approval']
    assert 'selector' not in approval and 'owner_id' not in approval
    assert approval['granted_at'].endswith('Z')
    assert client.post(path, json=body, headers={'Idempotency-Key': 'grant-1'}).get_json()['approval'] == approval
    wrong = client.delete('/api/platform/v1/runs/run-b/browser-approvals/' + approval['approval_id'])
    assert wrong.status_code == 404
    assert repo.get_submit_approval(OWNER, approval['approval_id'])['state'] == 'active'
    revoked = client.delete(path + '/' + approval['approval_id'])
    assert revoked.status_code == 200
    assert revoked.get_json()['approval']['state'] == 'revoked'
    assert client.post(path, json={**body, 'node_id': 'node-a'}).status_code == 400


@pytest.mark.parametrize('submit,network,browser', [(False, True, True), (True, False, True), (True, True, False)])
def test_submit_parent_gates(tmp_path, submit, network, browser):
    app, _ = build_app(tmp_path, submit=submit, network=network, browser=browser)
    assert app.config['PLATFORM_BROWSER_SUBMIT_ENABLED'] is False
    response = app.test_client().post('/api/platform/v1/runs/run-a/browser-approvals', json={'session_id': 'session-aaaaaaaaaa', 'selector': '#go'})
    assert response.status_code == 404


def test_grants_reject_cross_run_and_anonymous_owners(tmp_path):
    app, _ = build_app(tmp_path)
    seed(app, tmp_path)
    response = app.test_client().post('/api/platform/v1/runs/run-b/browser-approvals', json={'session_id': 'session-aaaaaaaaaa', 'selector': '#go'})
    assert response.status_code == 404
    app.config['DEV_OPERATOR'] = None
    response = app.test_client().post('/api/platform/v1/runs/run-a/browser-approvals', json={'session_id': 'session-aaaaaaaaaa', 'selector': '#go'})
    assert response.status_code in (401, 403)


def test_node_credentials_cannot_grant_and_other_owner_cannot_revoke(tmp_path):
    app, _ = build_app(tmp_path)
    platform, approvals = seed(app, tmp_path)
    platform.upsert_node(OWNER, {'node_id': 'node-a'})
    platform.provision_node_credential(OWNER, 'node-a', secret='n' * 40)
    client = app.test_client()
    path = '/api/platform/v1/runs/run-a/browser-approvals'
    granted = client.post(path, json={'session_id': 'session-aaaaaaaaaa', 'selector': '#go'}).get_json()['approval']
    app.config['DEV_OPERATOR'] = None
    assert client.post(path, json={'session_id': 'session-aaaaaaaaaa', 'selector': '#go'},
                       headers={'X-Platform-Node-Credential': 'node-a:' + 'n' * 40}).status_code in (401, 403)
    response = client.delete(path + '/' + granted['approval_id'], headers={'CF-Access-Authenticated-User-Email': 'other@example.test'})
    assert response.status_code in (401, 403, 404)
    assert approvals.get_submit_approval(OWNER, granted['approval_id'])['state'] == 'active'


@pytest.mark.parametrize('selector', ['#' + '表' * 171, '#password', '\ud800', '', '#go\n'])
def test_submit_selector_contract_rejects_invalid_owner_requests(tmp_path, selector):
    app, _ = build_app(tmp_path)
    seed(app, tmp_path)
    response = app.test_client().post('/api/platform/v1/runs/run-a/browser-approvals',
                                     json={'session_id': 'session-aaaaaaaaaa', 'selector': selector})
    assert response.status_code == 400
