"""Behavior regressions for production execution, rollback and delivery boundaries."""
from pathlib import Path
from contextlib import closing
import json
import shutil
import os
import sqlite3
import subprocess
import sys
import time
import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.infrastructure.platform_db import PlatformRepository
from hub.domain.control import generate_ed25519_keypair
from hub.domain.platform_command import PlatformCommand
from tools.platform.node_runtime import NodeRuntime, NodeRuntimeConfig
from tools.platform.providers.base import ModelResponse

ROOT = Path(__file__).resolve().parents[1]
OWNER = 'review@example.test'


@pytest.mark.parametrize("platform_enabled", [False, True])
def test_packaged_release_can_start_platform(tmp_path, platform_enabled):
    archive = tmp_path / 'release.tgz'
    subprocess.run(['bash', str(ROOT / 'deploy/package-release.sh'), str(archive)], cwd=ROOT, check=True, capture_output=True)
    extracted = tmp_path / 'release'
    extracted.mkdir()
    subprocess.run(['tar', '-xzf', str(archive), '-C', str(extracted)], check=True)
    script = f"from pathlib import Path; from hub.bootstrap import create_app; from hub.config import FleetConfig; create_app(FleetConfig.from_root(Path.cwd(), platform_enabled={platform_enabled}))"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    env.pop('PYTHONPATH', None)
    result = subprocess.run([sys.executable, '-c', script], cwd=extracted, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def _legacy_append(repo, number):
    # The supported rollback binary's insert contract omits turn_sequence.
    with closing(sqlite3.connect(repo.db_path)) as conn, conn:
        conn.execute("INSERT INTO messages(message_id,conversation_id,owner_id,role,content_ref,content,client_token,created_at) VALUES(?,?,?,?,?,?,?,?)",
                     (f'msg-{number}', 'conv-review', OWNER, 'user', 'inline', f'turn {number}', f'turn-{number}', str(number)))
        conn.execute("INSERT INTO runs(run_id,conversation_id,owner_id,trigger_message_id,state,config_snapshot,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                     (f'run-{number}', 'conv-review', OWNER, f'msg-{number}', 'queued', '{}', number, number))


def test_old_writer_works_after_schema_upgrade_and_code_rollback(tmp_path):
    repo = PlatformRepository(tmp_path / 'platform.db')
    repo.init()
    repo.create_conversation(OWNER, 'conv-review', title='', workspace_id=None)
    # Build the pre-sequence schema; no dependence on private Git history.
    with closing(sqlite3.connect(repo.db_path)) as conn, conn:
        conn.execute('DROP TRIGGER IF EXISTS assign_message_turn_sequence')
        conn.execute('DROP INDEX idx_message_turn_order')
        conn.execute('ALTER TABLE messages DROP COLUMN turn_sequence')
        conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    _legacy_append(repo, 1)
    repo.init()
    repo.append_turn(OWNER, 'conv-review', 'msg-2', 'run-2', text='turn 2', client_token='turn-2', config_snapshot={}, now=2)
    _legacy_append(repo, 3)
    _legacy_append(repo, 4)
    # Older init rewrites version 1 but leaves additive indexes/triggers intact.
    with closing(sqlite3.connect(repo.db_path)) as conn, conn:
        conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    repo.init()
    messages = repo.get_conversation(OWNER, 'conv-review')['messages']
    assert [row['content'] for row in messages] == ['turn 1', 'turn 2', 'turn 3', 'turn 4']
    assert [row['turn_sequence'] for row in messages] == [1, 2, 3, 4]


def test_signed_node_exec_cannot_read_outside_workspace(tmp_path):
    private, public = generate_ed25519_keypair()
    outside = tmp_path / 'outside-workspace.txt'
    outside.write_text('review-outside-sentinel')
    runtime = NodeRuntime(NodeRuntimeConfig(node_id='node-review', credential='node-review:fixture', workspace_root=tmp_path / 'workspace', journal_path=tmp_path / 'journal.db', hub_url='https://hub.invalid', public_key=public, capabilities=frozenset({'workspace.exec'})))
    command = PlatformCommand.create(command_id='cmd-review', target_node='node-review', owner_id=OWNER, action='tool.workspace.exec', resource_id='home', arguments={'argv':['cat',str(outside)]}, expires_at=time.time()+60).signed(private)
    receipt = runtime.client.handle(command.as_dict())
    assert 'review-outside-sentinel' not in str(receipt), receipt
    assert receipt['status'] == 'failed'
    assert receipt['result']['error_code'] == 'sandbox_unavailable'


def test_cancellation_does_not_hide_unknown_remote_write(tmp_path):
    private, public = generate_ed25519_keypair()
    app = create_app(FleetConfig.from_root(tmp_path / 'hub', dev_operator=OWNER, platform_enabled=True, platform_worker_enabled=True, platform_remote_execution_enabled=True, platform_command_signing_raw=private, platform_require_command_signature=True))
    fleet = app.extensions['fleet']
    repo = fleet['platform_repository']
    repo.upsert_model(OWNER, {'profile_id':'local','provider':'deterministic','model':'local'})
    repo.upsert_workspace(OWNER, {'workspace_id':'home','root_path':str(tmp_path/'hub-workspace')})
    repo.create_conversation(OWNER, 'conv-review', title='', workspace_id='home')
    repo.append_turn(OWNER, 'conv-review', 'msg-review', 'run-review', text='write', client_token='turn-review', config_snapshot={'workspace_id':'home','model_profile_id':'local','execution_node_id':'node-review'}, now=time.time())
    runtime = NodeRuntime(NodeRuntimeConfig(node_id='node-review', credential='node-review:fixture', workspace_root=tmp_path/'node-workspace', journal_path=tmp_path/'journal.db', hub_url='https://hub.invalid', public_key=public, capabilities=frozenset({'workspace.write'})))
    worker = fleet['services']['platform_worker']
    class Provider:
        def complete(self, messages, tools, *, request_observer=None):
            return ModelResponse(kind='tool_call', tool='workspace.write', arguments={'path':'published.txt','content':'side effect happened'})
    class LostReceipt:
        def wait_for_receipt(self, command_id, *, timeout_s):
            row = fleet['platform_commands'].get(command_id)
            assert runtime.client.handle(row)['status'] == 'succeeded'
            repo.cancel_run(OWNER, 'run-review', now=time.time())
            return fleet['platform_commands'].mark_unknown(command_id, reason='receipt_timeout')
    worker.provider_factory = lambda profile: Provider()
    worker.remote_waiter = LostReceipt()
    result = worker.run_once(OWNER)
    command = fleet['platform_commands'].get('run-review:step:1')
    assert (tmp_path/'node-workspace/published.txt').read_text() == 'side effect happened'
    assert command['status'] == 'unknown'
    assert result['state'] == 'unknown', {'run_state':result['state'],'command_state':command['status'],'events':repo.list_run_events(OWNER,'run-review')}
    stored_run = repo.get_run(OWNER, 'run-review')
    assert stored_run['state'] == 'unknown'
    assert stored_run['cancel_requested'] is True
    finished = [event for event in repo.list_run_events(OWNER, 'run-review')['events']
                if event['kind'] == 'run_finished']
    assert len(finished) == 1
    assert finished[0]['payload']['state'] == 'unknown'


@pytest.mark.parametrize('answer', ['界' * 22000, '😀' * 18000, '"\\\n\x01' * 8000], ids=['cjk', 'emoji', 'escaped'])
@pytest.mark.parametrize('leased', [True, False])
def test_successful_multibyte_completion_is_persisted(tmp_path, answer, leased):
    from tools.platform.providers.openai_compatible import OpenAICompatibleProvider, TransportResponse
    app = create_app(FleetConfig.from_root(tmp_path, dev_operator=OWNER, platform_enabled=True, platform_worker_enabled=True))
    fleet = app.extensions['fleet']
    repo = fleet['platform_repository']
    repo.upsert_workspace(OWNER, {'workspace_id':'home','root_path':str(tmp_path/'workspace')})
    repo.create_conversation(OWNER, 'conv-review', title='', workspace_id='home')
    repo.append_turn(OWNER, 'conv-review', 'msg-review', 'run-review', text='report', client_token='turn-review', config_snapshot={'workspace_id':'home'}, now=time.time())
    class Transport:
        def request(self, **kwargs):
            return TransportResponse(200, {'content-type':'application/json'}, json.dumps({'choices':[{'message':{'role':'assistant','content':answer},'finish_reason':'stop'}], 'usage':{'prompt_tokens':10,'completion_tokens':22000}}).encode())
    provider = OpenAICompatibleProvider(model='review', endpoint='https://provider.invalid/v1/chat/completions', api_key='fixture', transport=Transport())
    worker = fleet['services']['platform_worker']
    worker.provider_factory = lambda profile: provider
    if leased:
        result = worker.run_once(OWNER)
        assert result['state'] == 'succeeded'
    else:
        from tools.platform.assistant_worker import PersistentAssistantWorker
        from tools.platform.runtime.native import NativeAssistantRuntime
        result = PersistentAssistantWorker(NativeAssistantRuntime(provider, None), fleet['services']['run_events']).execute(
            run_id='run-review', owner_id=OWNER, epoch=1, messages=[], tools=[])
        assert result.state == 'succeeded'
    assert repo.get_run(OWNER, 'run-review')['result_text'] == answer
    events = repo.list_run_events(OWNER, 'run-review')['events']
    finished = next(e for e in events if e['kind'] == 'run_finished')
    assert 'text' not in finished['payload']
    assert len(json.dumps(finished['payload']).encode()) < 64 * 1024
    repo.append_turn(OWNER, 'conv-review', 'msg-next', 'run-next', text='continue', client_token='next', config_snapshot={}, now=time.time())
    context = repo.get_run_execution_context(OWNER, 'run-next')
    assert context['messages'][1]['role'] == 'assistant'
    assert context['messages'][1]['content'] == answer




def test_migration_repairs_zero_sequence_left_by_previous_rollback(tmp_path):
    repo = PlatformRepository(tmp_path / 'platform.db')
    repo.init()
    repo.create_conversation(OWNER, 'conv-review', title='', workspace_id=None)
    for n in range(1, 4):
        repo.append_turn(OWNER, 'conv-review', f'msg-{n}', f'run-{n}', text=f'turn {n}', client_token=f'turn-{n}', config_snapshot={}, now=n)
    with closing(sqlite3.connect(repo.db_path)) as conn, conn:
        conn.execute('DROP TRIGGER assign_message_turn_sequence')
        conn.execute("UPDATE messages SET turn_sequence=0 WHERE message_id='msg-3'")
        conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    repo.init()
    _legacy_append(repo, 4)
    assert [m['content'] for m in repo.get_conversation(OWNER, 'conv-review')['messages']] == [f'turn {n}' for n in range(1, 5)]


def test_database_sequences_serialize_mixed_writer_versions(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    repo = PlatformRepository(tmp_path / 'platform.db')
    repo.init()
    repo.create_conversation(OWNER, 'conv-review', title='', workspace_id=None)
    def write(n):
        if n % 2:
            _legacy_append(repo, n)
        else:
            repo.append_turn(OWNER, 'conv-review', f'msg-{n}', f'run-{n}', text=f'turn {n}', client_token=f'turn-{n}', config_snapshot={}, now=n)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(1, 21)))
    messages = repo.get_conversation(OWNER, 'conv-review')['messages']
    assert [m['turn_sequence'] for m in messages] == list(range(1, 21))
    assert {m['content'] for m in messages} == {f'turn {n}' for n in range(1, 21)}


def test_future_schema_is_rejected_without_migration(tmp_path):
    from hub.infrastructure.platform_db import PlatformRepositoryError
    path = tmp_path / 'platform.db'
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        conn.execute("INSERT INTO meta VALUES('schema_version','999')")
    with pytest.raises(PlatformRepositoryError, match='unsupported_schema_version'):
        PlatformRepository(path).init()
    with closing(sqlite3.connect(path)) as conn, conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '999'
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [('meta',)]






def test_node_launcher_is_immutable_validated_and_used(tmp_path):
    private, public = generate_ed25519_keypair()
    arguments = dict(node_id='node-review', credential='node-review:fixture',
                     workspace_root=tmp_path/'workspace', journal_path=tmp_path/'journal.db',
                     hub_url='https://hub.invalid', public_key=public,
                     capabilities=frozenset({'workspace.exec'}))
    for invalid in ('/bin/sh', [None], [''], ['bad\x00arg']):
        with pytest.raises(ValueError, match='sandbox launcher'):
            NodeRuntimeConfig(**arguments, sandbox_launcher=invalid)
    # A disposable denying launcher proves argv reaches the configured boundary.
    # It does not stand in for validating an actual production isolation profile.
    launcher = tmp_path / 'launcher.py'
    record = tmp_path / 'received.json'
    launcher.write_text(
        'import json, pathlib, sys\n'
        f'pathlib.Path({str(record)!r}).write_text(json.dumps(sys.argv[1:]))\n'
        'sys.exit(23)\n')
    argv = [sys.executable, str(launcher)]
    config = NodeRuntimeConfig(**arguments, sandbox_launcher=argv)
    argv.clear()
    runtime = NodeRuntime(config)
    command = PlatformCommand.create(
        command_id='cmd-launcher', target_node='node-review', action='tool.workspace.exec',
        resource_id='home', arguments={'argv': ['pwd']}, expires_at=time.time()+60).signed(private)
    result = runtime.client.handle(command.as_dict())
    assert result['status'] == 'failed'
    assert result['result']['result']['returncode'] == 23
    assert json.loads(record.read_text()) == ['pwd']
