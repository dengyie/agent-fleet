"""Normal assistant service logs must use the registered service's Node."""
import os
import time

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.application.run_worker_service import LocalRunWorkerService
from hub.platform.providers.base import ModelResponse
from tests.test_platform_node_runtime import OWNER, PRIVATE_KEY, PUBLIC_KEY, _Transport
from tests.test_platform_run_worker import _catalog, _queued
from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig
from tools.platform.node_executor import NodeToolExecutor
from tools.platform.services import ServiceLogReaderError
from fleet_core.command_timing import CommandTimingPolicy
from hub.platform.service_log_broker import ServiceLogBroker


@pytest.mark.skipif(os.name == 'nt', reason='POSIX executable log fixture')
@pytest.mark.parametrize('remote_workspace', [False, True])
def test_normal_service_logs_suspend_resume_on_service_node_without_replay(tmp_path, remote_workspace):
    app = create_app(FleetConfig.from_root(
        tmp_path, dev_operator=OWNER, platform_enabled=True,
        service_monitoring_enabled=True, platform_command_signing_raw=PRIVATE_KEY,
        platform_require_command_signature=True,
    ))
    repo = _catalog(app, tmp_path)
    repo.upsert_node(OWNER, {'node_id': 'service-node', 'capabilities': {'service.logs': True}})
    repo.upsert_node(OWNER, {'node_id': 'workspace-node', 'capabilities': {'workspace.read': True}})
    repo.provision_node_credential(OWNER, 'service-node', secret='a' * 40)
    if remote_workspace:
        repo.update_defaults(OWNER, {'model_profile_id': 'local', 'workspace_id': 'home',
                                    'execution_node_id': 'workspace-node'}, 1)
    services = app.extensions['fleet']['services']
    services['service_health'].register(OWNER, {
        'service_id': 'api', 'node_id': 'service-node', 'adapter': 'systemd',
        'target_alias': 'api.service',
    })
    shims = tmp_path / 'shims'
    shims.mkdir()
    record = tmp_path / 'log-invocations'
    executable = shims / 'journalctl'
    executable.write_text('#!/bin/sh\n'
        f'printf "call\\n" >> {str(record)!r}\n'
        'printf "service-ready token=private-value\\n"\n', encoding='utf-8')
    executable.chmod(0o755)
    runtime = NodeRuntime(NodeRuntimeConfig(
        node_id='service-node', credential='service-node:' + 'a' * 40,
        workspace_root=tmp_path / 'unused', journal_path=tmp_path / 'logs.db',
        hub_url='https://hub.invalid', public_key=PUBLIC_KEY,
        capabilities=frozenset({'service.logs'}), log_path_prefix=(shims,),
    ), transport=_Transport(app.test_client()))
    run_id = _queued(app)['run_id']
    calls = []

    class Provider:
        def complete(self, messages, tools, **kwargs):
            assert 'service.read_logs' in {tool['name'] for tool in tools}
            calls.append(messages)
            if len(calls) == 1:
                return ModelResponse(kind='tool_call', tool='service.read_logs',
                                     arguments={'service_id': 'api', 'window_s': 60, 'max_bytes': 1024})
            assert 'service-ready' in str(messages[-1]['result'])
            assert 'private-value' not in str(messages)
            return ModelResponse(kind='final', text='日志读取完成')

    now = [time.time()]

    def worker(name):
        return LocalRunWorkerService(repo, services['run_events'], worker_id=name, clock=lambda: now[0],
            diagnostics=services['diagnostics'], remote_execution_enabled=True,
            remote_delivery=services['platform_delivery'], provider_factory=lambda _: Provider())

    first = worker('first').run_once(OWNER)
    assert first['state'] == 'waiting_node'
    command_id = run_id + ':step:1'
    commands = app.extensions['fleet']['platform_commands']
    command = commands.get(command_id)
    assert command['action'] == 'service.logs'
    assert command['target_node'] == 'service-node' and command['resource_id'] == 'api'
    assert command['run_id'] == run_id
    assert command['arguments'] == {'adapter': 'systemd', 'target_alias': 'api.service',
                                    'service_version': 1, 'action': 'logs', 'window_s': 60.0, 'max_bytes': 1024}
    original_deadline = command['expires_at']
    now[0] += 1
    assert worker('still-waiting').run_once(OWNER)['state'] == 'waiting_node'
    assert len(calls) == 1 and not record.exists()
    assert runtime.poll_once() == {'ok': True, 'commands': 1, 'receipts': 1}
    assert runtime.client.handle(runtime.client.transport.poll_commands[0])['status'] == 'succeeded'
    now[0] += 1
    final = worker('restarted').run_once(OWNER)
    assert final['state'] == 'succeeded'
    assert len(calls) == 2 and record.read_text().splitlines() == ['call']
    assert commands.get(command_id)['expires_at'] == original_deadline
    progress = repo.get_run(OWNER, run_id)['progress']
    segment = next(item for item in progress['segments'] if item.get('tool') == 'service.read_logs')
    assert segment['title'] == '读取服务日志 · api'
    assert 'service-ready' in segment['output'] and 'private-value' not in str(progress)
    receipts = [event['payload'] for event in repo.list_run_events(OWNER, run_id)['events']
                if event['kind'] == 'node_receipt']
    assert receipts and all(event['node_id'] == 'service-node' for event in receipts)


def test_worker_does_not_offer_logs_without_reader_or_capable_service(tmp_path):
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER,
        platform_enabled=True, service_monitoring_enabled=True))
    repo = _catalog(app, tmp_path)
    _queued(app)
    services = app.extensions['fleet']['services']
    declarations = []

    class Provider:
        def complete(self, messages, tools, **kwargs):
            declarations.append({tool['name'] for tool in tools})
            return ModelResponse(kind='final', text='done')

    result = LocalRunWorkerService(repo, services['run_events'], diagnostics=services['diagnostics'],
        remote_execution_enabled=True, remote_delivery=services['platform_delivery'],
        provider_factory=lambda _: Provider()).run_once(OWNER)
    assert result['state'] == 'succeeded'
    assert 'service.get_health' in declarations[0]
    assert 'service.read_logs' not in declarations[0]


