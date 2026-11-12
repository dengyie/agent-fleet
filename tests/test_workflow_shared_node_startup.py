"""Ordinary Hub manifest composition retains one physical slot across workspaces."""
from __future__ import annotations

from dataclasses import replace
import json
import multiprocessing
import os
from pathlib import Path

import pytest

from fleet_core.control import generate_ed25519_keypair
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.capability import CapabilityDefinition, NodePolicy
from hub.domain.workflow_contract import parse_definition, resolve_contract
from hub.domain.workflow_contract_types import AuthorizedInputs, CapabilityRequirement, CatalogRef
from hub.infrastructure.capability_repository import CapabilityRepository
from hub.infrastructure.platform_db import PlatformRepository
from hub.infrastructure.workflow_repository import WorkflowRepository
from test_capability_router import offer_payload, requirements
from test_workflow_repository import OWNER, registered
from test_workflow_startup import registration

WORKSPACES = ('workspace-a', 'workspace-b')
CAP = CatalogRef('coding.test', 1)


def setup_manifest(root: Path, *, workspaces=WORKSPACES):
    private, _public = generate_ed25519_keypair()
    config = FleetConfig.from_root(root, platform_enabled=True, platform_workflows_enabled=True,
        platform_workflow_registration_file=root / 'manifest.json', platform_workflow_dispatch_enabled=True,
        platform_worker_enabled=True, platform_worker_scheduler_enabled=True, platform_remote_execution_enabled=True,
        platform_capability_routing_enabled=True, platform_artifact_transfer_enabled=True,
        platform_command_signing_raw=private, project_whitelist={'mac-local': ['agent-fleet']}, serve_frontend=False)
    platform = PlatformRepository(config.platform_db); platform.init()
    platform.upsert_node(OWNER, {'node_id': 'node-a'})
    platform.provision_node_credential(OWNER, 'node-a', secret='n' * 32)
    cap = CapabilityDefinition('coding.test', 1, 'fixed-tests', 'in-v1', 'out-v1', b'{"type":"object"}',
        b'{"type":"object"}', 'workspace_read', 'manual_only', ('operator',), 3600, 65536)
    rows = []
    for workspace in workspaces:
        source = root / workspace; source.mkdir()
        platform.upsert_workspace(OWNER, {'workspace_id': workspace, 'root_path': str(source)})
        row = registration(OWNER, workspace)['coding_delivery'][0]
        for step in ('develop', 'review', 'test'):
            conversation = workspace + '-' + step
            row[step]['conversation_id'] = conversation
            platform.create_conversation(OWNER, conversation, title=conversation, workspace_id=workspace)
        row['test']['remote'] = {'node_id': 'node-a', 'capability': cap.payload(),
            'architecture': 'x86_64', 'software': {'python': '3.11.2'}, 'residency_alias': 'private'}
        rows.append(row)
    value = {'schema_version': 1, 'coding_delivery': rows}
    save(config, value)
    return config, platform, value


def save(config, value):
    config.platform_workflow_registration_file.write_text(json.dumps(value), encoding='utf-8')


def app_for(config):
    return create_app(config, repositories={'workflow_clock': lambda: 100.0})


def policy(app):
    workflows = app.extensions['fleet']['repositories']['workflows']
    with workflows.transaction(write=False) as connection:
        rows = connection.execute('SELECT snapshot FROM capability_node_policies').fetchall()
    assert len(rows) == 1
    return NodePolicy.from_snapshot(bytes(rows[0][0]))


def report(capabilities, platform, *, now=100):
    fingerprint = platform.authenticate_node_credential('node-a', 'n' * 32)['credential_fingerprint']
    raw = json.dumps(offer_payload(slots=4, software={'python': '3.11.2'},
        capabilities=[{'id': CAP.id, 'revision': 1}], local_allowed_capabilities=[{'id': CAP.id, 'revision': 1}])).encode()
    capabilities.record_offer(OWNER, 'node-a', raw, credential_fingerprint=fingerprint, now=now)


def selection(workspace, digest='a' * 64):
    return requirements(workspace_id=workspace, template=CatalogRef('coding-delivery', 1), adapter_id='fixed-tests',
        capabilities=(CapabilityRequirement(CAP.id, (1,)),), software=(('python', '3.11.2'),), input_digest=digest)


