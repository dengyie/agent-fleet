"""Signed provider actions reuse control delivery without acquiring a fake session."""
from datetime import datetime, timezone
import sys
import time
from pathlib import Path

from fleet_core.control import canonical_json_bytes, generate_ed25519_keypair, sign_payload
from tools.supervisor.control_client import ControlClient, NonceStore
from tools.supervisor.supervisor import Supervisor


ACCOUNT = 'pa_' + 'a' * 32
OTHER = 'pa_' + 'b' * 32


def wire(private, *, nonce='first'):
    iso = lambda value: datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')
    now = time.time()
    body = {'command_id': 'cmd_' + '1' * 32, 'action': 'manage_provider_account',
            'target': {'machine_id': 'node', 'session_id': ACCOUNT, 'attempt_id': None},
            'payload': {'account_id': ACCOUNT, 'revision': 1, 'operation': 'create', 'owner_digest': 'a' * 64},
            'issued_at': iso(now), 'expires_at': iso(now + 300),
            'nonce': nonce, 'reason_code': 'operator_requested'}
    body['signature'] = sign_payload(private, canonical_json_bytes(body))
    return body


def test_signed_account_command_has_no_session_dependency_and_replays_receipt(tmp_path):
    private, public = generate_ed25519_keypair()
    executed, receipts = [], []
    class Accounts:
        def execute(self, command, transport, headers):
            executed.append(command['payload'])
            return 'succeeded', 'provider_account_created'
    def transport(path, data, headers):
        receipts.append(data)
        return 200, {'ok': True}
    client = ControlClient(hub_url='https://fixture.invalid', machine_id='node', credential='credential',
        hub_public_key=public, public_supervisor=Supervisor(tmp_path / 'sessions', 'node'),
        nonce_store=NonceStore(tmp_path / 'nonces'), transport=transport, provider_accounts=Accounts())
    command = wire(private)
    client._handle_command(command)
    client._handle_command(command)
    assert len(executed) == 1
    assert [r['status'] for r in receipts] == ['succeeded', 'succeeded']
    assert receipts[0] == receipts[1]


def test_unsigned_or_old_node_account_commands_never_execute(tmp_path):
    private, public = generate_ed25519_keypair()
    receipts = []
    client = ControlClient(hub_url='https://fixture.invalid', machine_id='node', credential='credential',
        hub_public_key=public, public_supervisor=Supervisor(tmp_path / 'sessions', 'node'),
        nonce_store=NonceStore(tmp_path / 'nonces'),
        transport=lambda p, d, h: (receipts.append(d) or (200, {'ok': True})))
    command = wire(private)
    client._handle_command(command)
    assert receipts[-1]['reason'] == 'provider_accounts_unavailable'
    altered = wire(private, nonce='different')
    altered['command_id'] = 'cmd_' + '2' * 32
    client._handle_command(altered)
    assert receipts[-1]['reason'] == 'invalid_signature'


def test_broken_account_reports_do_not_block_unrelated_control_poll(tmp_path):
    _, public = generate_ed25519_keypair()
    paths = []
    class BrokenAccounts:
        def tick(self, transport, headers):
            raise ValueError('private store details')
    def transport(path, data, headers):
        paths.append(path)
        return 200, {'ok': True, 'commands': []}
    client = ControlClient(hub_url='https://fixture.invalid', machine_id='node', credential='credential',
        hub_public_key=public, public_supervisor=Supervisor(tmp_path / 'sessions', 'node'),
        nonce_store=NonceStore(tmp_path / 'nonces'), transport=transport, provider_accounts=BrokenAccounts())
    assert client.poll_once() == {'ok': True, 'commands': 0}
    assert paths == ['/api/supervisor/poll']


def test_stop_provider_account_is_scoped_to_its_managed_group(tmp_path):
    supervisor = Supervisor(tmp_path / 'sessions', 'node')
    selected = supervisor.launch(agent='agy', command=[sys.executable, '-c', 'import time; time.sleep(30)'],
        cwd=str(tmp_path), env_allowlist={}, capability_manifest={'provider_account_id': ACCOUNT})
    other = supervisor.launch(agent='agy', command=[sys.executable, '-c', 'import time; time.sleep(30)'],
        cwd=str(tmp_path), env_allowlist={}, capability_manifest={'provider_account_id': OTHER})
    try:
        peer = Supervisor(tmp_path / 'sessions', 'node')
        assert peer.stop_provider_account(ACCOUNT)
        assert supervisor._ops.proc_poll(supervisor._entries[other.session_id].handle) is None
        assert peer.get(selected.session_id).state == 'terminated'
    finally:
        supervisor.terminate_session(selected.session_id, grace_s=.1)
        supervisor.terminate_session(other.session_id, grace_s=.1)


def test_provider_capability_is_bounded_in_public_report():
    from report_schema import sanitize_agent_state
    assert sanitize_agent_state('agy', {'installed': True,
        'provider_accounts': {'schema_version': 1, 'credential': 'hidden'}}) == {
        'installed': True, 'provider_accounts': {'schema_version': 1}}
    assert 'provider_accounts' not in sanitize_agent_state('agy', {'provider_accounts': {'schema_version': True}})
    assert 'provider_accounts' not in sanitize_agent_state('codex', {'provider_accounts': {'schema_version': 1}})


def test_actual_workers_hold_account_leases_and_observe_only_their_generation(tmp_path):
    from tools.providers.agy_accounts import AgyAccounts
    from tools.adapters.agy import AgyAdapter
    from test_provider_account_e2e import eventually
    accounts = AgyAccounts(tmp_path / 'accounts')
    supervisor = Supervisor(tmp_path / 'sessions', 'node')
    peer = Path(__file__).parent / 'fixtures/providers/agy_account_peer.py'
    try:
        for identifier, session in ((ACCOUNT, 'worker_a'), (OTHER, 'worker_b')):
            accounts.apply(identifier, 'create', 1, owner_digest='a' * 64)
            accounts.apply(identifier, 'login', 2, owner_digest='a' * 64)
            accounts.set_state(identifier, 2, 'ready')
            (accounts.profile(identifier) / 'fixture-credential.json').write_text('fixture', encoding='utf-8')
            adapter = AgyAdapter(command=[sys.executable, str(peer)])
            adapter.bind_account(accounts.root, identifier, 'a' * 64)
            supervisor.launch(agent='agy', command=adapter.build_argv('hold'), cwd=str(tmp_path),
                env_allowlist={}, session_id=session, capability_manifest={'provider_account_id': identifier})
            trace = accounts.profile(identifier) / 'fixture-trace.jsonl'
            eventually(lambda: trace.read_text(encoding='utf-8') if trace.exists() else '',
                lambda text: 'session/prompt' in text)
        assert not accounts.quiesce(ACCOUNT, timeout=0)
        accounts.apply(ACCOUNT, 'disable', 3, owner_digest='a' * 64)
        result = supervisor.wait_session('worker_a', timeout_s=5)
        assert result.exit_code != 0
        assert accounts.quiesce(ACCOUNT, timeout=1)
        assert supervisor._ops.proc_poll(supervisor._entries['worker_b'].handle) is None
        assert accounts.read(OTHER)['state'] == 'ready'
    finally:
        for session in list(supervisor._entries):
            supervisor.terminate_session(session, grace_s=.1)
