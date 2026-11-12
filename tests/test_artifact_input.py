"""Hub input grants stream pinned bytes through real SQLite leases and slots."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from fleet_core.control import generate_ed25519_keypair
from hub.application.artifact_input_service import ArtifactInputService
from hub.domain.artifact_receive import ArtifactReceiveError, RegisteredNodeInputs
from hub.infrastructure.artifact_input_repository import ArtifactInputRepository
from hub.infrastructure.artifact_transfer_repository import ArtifactTransferRepository
from hub.infrastructure.command_repository import CommandRepository
from hub.platform.artifacts import ArtifactStore
from test_workflow_coordinator import Setup, make_setup
from test_workflow_repository import OWNER, WORKSPACE


@dataclass
class InputFixture:
    setup: Setup
    artifacts: ArtifactStore
    repository: ArtifactInputRepository
    service: ArtifactInputService
    command: dict[str, Any]
    data: bytes
    headers: dict[str, str]

    def content(self, **changes: str):
        return self.service.content(self.command["grant_id"], **(self.headers | changes))


@pytest.fixture
def inputs(tmp_path: Path) -> InputFixture:
    data = b"frozen source\n" * 15000
    source = tmp_path / "source.tar"
    source.write_bytes(data)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    manifest = artifacts.put_file(OWNER, WORKSPACE, source)
    setup = make_setup(tmp_path, source={key: manifest[key] for key in ("artifact_id", "sha256", "size")})
    setup.platform.upsert_node(OWNER, {"node_id": "node-a", "label": "Input target"})
    secret = "i" * 40
    setup.platform.provision_node_credential(OWNER, "node-a", secret=secret)
    fingerprint = setup.platform.authenticate_node_credential("node-a", secret)["credential_fingerprint"]
    private, public = generate_ed25519_keypair()
    commands = CommandRepository(setup.repository.db_path, clock=setup.clock)
    commands.init()
    ArtifactTransferRepository(setup.repository.db_path, command_public_key=public, clock=setup.clock).init()
    repository = ArtifactInputRepository(setup.repository, commands, signing_key=private, public_key=public)
    target = RegisteredNodeInputs(OWNER, WORKSPACE, "node-a", (("source", "source", ".tar"),))
    service = ArtifactInputService(repository, artifacts, targets=(target,), clock=setup.clock)
    claim = setup.repository.claim_due(OWNER, worker_id="input-coordinator", now=setup.clock(), limit=1)[0]
    assert service.prepare(claim, expected_revision=1, resolved=setup.registrations[0].resolved,
                           generation=1, target_key="node-a") is False
    command = commands.claim_for_node("node-a", "input-worker", owner_id=OWNER, lease_s=60)[0]
    return InputFixture(setup, artifacts, repository, service, command, data, {
        "command_id": command["command_id"], "owner": OWNER, "node": "node-a", "worker": "input-worker",
        "credential_fingerprint": fingerprint,
    })


def test_prepare_persists_one_signed_grant_without_fabricating_a_node_receipt(inputs: InputFixture) -> None:
    steps = inputs.setup.repository.current_steps(OWNER, "workflow-a")
    assert steps[0].state == "waiting_node"
    assert inputs.command["signature"]
    assert inputs.command["arguments"]["artifact_id"]
    with inputs.setup.repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM platform_commands WHERE action='artifact.receive'").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM workflow_dispatches").fetchone()[0] == 0


def test_content_uses_bounded_verified_reads_and_preserves_the_original_reference(
    inputs: InputFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_full_read(*args, **kwargs):
        pytest.fail("streaming input must not allocate a full artifact")

    monkeypatch.setattr(inputs.artifacts, "read", no_full_read)
    with inputs.content() as (request, stream):
        chunks = list(stream)
    assert request.artifact_id == inputs.command["arguments"]["artifact_id"]
    assert max(map(len, chunks)) <= 65536
    assert b"".join(chunks) == inputs.data


@pytest.mark.parametrize("changes", [
    {"worker": "other-worker"}, {"node": "other-node"},
    {"owner": "other@example.test"}, {"command_id": "unrelated-command"},
])
def test_unrelated_lease_cannot_open_input_bytes(
    inputs: InputFixture, monkeypatch: pytest.MonkeyPatch, changes: dict[str, str],
) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("unrelated command must be rejected before opening artifact bytes")

    monkeypatch.setattr(inputs.artifacts, "open_verified", forbidden)
    with pytest.raises(ArtifactReceiveError):
        with inputs.content(**changes):
            pytest.fail("unrelated input lease was admitted")


def test_per_node_stream_slots_release_when_the_caller_closes_early(inputs: InputFixture) -> None:
    with ExitStack() as stack:
        stack.enter_context(inputs.content())
        stack.enter_context(inputs.content())
        with pytest.raises(ArtifactReceiveError, match="transfer_concurrency_limit"):
            with inputs.content():
                pytest.fail("third active stream for a node was admitted")
    with inputs.content() as (_request, stream):
        assert b"".join(stream) == inputs.data


@pytest.mark.parametrize("revocation", ["credential", "workflow", "lease"])
def test_stream_stops_when_original_authorization_changes(inputs: InputFixture, revocation: str) -> None:
    with inputs.content() as (_request, stream):
        assert next(stream) == inputs.data[:65536]
        with inputs.setup.repository.transaction() as connection:
            if revocation == "credential":
                connection.execute("UPDATE node_credentials SET enabled=0 WHERE node_id='node-a'")
            elif revocation == "workflow":
                connection.execute("UPDATE workflow_runs SET cancel_requested=1 WHERE workflow_id='workflow-a'")
            else:
                connection.execute("UPDATE platform_commands SET lease_until=? WHERE command_id=?",
                                   (inputs.setup.clock() - 1, inputs.command["command_id"]))
        with pytest.raises(ArtifactReceiveError):
            next(stream)


def test_corrupted_content_cannot_be_streamed_and_does_not_leak_a_slot(inputs: InputFixture) -> None:
    path = inputs.artifacts.root / inputs.command["arguments"]["artifact_id"] / "content"
    path.write_bytes(b"corrupted")
    with pytest.raises(ArtifactReceiveError, match="receive_not_found"):
        with inputs.content():
            pytest.fail("corrupted artifact was opened")
    path.write_bytes(inputs.data)
    with inputs.content() as (_request, stream):
        assert b"".join(stream) == inputs.data
