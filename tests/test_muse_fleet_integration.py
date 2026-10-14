import time

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.control import generate_ed25519_keypair
from hub.domain.platform_command import PlatformCommand
from tools.browser_bridge.contract import WEB_AGENT_TOOLS, WebAgentExecutor
from tools.browser_bridge.muse import MuseBridge, MuseBridgeError
from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig
from tools.platform.remote_tool_broker import RemoteToolBroker
from tools.platform.tool_broker import ToolBroker
from test_muse_browser_bridge import Transport, message
from test_platform_node_http_e2e import FlaskTransport

OWNER = 'owner@example.test'


def test_signed_hub_node_submit_read_and_duplicate_command(tmp_path):
    private, public = generate_ed25519_keypair()
    app = create_app(FleetConfig.from_root(
        tmp_path, ingest_token='ingest', dev_operator=OWNER, platform_enabled=True,
        platform_command_signing_raw=private))
    repo = app.extensions['fleet']['platform_repository']
    delivery = app.extensions['fleet']['services']['platform_delivery']
    repo.upsert_node(OWNER, {'node_id':'muse-node', 'label':'Local Muse',
                            'capabilities': {name:True for name in WEB_AGENT_TOOLS}})
    repo.provision_node_credential(OWNER, 'muse-node', secret='n' * 40)
    runtime = NodeRuntime(NodeRuntimeConfig(
        node_id='muse-node', credential='muse-node:' + 'n' * 40,
        hub_url='https://hub.invalid', workspace_root=tmp_path / 'workspace',
        journal_path=tmp_path / 'journal.db', public_key=public,
        capabilities=WEB_AGENT_TOOLS, muse_bridge_enabled=True,
        muse_bridge_state_path=tmp_path / 'muse.db', muse_workspace_ids=('workspace',)),
        transport=FlaskTransport(app.test_client()))
    transport = Transport()
    runtime.web_agent_executor.bridge = MuseBridge(tmp_path / 'muse.db', transport=transport)

    class Waiter:
        def wait_for_receipt(self, command_id, **kwargs):
            result = runtime.poll_once()
            assert result == {'ok':True, 'commands':1, 'receipts':1}
            return delivery.wait_for_receipt(command_id, timeout_s=1)

    broker = RemoteToolBroker(delivery, node_id='muse-node', resource_id='workspace',
                              run_id='run-muse', waiter=Waiter(), web_agent_enabled=True)
    submitted = broker.execute(command_id='run-muse:step:1', tool='web_agent.submit',
        arguments={'task_id':'task-muse', 'prompt':'Say hello'}, owner_id=OWNER, epoch=1)
    assert submitted.state == 'succeeded'
    assert submitted.result['state'] == 'sent'
    assert len(transport.sends) == 1
    command = app.extensions['fleet']['platform_commands'].get('run-muse:step:1')
    assert command['retry_class'] == 'manual_only'
    assert runtime.client.handle(command)['status'] == 'succeeded'
    assert len(transport.sends) == 1
    transport.messages.append(message('reply', 'assistant', 'Hello\n' + submitted.result['completion_marker']))
    result = broker.execute(command_id='run-muse:step:2', tool='web_agent.read',
        arguments={'task_id':'task-muse'}, owner_id=OWNER, epoch=1)
    assert result.state == 'succeeded'
    assert result.result['state'] == 'completed'
    assert result.result['reply'] == 'Hello'
    assert runtime.browser_backend is None
    assert set(runtime.manifest()['capabilities']) == WEB_AGENT_TOOLS


def test_capabilities_are_opt_in_and_workspace_bound(tmp_path):
    _, public = generate_ed25519_keypair()
    runtime = NodeRuntime(NodeRuntimeConfig(
        node_id='node-a', credential='node-a:s', hub_url='https://hub.invalid',
        workspace_root=tmp_path, journal_path=tmp_path/'journal.db', public_key=public,
        capabilities=WEB_AGENT_TOOLS))
    assert not (runtime.executor.allowed_tools & WEB_AGENT_TOOLS)
    assert not (set(runtime.manifest()['capabilities']) & WEB_AGENT_TOOLS)
    assert not (set(d['name'] for d in ToolBroker.tool_definitions()) & WEB_AGENT_TOOLS)
    assert WEB_AGENT_TOOLS <= set(d['name'] for d in ToolBroker.tool_definitions(web_agent_enabled=True))
    bridge = MuseBridge(tmp_path / 'muse.db', transport=Transport())
    executor = WebAgentExecutor(bridge, workspace_ids=['workspace'])
    with pytest.raises(MuseBridgeError, match='workspace_forbidden'):
        executor.execute('web_agent.status', {}, resource_id='other')
    with pytest.raises(MuseBridgeError, match='invalid_arguments'):
        executor.execute('web_agent.status', {'expression':'bad'}, resource_id='workspace')


def test_remote_gate_rejects_before_enqueue():
    broker = RemoteToolBroker(object(), node_id='node-a', resource_id='workspace', run_id='run-a')
    result = broker.execute(command_id='run-a:step:1', tool='web_agent.submit',
        arguments={'task_id':'a', 'prompt':'hello'}, owner_id=OWNER, epoch=1)
    assert result.error_code == 'capability_unavailable'