@pytest.mark.parametrize('reverse', [False, True])
def test_shared_node_startup_keeps_workspace_scoped_targets_and_one_policy(tmp_path, reverse):
    config, platform, value = setup_manifest(tmp_path)
    if reverse: value['coding_delivery'].reverse(); save(config, value)
    app = app_for(config); fleet = app.extensions['fleet']; services = fleet['services']
    node = policy(app)
    assert node.workspaces == WORKSPACES and node.slot_limit == 1
    assert node.residency_aliases == ('private',)
    coordinator = services['workflow_coordinator']
    assert len(coordinator.dispatchers) == 6
    assert set(services['artifact_inputs'].targets) == {(OWNER, ws, 'node-a') for ws in WORKSPACES}
    for ws in WORKSPACES:
        target = coordinator.dispatchers[(OWNER, ws, 'node-a')].executor.target
        assert target.workspace_id == ws and target.conversation_id == ws + '-test'
        assert services['artifact_inputs'].targets[(OWNER, ws, 'node-a')].aliases == (('source', 'source', '.tar'),)
    capabilities = fleet['repositories']['capabilities']; report(capabilities, platform)
    assert all(capabilities.select(selection(ws), now=101).binding.node_id == 'node-a' for ws in WORKSPACES)
    platform.upsert_workspace(OWNER, {'workspace_id': 'unregistered-workspace', 'root_path': str(tmp_path)})
    assert capabilities.select(selection('unregistered-workspace'), now=101).exclusions[0].reason_code == 'workspace_forbidden'
    assert capabilities.select(replace(selection(WORKSPACES[0]), owner_id='other@example.test'), now=101).binding is None
    # Registration order on restart cannot replace one workspace with another.
    value['coding_delivery'].reverse(); save(config, value)
    restarted = app_for(config)
    assert policy(restarted).snapshot == node.snapshot
    fresh = restarted.extensions['fleet']['repositories']['capabilities']
    assert fresh.select(selection(WORKSPACES[0]), now=101).exclusions[0].reason_code == 'offer_stale'
    report(fresh, platform, now=101)
    assert all(fresh.select(selection(ws), now=102).binding for ws in WORKSPACES)


@pytest.mark.parametrize('conflict', ['residency', 'capability'])
def test_shared_node_conflict_does_not_publish_partial_registration(tmp_path, conflict):
    config, _platform, value = setup_manifest(tmp_path)
    one = {'schema_version': 1, 'coding_delivery': value['coding_delivery'][:1]}; save(config, one)
    app = app_for(config); original = policy(app).snapshot
    if conflict == 'residency': value['coding_delivery'][1]['test']['remote']['residency_alias'] = 'different-residency'
    else: value['coding_delivery'][1]['test']['remote']['capability']['max_active_seconds'] = 7200
    save(config, value)
    with pytest.raises(ValueError, match='workflow_registration_(remote_node_policy|capability)_conflict'):
        app_for(config)
    assert policy(app).snapshot == original


def test_shared_node_preserves_each_workspace_software_selection(tmp_path):
    config, platform, value = setup_manifest(tmp_path)
    value['coding_delivery'][1]['test']['remote']['software'] = {'python': '3.12.7'}
    save(config, value)
    app = app_for(config); fleet = app.extensions['fleet']
    capabilities = fleet['repositories']['capabilities']; report(capabilities, platform)
    routes = {route.workspace_id: route.selection
        for route in fleet['services']['workflow_coordinator'].registrations.values() if hasattr(route, 'selection')}
    assert routes[WORKSPACES[0]].software == (('python', '3.11.2'),)
    assert routes[WORKSPACES[1]].software == (('python', '3.12.7'),)
    assert capabilities.select(routes[WORKSPACES[0]], now=101).binding.node_id == 'node-a'
    assert capabilities.select(routes[WORKSPACES[1]], now=101).exclusions[0].reason_code == 'software_mismatch'


def test_shared_node_supports_the_manifest_workspace_limit(tmp_path):
    workspaces = tuple('workspace-%02d' % number for number in range(32))
    config, _platform, _value = setup_manifest(tmp_path, workspaces=workspaces)
    app = app_for(config); fleet = app.extensions['fleet']
    assert policy(app).workspaces == workspaces
    assert len(fleet['services']['workflow_coordinator'].dispatchers) == 96
    assert len(fleet['services']['artifact_inputs'].targets) == 32
    assert policy(app).slot_limit == 1


