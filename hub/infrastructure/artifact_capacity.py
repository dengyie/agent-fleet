"""One SQLite admission fence combines durable uploads and live kernel downloads."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
from typing import Iterator

from hub.domain.artifact_receive import ArtifactReceiveError
from hub.domain.artifact_transfer import ArtifactTransferError, TransferLimits
from hub.platform.artifact_lock import ArtifactSlotError, artifact_io_slot_count

ACTIVE_TRANSFER_SQL = "'reserved','uploading','verifying','publishing','unknown'"


class ArtifactCapacityError(ArtifactTransferError):
    """Host-local capacity cannot be established or its last slot is occupied."""


def node_download_key(owner: str, node: str) -> str:
    return "artifact-download-" + hashlib.sha256((owner + "\0" + node).encode("utf-8")).hexdigest()


@contextmanager
def capacity_directory(database: Path) -> Iterator[int]:
    if os.name == "nt":
        from hub.platform import artifact_windows as windows

        try:
            with windows.root_handle(database.parent, private_child=".artifact-capacity") as descriptor:
                yield descriptor
        except (OSError, ArtifactReceiveError, ArtifactSlotError) as exc:
            raise ArtifactCapacityError(getattr(exc, "code", "transfer_slot_unavailable")) from exc
        return
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise ArtifactCapacityError("capability_unavailable")
    descriptor = None
    try:
        descriptor = os.open(database.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise ArtifactCapacityError("transfer_slot_unavailable")
        yield descriptor
    except (OSError, ArtifactSlotError) as exc:
        raise ArtifactCapacityError(getattr(exc, "code", "transfer_slot_unavailable")) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def check_capacity(connection: sqlite3.Connection, directory: int, *, owner: str, node: str,
                   limits: TransferLimits) -> None:
    """Use one indexed aggregate and six nonblocking kernel slot probes."""
    if not connection.in_transaction:
        raise ArtifactCapacityError("transfer_store")
    uploads = connection.execute(
        "SELECT count(*),COALESCE(sum(owner_id=? AND node_id=?),0) FROM artifact_transfers "
        f"WHERE state IN ({ACTIVE_TRANSFER_SQL})", (owner, node)).fetchone()
    try:
        total = uploads[0] + artifact_io_slot_count(directory, "artifact-download-global", 4)
        per_node = uploads[1] + artifact_io_slot_count(directory, node_download_key(owner, node), 2)
    except ArtifactSlotError as exc:
        raise ArtifactCapacityError(exc.code) from exc
    if total >= limits.global_concurrency or per_node >= limits.per_node_concurrency:
        raise ArtifactCapacityError("transfer_concurrency_limit")