@pytest.mark.parametrize('condition,error', [
    ('other-owner', 'not_found'), ('disabled-service', 'not_found'),
    ('disabled-node', 'service_logs_unavailable'), ('missing-capability', 'service_logs_unavailable'),
    ('reconcile-only', 'service_logs_unavailable'), ('cancelled', 'execution_cancelled'),
    ('oversized', 'invalid_max_bytes'), ('forged-arguments', 'invalid_arguments'),
])
def test_service_log_admission_rejects_invalid_target_without_enqueue(tmp_path, condition, error):
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER,
        platform_enabled=True, service_monitoring_enabled=True,
        platform_command_signing_raw=PRIVATE_KEY))
    repo = _catalog(app, tmp_path)
    services = app.extensions['fleet']['services']
    caps = {'service.logs': True}
    if condition == 'missing-capability':
        caps = {}
    elif condition == 'reconcile-only':
        caps = {'reconcile.service.logs': True}
    repo.upsert_node(OWNER, {'node_id': 'service-node', 'capabilities': caps})
    services['service_health'].register(OWNER, {
        'service_id': 'api', 'node_id': 'service-node', 'adapter': 'systemd',
        'target_alias': 'api.service', 'enabled': condition != 'disabled-service'})
    if condition == 'disabled-node':
        repo.upsert_node(OWNER, {'node_id': 'service-node', 'capabilities': caps, 'enabled': False})
    broker = ServiceLogBroker(services['diagnostics'], repo, services['platform_delivery'],
        run_id='run-log', clock=time.time, timing_policy=CommandTimingPolicy(),
        should_cancel=lambda: condition == 'cancelled')
    owner = 'other@example.test' if condition == 'other-owner' else OWNER
    arguments = {'service_id': 'api'}
    if condition == 'oversized':
        arguments['max_bytes'] = 65537
    if condition == 'forged-arguments':
        arguments['argv'] = ['cat']
    receipt = broker.execute(command_id='run-log:step:1', tool='service.read_logs',
                             arguments=arguments, owner_id=owner, epoch=1)
    assert receipt.state == 'failed' and receipt.error_code == error
    assert app.extensions['fleet']['platform_commands'].get('run-log:step:1') is None
    if condition not in {'cancelled', 'oversized', 'forged-arguments'}:
        assert broker.available(owner) is False


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('failure', ['reader_failed', 'timeout', 'permission_denied'])
def test_normal_log_action_requires_its_own_capability_and_reports_known_failures(enabled, failure):
    calls = []

    def reader(command):
        calls.append(command)
        raise ServiceLogReaderError(failure)

    executor = NodeToolExecutor(None, log_reader=reader,
        allowed_service_actions={'service.logs'} if enabled else set(),
        allowed_postcheck_actions={'reconcile.service.logs'})
    result = executor({'command_id': 'log-command', 'action': 'service.logs', 'resource_id': 'api',
        'arguments': {'adapter': 'systemd', 'target_alias': 'api.service', 'service_version': 1,
                      'action': 'logs', 'window_s': 60, 'max_bytes': 1024}})
    assert result['state'] == 'failed'
    assert result['error_code'] == (failure if enabled else 'capability_unavailable')
    assert len(calls) == int(enabled)


@pytest.mark.parametrize('outcome', ['failed', 'unknown', 'cancelled'])
def test_waiting_log_receipt_terminal_outcomes_do_not_recall_provider_or_enqueue(tmp_path, outcome):
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER,
        platform_enabled=True, service_monitoring_enabled=True,
        platform_command_signing_raw=PRIVATE_KEY))
    repo = _catalog(app, tmp_path)
    services = app.extensions['fleet']['services']
    repo.upsert_node(OWNER, {'node_id': 'service-node', 'capabilities': {'service.logs': True}})
    services['service_health'].register(OWNER, {
        'service_id': 'api', 'node_id': 'service-node', 'adapter': 'systemd', 'target_alias': 'api.service'})
    run = _queued(app)
    calls = []
    now = [time.time()]

    class Provider:
        def complete(self, messages, tools, **kwargs):
            calls.append(messages)
            assert len(calls) == 1
            return ModelResponse(kind='tool_call', tool='service.read_logs', arguments={'service_id': 'api'})

    def worker():
        return LocalRunWorkerService(repo, services['run_events'], diagnostics=services['diagnostics'],
            remote_execution_enabled=True, remote_delivery=services['platform_delivery'],
            clock=lambda: now[0],
            provider_factory=lambda _: Provider())

    assert worker().run_once(OWNER)['state'] == 'waiting_node'
    commands = app.extensions['fleet']['platform_commands']
    command_id = run['run_id'] + ':step:1'
    original_deadline = commands.get(command_id)['expires_at']
    if outcome == 'unknown':
        commands.mark_unknown(command_id, reason='receipt_timeout')
    elif outcome == 'failed':
        commands.claim_for_node('service-node', 'node-worker', owner_id=OWNER)
        commands.record_receipt(command_id, 'failed', worker_id='node-worker',
            result={'state': 'failed', 'result': {}, 'error_code': 'reader_failed'})
    else:
        repo.cancel_run(OWNER, run['run_id'], now=time.time())
    now[0] += 1
    assert worker().run_once(OWNER)['state'] == outcome
    assert len(calls) == 1
    assert commands.get(command_id)['expires_at'] == original_deadline
