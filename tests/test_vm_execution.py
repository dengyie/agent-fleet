"""VM execution uses the original signed Node lifecycle without a launcher."""
import base64
import json
import sys
import time
import os

import pytest

from fleet_core.control import generate_ed25519_keypair
from fleet_core.platform_command import PlatformCommand
from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig
from tools.plugins.node import install_enrollment, load_runtime
from fleet_core.backends.vm import VMBackend
from fleet_core.backends.directory import MAX_OUTPUT_BYTES

PRIVATE, PUBLIC = generate_ed25519_keypair()


def runtime(tmp_path, **kwargs):
    return NodeRuntime(NodeRuntimeConfig(node_id='vm', credential='vm:secret', public_key=PUBLIC,
        hub_url='https://hub.example', workspace_root=tmp_path / 'work', workspace_id='work',
        journal_path=tmp_path / 'state' / 'commands.db',
        capabilities=frozenset({'workspace.exec', 'workspace.list', 'workspace.read', 'workspace.write'}),
        **kwargs))


def command(argv, *, command_id='exec-1', timeout_s=10):
    return PlatformCommand.create(command_id=command_id, target_node='vm', resource_id='work',
        action='tool.workspace.exec', arguments={'argv': argv, 'timeout_s': timeout_s},
        expires_at=time.time() + 120, retry_class='reconcile_before_retry').signed(PRIVATE).as_dict()


def test_direct_vm_executes_interpreter_and_reuses_durable_receipt(tmp_path):
    node = runtime(tmp_path, execution_mode='vm_direct')
    cmd = command([sys.executable, '-c',
        'from pathlib import Path; p=Path("once.txt"); p.write_text(p.read_text()+"x" if p.exists() else "x"); print(p.resolve())'])
    try:
        receipt = node.client.handle(cmd)
        assert receipt['status'] == 'succeeded'
        assert receipt['result']['result']['output'].strip() == str(tmp_path / 'work' / 'once.txt')
        assert node.client.handle(cmd) == receipt
        assert (tmp_path / 'work' / 'once.txt').read_text() == 'x'
        assert node.manifest()['workspace_execution']['mode'] == 'vm_direct'
        assert node.manifest()['capabilities']['workspace.exec'] is True
    finally:
        node.close_all()


def test_unconfigured_sandbox_does_not_report_exec(tmp_path):
    node = runtime(tmp_path)
    try:
        assert 'workspace.exec' not in node.manifest()['capabilities']
        assert 'tool.workspace.exec' not in node._capability_actions()
        assert node.client.handle(command([sys.executable, '--version']))['status'] == 'failed'
    finally:
        node.close_all()


def test_workspace_only_enrollment_round_trips_into_original_cli(tmp_path):
    enrollment = {'node_id': 'vm', 'hub_url': 'https://hub.example', 'credential': 'vm:secret',
        'public_key': base64.b64encode(PUBLIC).decode(), 'plugins': {},
        'workspace': {'execution_mode': 'vm_direct', 'roots': {'work': str(tmp_path / 'work')},
                      'sandbox_launcher': []}}
    source = tmp_path / 'enrollment.json'
    source.write_text(json.dumps(enrollment), encoding='utf-8')
    config = install_enrollment(source, tmp_path / 'state')
    node, interval = load_runtime(config)
    try:
        assert interval == 2
        assert node.client.handle(command([sys.executable, '-c', 'print("VM_OK")']))['status'] == 'succeeded'
        assert not (tmp_path / 'state' / 'unused-workspace').exists()
    finally:
        node.close_all()


@pytest.mark.parametrize('mode', ['host', '', None, True])
def test_invalid_mode_is_rejected_before_runtime_side_effects(tmp_path, mode):
    with pytest.raises(ValueError, match='execution mode'):
        runtime(tmp_path, execution_mode=mode)
    assert not (tmp_path / 'work').exists()