def test_unsigned_or_tampered_commands_never_touch_browser(tmp_path):
    private, public = generate_ed25519_keypair()
    runtime = NodeRuntime(NodeRuntimeConfig(
        node_id='node-a', credential='node-a:s', hub_url='https://hub.invalid',
        workspace_root=tmp_path, journal_path=tmp_path/'journal.db', public_key=public,
        capabilities=WEB_AGENT_TOOLS, muse_bridge_enabled=True,
        muse_bridge_state_path=tmp_path/'muse.db', muse_workspace_ids=('workspace',)))
    transport = Transport()
    runtime.web_agent_executor.bridge = MuseBridge(tmp_path/'muse.db', transport=transport)
    command = PlatformCommand.create(command_id='a', target_node='node-a', action='tool.web_agent.submit',
        resource_id='workspace', arguments={'task_id':'a','prompt':'hello'},
        retry_class='manual_only', expires_at=time.time()+60)
    assert runtime.client.handle(command.as_dict())['status'] == 'rejected'
    changed = command.signed(private).as_dict()
    changed['arguments']['prompt'] = 'changed'
    assert runtime.client.handle(changed)['status'] == 'rejected'
    assert not transport.sends


@pytest.mark.parametrize('enabled', [False, True])
def test_worker_exposes_only_selected_nodes_declared_tools(tmp_path, enabled):
    from hub.application.run_worker_service import LocalRunWorkerService
    from tools.platform.providers.base import ModelResponse
    app = create_app(FleetConfig.from_root(tmp_path, ingest_token='i', dev_operator=OWNER, platform_enabled=True))
    repo = app.extensions['fleet']['platform_repository']
    repo.upsert_node(OWNER, {'node_id':'muse-node', 'label':'Muse',
                           'capabilities': {name:True for name in WEB_AGENT_TOOLS} if enabled else {}})
    repo.upsert_model(OWNER, {'profile_id':'model', 'provider':'deterministic', 'model':'test'})
    repo.upsert_workspace(OWNER, {'workspace_id':'workspace', 'root_path':str(tmp_path)})
    repo.create_conversation(OWNER, 'conv-muse', title='', workspace_id='workspace')
    repo.append_turn(OWNER, 'conv-muse', 'msg', 'run-muse', text='Check Muse', client_token='a',
        config_snapshot={'workspace_id':'workspace', 'model_profile_id':'model', 'execution_node_id':'muse-node'}, now=1)
    seen = []
    class Provider:
        def complete(self, messages, tools, **kwargs):
            seen.extend(t['name'] for t in tools)
            return ModelResponse(kind='final', text='done')
    worker = LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
        provider_factory=lambda profile:Provider(), remote_execution_enabled=True,
        remote_delivery=app.extensions['fleet']['services']['platform_delivery'])
    assert worker.run_once(OWNER)['state'] == 'succeeded'
    assert set(seen) & WEB_AGENT_TOOLS == (WEB_AGENT_TOOLS if enabled else set())


def test_node_cli_config_requires_local_consent_and_private_files(tmp_path):
    import base64
    import json
    import os
    from tools.browser_bridge.node import load_runtime
    _, public = generate_ed25519_keypair()
    credential = tmp_path / 'credential'
    credential.write_text('node-a:local-credential')
    key = tmp_path / 'public'
    key.write_text(base64.b64encode(public).decode())
    config = {'node_id':'node-a', 'hub_url':'https://hub.invalid',
              'credential_file':str(credential), 'public_key_file':str(key),
              'state_directory':str(tmp_path / 'state'), 'workspace_ids':['workspace'],
              'allow_muse_tasks':False}
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match='local_muse_opt_in_required'):
        load_runtime(path)
    config['allow_muse_tasks'] = True
    path.write_text(json.dumps(config))
    previous_umask = os.umask(0o022)
    try:
        runtime, interval = load_runtime(path)
    finally:
        os.umask(previous_umask)
    assert interval == 2
    assert set(runtime.manifest()['capabilities']) == WEB_AGENT_TOOLS
    assert 'credential' not in str(runtime.manifest())
    assert 'workspace.exec' not in runtime.executor.allowed_tools
    assert (tmp_path / 'state').stat().st_mode & 0o077 == 0
    (tmp_path / 'state').chmod(0o755)
    with pytest.raises(ValueError, match='private_state_directory_required'):
        load_runtime(path)


def test_legacy_unsigned_node_client_cannot_execute_web_agent(tmp_path):
    from tools.platform.journal import NodeJournal
    from tools.platform.node_client import NodeClient
    journal = NodeJournal(tmp_path / 'journal.db')
    journal.init()
    calls = []
    client = NodeClient(journal, executor=lambda command:calls.append(command))
    command = PlatformCommand.create(command_id='unsigned', target_node='node-a',
        action='tool.web_agent.submit', resource_id='workspace',
        arguments={'task_id':'a','prompt':'hello'}, expires_at=time.time()+60)
    assert client.handle(command.as_dict())['status'] == 'rejected'
    assert calls == []
