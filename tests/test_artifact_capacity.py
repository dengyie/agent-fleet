"""Upload reservations and live downloads share one host-wide admission limit."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
import hashlib
import multiprocessing
import os
from pathlib import Path
from typing import Any

import pytest

from hub.domain.artifact_receive import ArtifactReceiveError
from hub.domain.artifact_transfer import ArtifactTransferError
from fleet_core.platform_command import PlatformCommand
from hub.application.artifact_transfer_service import ArtifactTransferService
from hub.infrastructure.artifact_input_repository import ArtifactInputRepository
from hub.infrastructure.artifact_transfer_repository import ArtifactTransferRepository
from hub.infrastructure.command_repository import CommandRepository
from hub.infrastructure.workflow_repository import WorkflowRepository
from hub.platform.artifacts import ArtifactStore
from test_artifact_transfer import TransferFixture, transfer
from test_workflow_dispatch import setup
from test_workflow_repository import OWNER


def inputs(fixture: TransferFixture) -> ArtifactInputRepository:
    commands = CommandRepository(fixture.repository.db_path, clock=fixture.workflow.clock)
    return ArtifactInputRepository(fixture.workflow.repository, commands,
        signing_key=fixture.private_key, public_key=fixture.public_key, limits=fixture.service.limits)


def prepare_second(fixture: TransferFixture) -> str:
    commands = CommandRepository(fixture.repository.db_path, clock=fixture.workflow.clock)
    original = commands.get(fixture.command_id)
    command = PlatformCommand.create(command_id="export-second", target_node="node-a", owner_id=OWNER,
        action="artifact.export", resource_id=original["resource_id"], arguments={"output_key": "second"},
        expires_at=4000, run_id=original["run_id"]).signed(fixture.private_key)
    commands.enqueue(command, idempotency_key=command.command_id)
    commands.claim_for_node("node-a", "node-worker", owner_id=OWNER, lease_s=300)
    return command.command_id


def reserve_second(fixture: TransferFixture) -> Any:
    return fixture.service.reserve(command_id=prepare_second(fixture), owner_id=OWNER, node_id="node-a",
        worker_id="node-worker", output_key="second", name="second.bin", size=7,
        sha256=hashlib.sha256(b"payload").hexdigest())


def test_download_then_upload_cannot_overbook_node(transfer: TransferFixture) -> None:
    repository = inputs(transfer)
    with repository.download_slot(OWNER, "node-a"):
        transfer.reserve()
        with pytest.raises(ArtifactTransferError, match="transfer_concurrency_limit"):
            reserve_second(transfer)
    assert reserve_second(transfer).scope.output_key == "second"


def test_upload_then_download_cannot_overbook_node(transfer: TransferFixture) -> None:
    transfer.reserve()
    repository = inputs(transfer)
    with repository.download_slot(OWNER, "node-a"):
        with pytest.raises(ArtifactReceiveError, match="transfer_concurrency_limit"):
            with repository.download_slot(OWNER, "node-a"):
                pytest.fail("third combined node transfer admitted")
    with repository.download_slot(OWNER, "node-a"):
        assert True


def test_downloads_and_uploads_share_global_limit(transfer: TransferFixture) -> None:
    repository = inputs(transfer)
    with ExitStack() as stack:
        for node in ("node-b", "node-c", "node-d"):
            stack.enter_context(repository.download_slot(OWNER, node))
        transfer.reserve()
        with pytest.raises(ArtifactTransferError, match="transfer_concurrency_limit"):
            reserve_second(transfer)
        with pytest.raises(ArtifactReceiveError, match="transfer_concurrency_limit"):
            with repository.download_slot(OWNER, "node-e"):
                pytest.fail("fifth combined host transfer admitted")


def _race_capacity(database: Path, root: Path, private: bytes, public: bytes, action: str,
                   node: str, barrier: Any, release: Any, sender: Any) -> None:
    try:
        repository = ArtifactTransferRepository(database, command_public_key=public, clock=lambda: 104)
        service = ArtifactTransferService(repository, ArtifactStore(root), clock=lambda: 104)
        receiver = ArtifactInputRepository(WorkflowRepository(database), CommandRepository(database),
            signing_key=private, public_key=public)
        barrier.wait(timeout=20)
        with ExitStack() as stack:
            if action == "upload":
                service.reserve(command_id="export-second", owner_id=OWNER, node_id="node-a",
                    worker_id="node-worker", output_key="second", name="second.bin", size=7,
                    sha256=hashlib.sha256(b"payload").hexdigest())
            else:
                stack.enter_context(receiver.download_slot(OWNER, node))
            sender.send("admitted")
            if not release.wait(20):
                raise RuntimeError("capacity race parent did not release the winner")
    except (ArtifactReceiveError, ArtifactTransferError) as exc:
        sender.send(exc.code)
    finally:
        sender.close()


@pytest.mark.parametrize("scope", ["node", "global"])
def test_upload_and_download_processes_compete_for_one_last_slot(transfer: TransferFixture, scope: str) -> None:
    transfer.reserve()
    prepare_second(transfer)
    context = multiprocessing.get_context("spawn")
    barrier, release = context.Barrier(3), context.Event()
    processes, receivers = [], []
    with ExitStack() as downloads:
        if scope == "global":
            for node in ("node-b", "node-c"):
                downloads.enter_context(inputs(transfer).download_slot(OWNER, node))
        try:
            for action, node in (("upload", "node-a"), ("download", "node-a" if scope == "node" else "node-d")):
                reader, sender = context.Pipe(duplex=False)
                process = context.Process(target=_race_capacity, args=(transfer.repository.db_path,
                    transfer.service.artifacts.root, transfer.private_key, transfer.public_key,
                    action, node, barrier, release, sender))
                process.start()
                sender.close()
                processes.append(process)
                receivers.append(reader)
            barrier.wait(timeout=20)
            results = []
            for reader in receivers:
                assert reader.poll(25), "capacity worker did not report its admission"
                results.append(reader.recv())
            assert sorted(results) == ["admitted", "transfer_concurrency_limit"]
        finally:
            release.set()
            for process in processes:
                process.join(25)
                if process.is_alive():
                    process.terminate()
                    process.join(10)
            for reader in receivers:
                reader.close()
        assert all(process.exitcode == 0 for process in processes)


def _crash_downloads(database: Path, private: bytes, public: bytes, release: Any, sender: Any) -> None:
    repository = ArtifactInputRepository(WorkflowRepository(database), CommandRepository(database),
        signing_key=private, public_key=public)
    with repository.download_slot(OWNER, "node-a"), repository.download_slot(OWNER, "node-a"):
        sender.send("holding-two")
        if not release.wait(20):
            raise RuntimeError("download crash parent did not release the child")
        os._exit(73)


def test_dead_download_process_releases_both_shared_slots(transfer: TransferFixture) -> None:
    context = multiprocessing.get_context("spawn")
    release = context.Event()
    reader, sender = context.Pipe(duplex=False)
    process = context.Process(target=_crash_downloads,
        args=(transfer.repository.db_path, transfer.private_key, transfer.public_key, release, sender))
    process.start()
    sender.close()
    try:
        assert reader.poll(20)
        assert reader.recv() == "holding-two"
        with pytest.raises(ArtifactTransferError, match="transfer_concurrency_limit"):
            transfer.reserve()
        release.set()
        process.join(20)
        assert process.exitcode == 73
        transfer.reserve()
        with inputs(transfer).download_slot(OWNER, "node-a"):
            with pytest.raises(ArtifactTransferError, match="transfer_concurrency_limit"):
                reserve_second(transfer)
    finally:
        release.set()
        process.join(20)
        if process.is_alive():
            process.terminate()
            process.join(10)
        reader.close()


@pytest.mark.parametrize("direction", ["upload-first", "download-first"])
def test_reduced_limits_are_shared_in_both_directions(transfer: TransferFixture, direction: str) -> None:
    transfer.service.limits = replace(transfer.service.limits, global_concurrency=1, per_node_concurrency=1)
    repository = inputs(transfer)
    if direction == "upload-first":
        transfer.reserve()
        with pytest.raises(ArtifactReceiveError, match="transfer_concurrency_limit"):
            with repository.download_slot(OWNER, "node-b"):
                pytest.fail("custom global limit ignored")
        return
    with repository.download_slot(OWNER, "node-b"):
        with pytest.raises(ArtifactTransferError, match="transfer_concurrency_limit"):
            transfer.reserve()
    assert transfer.reserve().state == "reserved"


def test_unknown_upload_keeps_capacity_until_reconciled(transfer: TransferFixture) -> None:
    manifest = transfer.reserve()
    with transfer.repository.transaction(write=True) as connection:
        connection.execute("UPDATE artifact_transfers SET state='unknown' WHERE transfer_id=?", (manifest.transfer_id,))
    with inputs(transfer).download_slot(OWNER, "node-a"):
        with pytest.raises(ArtifactTransferError, match="transfer_concurrency_limit"):
            reserve_second(transfer)


def test_download_pool_is_shared_across_reopened_directories_and_nodes(transfer: TransferFixture) -> None:
    with ExitStack() as slots:
        for node in ("node-a", "node-a", "node-b", "node-c"):
            slots.enter_context(inputs(transfer).download_slot(OWNER, node))
        for node in ("node-a", "node-d"):
            with pytest.raises(ArtifactReceiveError, match="transfer_concurrency_limit"):
                with inputs(transfer).download_slot(OWNER, node):
                    pytest.fail("reopening the capacity directory created a second slot pool")
    with inputs(transfer).download_slot(OWNER, "node-a"):
        assert True


@pytest.mark.parametrize("scope", ["node", "global"])
def test_download_processes_compete_for_one_last_slot(transfer: TransferFixture, scope: str) -> None:
    context = multiprocessing.get_context("spawn")
    barrier, release = context.Barrier(3), context.Event()
    processes, receivers = [], []
    with ExitStack() as held:
        nodes = ("node-a",) if scope == "node" else ("node-b", "node-c", "node-d")
        for node in nodes:
            held.enter_context(inputs(transfer).download_slot(OWNER, node))
        try:
            for node in ("node-a", "node-a" if scope == "node" else "node-e"):
                reader, sender = context.Pipe(duplex=False)
                process = context.Process(target=_race_capacity, args=(transfer.repository.db_path,
                    transfer.service.artifacts.root, transfer.private_key, transfer.public_key,
                    "download", node, barrier, release, sender))
                process.start()
                sender.close()
                processes.append(process)
                receivers.append(reader)
            barrier.wait(timeout=20)
            results = []
            for reader in receivers:
                assert reader.poll(25), "download worker did not report its admission"
                results.append(reader.recv())
            assert sorted(results) == ["admitted", "transfer_concurrency_limit"]
        finally:
            release.set()
            for process in processes:
                process.join(25)
                if process.is_alive():
                    process.terminate()
                    process.join(10)
            for reader in receivers:
                reader.close()
        assert all(process.exitcode == 0 for process in processes)


def test_download_process_death_releases_native_pool(transfer: TransferFixture) -> None:
    context = multiprocessing.get_context("spawn")
    release = context.Event()
    reader, sender = context.Pipe(duplex=False)
    process = context.Process(target=_crash_downloads,
        args=(transfer.repository.db_path, transfer.private_key, transfer.public_key, release, sender))
    process.start()
    sender.close()
    try:
        assert reader.poll(20) and reader.recv() == "holding-two"
        with pytest.raises(ArtifactReceiveError, match="transfer_concurrency_limit"):
            with inputs(transfer).download_slot(OWNER, "node-a"):
                pytest.fail("live child did not hold the physical download pool")
        release.set()
        process.join(20)
        assert process.exitcode == 73
        with inputs(transfer).download_slot(OWNER, "node-a"), inputs(transfer).download_slot(OWNER, "node-a"):
            assert True
    finally:
        release.set()
        process.join(20)
        if process.is_alive():
            process.terminate()
            process.join(10)
        reader.close()