def prepare_admissions(app):
    fleet = app.extensions['fleet']; workflows = fleet['repositories']['workflows']
    raw = json.loads(registered().snapshot); raw['template_id'] = 'coding-delivery'
    raw['steps'][0]['contract']['capability_requirements'] = [{'id': CAP.id, 'revisions': [1]}]
    definition = parse_definition(json.dumps(raw).encode())
    for ws in WORKSPACES:
        workflows.create_or_get(OWNER, ws, 'capacity-' + ws, client_token='capacity-' + ws, definition=definition, now=100)
    claims = workflows.claim_due(OWNER, worker_id='capacity-test', now=100, limit=2, lease_seconds=60)
    assert len(claims) == 2
    prepared = []
    for claim in claims:
        resolved = resolve_contract(definition, AuthorizedInputs(OWNER, claim.workspace_id, 'develop', CatalogRef('fixed-tests', 1), ()))
        frozen = workflows.freeze_inputs(claim, step_key='develop', generation=1, resolved=resolved, expected_revision=1, now=101)
        prepared.append((claim, frozen.revision, selection(claim.workspace_id, resolved.digest)))
    return prepared


def admit(capabilities, prepared):
    claim, revision, req = prepared
    return capabilities.admit(claim, step_key='develop', generation=1, expected_revision=revision,
        requirements=req, reserve_tokens=100, reserve_requests=2, now=102)


@pytest.mark.parametrize('held_state', ['held', 'isolated'])
def test_shared_node_uses_one_capacity_ledger_across_workspaces(tmp_path, held_state):
    config, platform, _ = setup_manifest(tmp_path); app = app_for(config)
    capabilities = app.extensions['fleet']['repositories']['capabilities']; report(capabilities, platform)
    prepared = prepare_admissions(app)
    assert admit(capabilities, prepared[0]).intent is not None
    if held_state == 'isolated':
        with capabilities.workflows.transaction() as connection:
            connection.execute("UPDATE capability_reservations SET state='isolated'")
            connection.execute("UPDATE workflow_steps SET state='unknown' WHERE workflow_id=?", (prepared[0][0].workflow_id,))
    second = admit(capabilities, prepared[1])
    assert second.intent is None and second.selection.exclusions[0].reason_code == 'capacity_exhausted'
    assert capabilities.workflows.current_steps(OWNER, prepared[1][0].workflow_id)[0].state == 'waiting_node'
    with capabilities.workflows.transaction(write=False) as connection:
        assert connection.execute('SELECT count(*) FROM capability_step_bindings').fetchone()[0] == 1
        assert connection.execute('SELECT count(*) FROM capability_reservations').fetchone()[0] == 1


def race_admission(path, prepared, ready, proceed, output):
    repository = CapabilityRepository(WorkflowRepository(Path(path)), boot_id='shared-node-race')
    repository.init(); ready.put(True)
    assert proceed.wait(timeout=15)
    result = admit(repository, prepared)
    output.put(result.intent is not None)


def test_shared_node_two_processes_cannot_oversell_different_workspaces(tmp_path):
    config, platform, _ = setup_manifest(tmp_path); app = app_for(config)
    prepared = prepare_admissions(app)
    context = multiprocessing.get_context('spawn')
    ready, output, proceed = context.Queue(), context.Queue(), context.Event()
    processes = [context.Process(target=race_admission, args=(str(config.platform_db), item, ready, proceed, output)) for item in prepared]
    for process in processes: process.start()
    try:
        for _ in processes: assert ready.get(timeout=15)
        capabilities = app.extensions['fleet']['repositories']['capabilities']; report(capabilities, platform, now=102)
        proceed.set()
        results = [output.get(timeout=15) for _ in processes]
        assert sorted(results) == [False, True]
        for process in processes:
            process.join(timeout=15); assert process.exitcode == 0
        with capabilities.workflows.transaction(write=False) as connection:
            rows = connection.execute('SELECT workspace_id,target_key,state FROM capability_reservations').fetchall()
            assert len(rows) == 1 and rows[0]['target_key'] == 'node-a' and rows[0]['state'] == 'held'
    finally:
        proceed.set()
        for process in processes:
            if process.is_alive(): process.terminate()
            process.join(timeout=5)