def test_vm_nonzero_exit_preserves_stdout_stderr_and_code(tmp_path):
    result = VMBackend(tmp_path).execute('command', [sys.executable, '-c',
        'import sys; print("stdout"); print("stderr", file=sys.stderr); sys.exit(7)'])
    assert result.state == 'failed' and result.error_code == 'process_failed'
    assert result.result['returncode'] == 7
    assert 'stdout' in result.result['output'] and 'stderr' in result.result['output']


@pytest.mark.parametrize('timeout', [None, True, 0, -1, float('inf'), float('nan'), 10**1000])
def test_vm_invalid_timeouts_are_receipts(tmp_path, timeout):
    assert VMBackend(tmp_path).execute('command', [sys.executable], timeout_s=timeout).error_code == 'invalid_timeout'


@pytest.mark.parametrize('argv', [[], [''], 'pwd', [None], ['pwd', '\x00'], ['pwd'] * 257, ['pwd', '\ud800']])
def test_vm_invalid_argv_are_receipts(tmp_path, argv):
    assert VMBackend(tmp_path).execute('command', argv).error_code == 'invalid_command'


def test_vm_output_is_bounded_without_changing_exit_status(tmp_path):
    receipt = VMBackend(tmp_path).execute('command', [sys.executable, '-c', 'print("x" * 100000)'])
    assert receipt.state == 'succeeded' and receipt.truncated
    assert len(receipt.result['output'].encode()) == MAX_OUTPUT_BYTES


def test_vm_environment_has_workspace_home_without_hub_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv('FLEET_SECRET', 'must-not-inherit')
    receipt = VMBackend(tmp_path).execute('command', [sys.executable, '-c',
        'import os; print(os.environ["HOME"]); print(os.environ["TMPDIR"]); print(os.environ.get("FLEET_SECRET"))'])
    assert receipt.state == 'succeeded'
    assert receipt.result['output'].splitlines() == [str((tmp_path / '.fleet-home').resolve()),
        str((tmp_path / '.fleet-tmp').resolve()), 'None']


@pytest.mark.parametrize('cancel', [False, True])
def test_vm_timeout_and_cancel_reap_descendants(tmp_path, cancel):
    from support.processes import assert_process_exited
    source = ('import pathlib,subprocess,sys,time; '
        'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]); '
        'pathlib.Path("child.pid").write_text(str(p.pid)); time.sleep(60)')
    backend = VMBackend(tmp_path)
    receipt = backend.execute('command', [sys.executable, '-c', source], timeout_s=2,
        should_cancel=(lambda: (tmp_path / 'child.pid').exists()) if cancel else None)
    assert receipt.state == ('cancelled' if cancel else 'failed')
    assert receipt.error_code == ('cancelled' if cancel else 'timeout')
    assert_process_exited(int((tmp_path / 'child.pid').read_text()))


def test_vm_cancel_before_spawn_has_no_side_effect(tmp_path):
    receipt = VMBackend(tmp_path).execute('command', [sys.executable, '-c',
        'open("must-not-exist","w").close()'], should_cancel=lambda: True)
    assert receipt.state == 'cancelled' and not (tmp_path / 'must-not-exist').exists()


def test_vm_allows_explicit_shell_argv(tmp_path):
    argv = [os.environ['COMSPEC'], '/c', 'echo VM_SHELL_OK'] if os.name == 'nt' else ['sh', '-c', 'printf VM_SHELL_OK']
    receipt = VMBackend(tmp_path).execute('command', argv)
    assert receipt.state == 'succeeded' and receipt.result['output'].strip() == 'VM_SHELL_OK'