def test_shared_node_receives_distinct_workspace_sources_with_original_signatures(tmp_path):
    from hub.application.task_service import ApplicationError
    from hub.domain.workflow_contract_types import ArtifactReference, InputBinding
    from tools.platform.artifact_transfer import ArtifactReceiver, InputDirectory
    from test_artifact_receive import ContentTransport

    config, platform, _ = setup_manifest(tmp_path); app = app_for(config)
    fleet = app.extensions['fleet']; services = fleet['services']
    workflows = fleet['repositories']['workflows']; inputs = services['artifact_inputs']
    delivery = services['platform_delivery']; artifacts = services['platform_artifacts']
    # Original command service shares the deterministic workflow clock here.
    delivery.clock = lambda: 100.0
    delivery.repository.clock = lambda: 100.0
    sources, resolved = {}, {}
    for ws in WORKSPACES:
        raw = (ws + ' distinct frozen source\n').encode()
        path = tmp_path / (ws + '.tar'); path.write_bytes(raw)
        artifact = artifacts.put_file(OWNER, ws, path)
        reference = {key: artifact[key] for key in ('artifact_id', 'sha256', 'size')}
        definition = registered(source=reference)
        workflows.create_or_get(OWNER, ws, 'source-' + ws, client_token='source-' + ws, definition=definition, now=100)
        sources[ws] = raw
        resolved[ws] = resolve_contract(definition, AuthorizedInputs(OWNER, ws, 'develop', CatalogRef('cli', 1),
            (InputBinding('source', OWNER, ws, ArtifactReference(**reference)),)))
    claims = workflows.claim_due(OWNER, worker_id='source-coordinator', now=100, limit=2)
    assert len(claims) == 2
    for claim in claims:
        assert inputs.prepare(claim, expected_revision=1, resolved=resolved[claim.workspace_id],
            generation=1, target_key='node-a') is False
    fingerprint = platform.authenticate_node_credential('node-a', 'n' * 32)['credential_fingerprint']
    commands = delivery.poll('node-a', 'source-worker', owner_id=OWNER, credential_fingerprint=fingerprint)
    assert len(commands) == 2 and {c['resource_id'] for c in commands} == set(WORKSPACES)
    assert len({c['grant_id'] for c in commands}) == 2
    received = tmp_path / 'node-inputs'
    journal = tmp_path / 'node-journal'
    if os.name == 'nt':
        from fixtures.workflow.windows_artifact_probe import private_directory
        private_directory(received)
        private_directory(journal)
    else:
        received.mkdir(mode=0o700)
        journal.mkdir(mode=0o700)
    receipts = {}
    for command in commands:
        headers = {'command_id': command['command_id'], 'owner': OWNER, 'node': 'node-a',
            'worker': 'source-worker', 'credential_fingerprint': fingerprint}
        with inputs.content(command['grant_id'], **headers) as (_request, chunks):
            raw = b''.join(chunks)
        assert raw == sources[command['resource_id']]
        transport = ContentTransport(raw)
        receiver = ArtifactReceiver(node_id='node-a', credential='node-a:' + 'n' * 32, worker_id='source-worker',
            hub_url='https://hub.test', public_key=inputs.repository.public_key,
            inputs={'source': InputDirectory(received)}, journal_path=journal / 'receives.db',
            transport=transport, clock=lambda: 100.0)
        receipt = receiver.receive(command)
        assert (received / (command['grant_id'] + '.tar')).read_bytes() == sources[command['resource_id']]
        assert receiver.receive(command) == receipt and len(transport.requests) == 1
        receipts[command['command_id']] = receipt
    first, second = commands
    with pytest.raises(ApplicationError) as mismatch:
        delivery.receipt(first['command_id'], 'succeeded', result=receipts[second['command_id']],
            worker_id='source-worker', owner_id=OWNER, node_id='node-a', credential_fingerprint=fingerprint)
    assert mismatch.value.code == 'invalid_receive_receipt'
    for command in commands:
        delivery.receipt(command['command_id'], 'succeeded', result=receipts[command['command_id']],
            worker_id='source-worker', owner_id=OWNER, node_id='node-a', credential_fingerprint=fingerprint)
    for claim in claims:
        current = workflows.get(OWNER, claim.workflow_id)
        assert inputs.prepare(claim, expected_revision=current.revision, resolved=resolved[claim.workspace_id],
            generation=1, target_key='node-a') is False
        assert workflows.current_steps(OWNER, claim.workflow_id)[0].state == 'ready'
        current = workflows.get(OWNER, claim.workflow_id)
        assert inputs.prepare(claim, expected_revision=current.revision, resolved=resolved[claim.workspace_id],
            generation=1, target_key='node-a') is True