@pytest.mark.parametrize('crash_phase', [None, 'receipt', 'ready'])
def test_signed_vm_http_failure_returns_to_model_without_replaying_command(tmp_path, monkeypatch, crash_phase):
    from hub.application.run_worker_service import LocalRunWorkerService
    from hub.platform.providers.base import ModelResponse
    from tests.test_native_run_checkpoints import Crash
    from tests.test_platform_node_http_e2e import FlaskTransport
    from tests.test_platform_run_worker import OWNER, _app, _catalog, _queued

    app = _app(tmp_path)
    repo = _catalog(app, tmp_path)
    repo.upsert_node(OWNER, {'node_id': 'vm', 'capabilities': {'workspace.exec': True}})
    repo.update_defaults(OWNER, {'model_profile_id': 'local', 'workspace_id': 'home', 'execution_node_id': 'vm'}, 1)
    run = _queued(app)
    delivery = app.extensions['fleet']['services']['platform_delivery']
    delivery.signing_key = PRIVATE
    credential = repo.provision_node_credential(OWNER, 'vm')['credential']
    node = NodeRuntime(NodeRuntimeConfig(node_id='vm', credential=credential, public_key=PUBLIC,
        hub_url='https://hub.example', workspace_root=tmp_path / 'vm-work', workspace_id='home',
        journal_path=tmp_path / 'node' / 'commands.db', execution_mode='vm_direct',
        capabilities=frozenset({'workspace.exec'})), transport=FlaskTransport(app.test_client()))
    calls = []

    class Provider:
        def complete(self, messages, tools, **kwargs):
            calls.append(json.loads(json.dumps(messages)))
            if len(calls) == 1:
                return ModelResponse(kind='tool_call', tool='workspace.exec', tool_call_id='failed-command',
                    arguments={'argv': [sys.executable, '-c',
                        'from pathlib import Path; import sys; p=Path("once"); p.write_text(p.read_text()+"x" if p.exists() else "x"); print("fix-the-command",file=sys.stderr); sys.exit(7)']})
            if len(calls) == 2:
                result = messages[-1]['result']
                assert result['returncode'] == 7 and result['error_code'] == 'process_failed'
                assert 'fix-the-command' in result['output']
                assert messages[-1]['tool_call_id'] == 'failed-command'
                return ModelResponse(kind='tool_call', tool='workspace.exec', arguments={
                    'argv': [sys.executable, '-c', 'print("VM_FIXED")']})
            assert messages[-1]['result']['output'].strip() == 'VM_FIXED'
            return ModelResponse(kind='final', text='命令已修正。')

    def worker():
        return LocalRunWorkerService(repo, app.extensions['fleet']['services']['run_events'],
            provider_factory=lambda _: Provider(), remote_execution_enabled=True, remote_delivery=delivery)

    save = repo.save_runtime_checkpoint
    crashed = []

    def crash_once(*args, **kwargs):
        value = save(*args, **kwargs)
        if crash_phase and args[2]['phase'] == crash_phase and not crashed:
            crashed.append(True)
            raise Crash()
        return value

    try:
        assert worker().run_once(OWNER)['state'] == 'waiting_node'
        assert node.poll_once()['ok']
        monkeypatch.setattr(repo, 'save_runtime_checkpoint', crash_once)
        with repo._connect() as conn:
            conn.execute('UPDATE runs SET runtime_resume_at=0 WHERE run_id=?', (run['run_id'],))
        if crash_phase:
            with pytest.raises(Crash):
                worker().run_once(OWNER)
            with repo._connect() as conn:
                conn.execute('UPDATE runs SET lease_expires_at=0 WHERE run_id=?', (run['run_id'],))
        assert worker().run_once(OWNER)['state'] == 'waiting_node'
        assert node.poll_once()['ok']
        with repo._connect() as conn:
            conn.execute('UPDATE runs SET runtime_resume_at=0 WHERE run_id=?', (run['run_id'],))
        assert worker().run_once(OWNER)['state'] == 'succeeded'
        assert len(calls) == 3
        assert (tmp_path / 'vm-work' / 'once').read_text() == 'x'
        assert delivery.repository.get(run['run_id'] + ':step:1')['attempt'] == 1
        public = app.test_client().get('/api/platform/v1/runs/' + run['run_id']).json
        segments = [s for s in public['progress']['segments'] if s['kind'] == 'tool']
        assert [s['state'] for s in segments] == ['failed', 'succeeded']
    finally:
        node.close_all()
