"""Transactional transfer admission, command authorization and publication ledger."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from hub.domain.artifact_transfer import (
    ArtifactTransferError, EXPORT_COMMAND_ACTIONS, TransferLimits, TransferManifest,
    TransferScope, TRANSFER_STATES, validate_reservation,
)
from fleet_core.platform_command import args_hash, verify_command
from hub.domain.artifact_export import ArtifactExportError, ExportRequest, validate_export_receipt
from hub.domain.workflow_contract import parse_contract
from .artifact_capacity import ACTIVE_TRANSFER_SQL, capacity_directory, check_capacity
from .platform_db import node_credential_is_current


class ArtifactTransferRepositoryError(ArtifactTransferError):
    """Stable storage/authorization error retaining its original cause."""


TRANSFER_TABLES = frozenset({"artifact_transfer_schema_meta", "artifact_transfers", "artifact_transfer_parts"})
TRANSFER_COLUMNS = frozenset({
    "transfer_id", "idempotency_key", "artifact_id", "command_id", "command_worker_id",
    "owner_id", "node_id", "workspace_id", "workflow_id", "step_key", "generation", "output_key",
    "name", "size", "sha256", "content_type", "state", "created_at", "expires_at",
    "absolute_expires_at", "updated_at", "failure_code", "staging_bytes", "publication_bytes", "input_digest",
})
MAX_TRANSFERS = 10_000
ACTIVE_SQL = ACTIVE_TRANSFER_SQL


def inspect_transfer_schema(connection: sqlite3.Connection) -> bool:
    """Recognize the complete additive schema even when the runtime gate is off."""
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'artifact_transfer%' LIMIT 10")}
    if not tables:
        return False
    if tables != TRANSFER_TABLES:
        raise ArtifactTransferRepositoryError("transfer_schema_unknown")
    meta = connection.execute("SELECT schema_version,minimum_reader_version FROM artifact_transfer_schema_meta LIMIT 2").fetchall()
    if len(meta) != 1 or tuple(meta[0]) != (1, 1):
        raise ArtifactTransferRepositoryError("transfer_schema_unknown")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(artifact_transfers)")}
    parts = {row[1] for row in connection.execute("PRAGMA table_info(artifact_transfer_parts)")}
    if columns != TRANSFER_COLUMNS or parts != {"transfer_id", "offset", "length", "sha256", "created_at"}:
        raise ArtifactTransferRepositoryError("transfer_schema_unknown")
    return True


class ArtifactTransferRepository:
    def __init__(self, db_path: Path, *, command_public_key: bytes | None = None, clock=time.time) -> None:
        self.db_path = Path(db_path)
        self.command_public_key = command_public_key
        self.clock = clock

    @contextmanager
    def transaction(self, *, write: bool = False, verify_schema: bool = True) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            connection = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if verify_schema and not inspect_transfer_schema(connection):
                raise ArtifactTransferRepositoryError("transfer_schema_unknown")
            yield connection
            connection.commit()
        except ArtifactTransferError:
            raise
        except (sqlite3.Error, ValueError, TypeError, KeyError, IndexError) as exc:
            raise ArtifactTransferRepositoryError("transfer_store") from exc
        finally:
            if connection is not None:
                connection.close()

    def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction(write=True, verify_schema=False) as connection:
            if inspect_transfer_schema(connection):
                connection.execute("CREATE INDEX IF NOT EXISTS artifact_transfers_capacity ON artifact_transfers(state,owner_id,node_id)")
                return
            statements = (
                "CREATE TABLE artifact_transfer_schema_meta(singleton INTEGER PRIMARY KEY CHECK(singleton=1),schema_version INTEGER NOT NULL,minimum_reader_version INTEGER NOT NULL)",
                "INSERT INTO artifact_transfer_schema_meta VALUES(1,1,1)",
                """CREATE TABLE artifact_transfers (
                    transfer_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                    artifact_id TEXT NOT NULL UNIQUE, command_id TEXT NOT NULL, command_worker_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL, node_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL, step_key TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>0),
                    output_key TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL CHECK(size BETWEEN 0 AND 268435456),
                    sha256 TEXT NOT NULL CHECK(length(sha256)=64), content_type TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('reserved','uploading','verifying','publishing','ready','failed','quarantined','unknown')),
                    created_at REAL NOT NULL, expires_at REAL NOT NULL, absolute_expires_at REAL NOT NULL,
                    updated_at REAL NOT NULL, failure_code TEXT, input_digest TEXT NOT NULL CHECK(length(input_digest)=64),
                    staging_bytes INTEGER NOT NULL DEFAULT 0 CHECK(staging_bytes>=0),
                    publication_bytes INTEGER NOT NULL DEFAULT 0 CHECK(publication_bytes>=0),
                    UNIQUE(command_id, output_key),
                    FOREIGN KEY(owner_id,workspace_id,workflow_id,step_key,generation)
                        REFERENCES workflow_steps(owner_id,workspace_id,workflow_id,step_key,generation)
                )""",
                "CREATE INDEX artifact_transfers_active_node ON artifact_transfers(node_id,state,expires_at)",
                "CREATE INDEX artifact_transfers_capacity ON artifact_transfers(state,owner_id,node_id)",
                "CREATE INDEX artifact_transfers_workspace ON artifact_transfers(owner_id,workspace_id,state)",
                "CREATE INDEX artifact_transfers_workflow ON artifact_transfers(owner_id,workflow_id,state)",
                """CREATE TABLE artifact_transfer_parts (
                    transfer_id TEXT NOT NULL, offset INTEGER NOT NULL CHECK(offset>=0),
                    length INTEGER NOT NULL CHECK(length BETWEEN 1 AND 1048576),
                    sha256 TEXT NOT NULL CHECK(length(sha256)=64), created_at REAL NOT NULL,
                    PRIMARY KEY(transfer_id,offset), FOREIGN KEY(transfer_id) REFERENCES artifact_transfers(transfer_id)
                )""",
            )
            for statement in statements:
                connection.execute(statement)

    @staticmethod
    def _manifest(row: sqlite3.Row) -> TransferManifest:
        scope = TransferScope(row["command_id"], row["owner_id"], row["node_id"], row["workspace_id"],
                              row["workflow_id"], row["step_key"], row["generation"], row["output_key"])
        return TransferManifest(row["transfer_id"], row["artifact_id"], scope, row["name"], row["size"],
                                row["sha256"], row["state"], 1024 * 1024, row["expires_at"])

    def _scope_command(self, connection: sqlite3.Connection, *, command_id: str, owner_id: str,
                       node_id: str, worker_id: str, output_key: str, now: float,
                       existing_artifact: str | None = None) -> tuple[sqlite3.Row, TransferScope, int, str, float]:
        command = connection.execute("SELECT * FROM platform_commands WHERE command_id=?", (command_id,)).fetchone()
        if command is None or (command["owner_id"], command["target_node"]) != (owner_id, node_id):
            raise ArtifactTransferRepositoryError("command_not_found")
        arguments = json.loads(command["arguments"])
        if command["action"] not in EXPORT_COMMAND_ACTIONS or type(arguments) is not dict or arguments.get("output_key") != output_key:
            raise ArtifactTransferRepositoryError("artifact_command_required")
        envelope = dict(command) | {"arguments": arguments}
        if (self.command_public_key is None or args_hash(arguments) != command["args_hash"]
                or not verify_command(envelope, self.command_public_key)):
            raise ArtifactTransferRepositoryError("artifact_command_unavailable")
        if (command["status"] not in {"leased", "accepted", "running"} or command["lease_owner"] != worker_id
                or command["lease_until"] is None or float(command["lease_until"]) <= now
                or float(command["expires_at"]) <= now):
            raise ArtifactTransferRepositoryError("artifact_command_unavailable")
        rows = connection.execute(
            "SELECT d.owner_id,d.workspace_id,d.workflow_id,d.step_key,d.generation,d.target_key,d.token,d.payload_hash,d.stage,"
            "s.is_current,s.state,s.contract_snapshot,s.resolved_digest,s.active_deadline,w.execution_state,w.cancel_requested,"
            "r.config_snapshot,r.state AS run_state,r.cancel_requested AS run_cancel_requested FROM workflow_dispatches d "
            "JOIN workflow_steps s USING(owner_id,workspace_id,workflow_id,step_key,generation) "
            "JOIN workflow_runs w USING(owner_id,workspace_id,workflow_id) "
            "JOIN runs r ON r.owner_id=d.owner_id AND r.run_id=d.binding_run_id "
            "JOIN workspaces ws ON ws.owner_id=d.owner_id AND ws.workspace_id=d.workspace_id AND ws.enabled=1 "
            "WHERE d.owner_id=? AND d.workspace_id=? AND d.binding_run_id=? LIMIT 2",
            (owner_id, command["resource_id"], command["run_id"]),
        ).fetchall()
        if len(rows) != 1:
            raise ArtifactTransferRepositoryError("artifact_scope_unavailable")
        row = rows[0]
        admission = connection.execute("SELECT admission_mode FROM workflow_schema_meta WHERE singleton=1").fetchone()
        if (row["stage"] != "bound" or row["is_current"] != 1 or row["target_key"] != node_id
                or admission is None or admission[0] == "frozen"
                or row["execution_state"] not in {"running", "waiting_external"}
                or row["state"] not in {"running", "waiting_external"} or row["cancel_requested"] or row["run_cancel_requested"]
                or row["active_deadline"] is None or float(row["active_deadline"]) <= now
                or row["run_state"] not in {"waiting_task", "queued", "running"}):
            raise ArtifactTransferRepositoryError("artifact_command_unavailable")
        snapshot = json.loads(row["config_snapshot"])
        marker = snapshot.get("workflow_dispatch") if type(snapshot) is dict else None
        if type(marker) is not dict or (marker.get("token"), marker.get("payload_hash")) != (row["token"], row["payload_hash"]):
            raise ArtifactTransferRepositoryError("artifact_scope_unavailable")
        specs = {item.output_key: item for item in parse_contract(bytes(row["contract_snapshot"])).output_specs}
        spec = specs.get(output_key)
        if spec is None or spec.kind != "artifact":
            raise ArtifactTransferRepositoryError("artifact_output_forbidden")
        ref = connection.execute(
            "SELECT artifact_id FROM workflow_artifact_refs WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=? AND output_key=?",
            (owner_id, row["workflow_id"], row["step_key"], row["generation"], "output:" + output_key),
        ).fetchone()
        if ref is not None and ref[0] != existing_artifact:
            raise ArtifactTransferRepositoryError("artifact_output_already_bound")
        scope = TransferScope(command_id, owner_id, node_id, command["resource_id"], row["workflow_id"],
                              row["step_key"], row["generation"], output_key)
        deadline = min(float(command["lease_until"]), float(command["expires_at"]), float(row["active_deadline"]))
        return command, scope, spec.max_bytes, row["resolved_digest"], deadline

    def authorize(self, manifest: TransferManifest, *, worker_id: str, now: float) -> float:
        with self.transaction() as connection:
            _command, scope, maximum, digest, deadline = self._scope_command(connection, command_id=manifest.scope.command_id,
                owner_id=manifest.scope.owner_id, node_id=manifest.scope.node_id, worker_id=worker_id,
                output_key=manifest.scope.output_key, now=now)
            row = connection.execute("SELECT input_digest,command_worker_id FROM artifact_transfers WHERE transfer_id=?", (manifest.transfer_id,)).fetchone()
            if scope != manifest.scope or manifest.size > maximum or row is None or tuple(row) != (digest, worker_id):
                raise ArtifactTransferRepositoryError("transfer_conflict")
            return deadline

    def command_scope(self, *, command_id: str, owner_id: str, node_id: str, worker_id: str,
                      output_key: str, now: float) -> TransferScope:
        with self.transaction() as connection:
            return self._scope_command(connection, command_id=command_id, owner_id=owner_id,
                node_id=node_id, worker_id=worker_id, output_key=output_key, now=now)[1]

    def receipt_guard(self, connection: sqlite3.Connection, command: dict[str, Any], status: str,
                      result: dict[str, Any], worker: str | None, now: float,
                      credential_fingerprint: str | None) -> None:
        if not node_credential_is_current(connection, owner=command["owner_id"], node=command["target_node"],
                                          fingerprint=credential_fingerprint):
            raise ArtifactExportError("export_credential_revoked")
        request = ExportRequest.parse(command, node_id=command["target_node"], public_key=self.command_public_key,
            now=now, allow_expired=True)
        if command["status"] in {"succeeded", "failed", "unknown", "expired"}:
            if (status, result) != (command["status"], command["result"]):
                raise ArtifactExportError("invalid_export_receipt")
            return
        if status != "succeeded":
            return
        row = connection.execute("SELECT artifact_id,size,sha256,state,owner_id,node_id,workspace_id,command_worker_id FROM artifact_transfers WHERE command_id=? AND output_key=? LIMIT 1",
            (request.command_id, request.output_key)).fetchone()
        if (row is None or row["state"] != "ready"
                or (row["owner_id"], row["node_id"], row["workspace_id"], row["command_worker_id"]) != (
                    command["owner_id"], command["target_node"], request.workspace_id, worker)):
            raise ArtifactExportError("invalid_export_receipt")
        validate_export_receipt(request, result, artifact_id=row["artifact_id"], size=row["size"], sha256=row["sha256"])

    @staticmethod
    def _charge(connection: sqlite3.Connection, *, owner_id: str, workspace_id: str, workflow_id: str | None = None) -> int:
        suffix = " AND workflow_id=?" if workflow_id is not None else ""
        parameters = (owner_id, workspace_id, workflow_id) if workflow_id is not None else (owner_id, workspace_id)
        charge = (f"CASE WHEN state='ready' OR state IN ({ACTIVE_SQL}) THEN size ELSE 0 END" if workflow_id is not None else
            f"CASE WHEN state='ready' THEN size+staging_bytes WHEN state IN ({ACTIVE_SQL}) THEN max(size,staging_bytes)+publication_bytes ELSE staging_bytes+publication_bytes END")
        return connection.execute("SELECT COALESCE(sum(" + charge + "),0) FROM artifact_transfers WHERE owner_id=? AND workspace_id=?" + suffix, parameters).fetchone()[0]

    def reserve(self, *, command_id: str, owner_id: str, node_id: str, worker_id: str,
                output_key: str, name: str, size: int, sha256: str, transfer_id: str, artifact_id: str,
                content_type: str, limits: TransferLimits, now: float, legacy_ready_bytes: int = 0) -> TransferManifest:
        validate_reservation(size=size, sha256=sha256, name=name, limits=limits)
        with self.transaction(write=True) as connection:
            prior = connection.execute("SELECT * FROM artifact_transfers WHERE command_id=? AND output_key=?", (command_id, output_key)).fetchone()
            if prior is not None:
                if (prior["owner_id"], prior["node_id"], prior["command_worker_id"]) != (owner_id, node_id, worker_id):
                    raise ArtifactTransferRepositoryError("transfer_not_found")
                fields = ("owner_id", "node_id", "command_worker_id", "name", "size", "sha256", "content_type")
                if tuple(prior[key] for key in fields) != (owner_id, node_id, worker_id, name, size, sha256, content_type):
                    raise ArtifactTransferRepositoryError("transfer_conflict")
                if prior["state"] == "ready":
                    return self._manifest(prior)
            command, scope, maximum, input_digest, _deadline = self._scope_command(connection, command_id=command_id,
                owner_id=owner_id, node_id=node_id, worker_id=worker_id, output_key=output_key, now=now)
            if prior is not None:
                if self._manifest(prior).scope != scope or prior["input_digest"] != input_digest:
                    raise ArtifactTransferRepositoryError("transfer_conflict")
                return self._manifest(prior)
            if size > maximum:
                raise ArtifactTransferRepositoryError("artifact_output_too_large")
            with capacity_directory(self.db_path) as directory:
                check_capacity(connection, directory, owner=owner_id, node=node_id, limits=limits)
            if legacy_ready_bytes + self._charge(connection, owner_id=owner_id, workspace_id=scope.workspace_id) + size > limits.workspace_quota:
                raise ArtifactTransferRepositoryError("workspace_quota_exceeded")
            if self._charge(connection, owner_id=owner_id, workspace_id=scope.workspace_id, workflow_id=scope.workflow_id) + size > limits.workflow_budget:
                raise ArtifactTransferRepositoryError("workflow_artifact_budget_exceeded")
            expires = min(now + limits.reservation_ttl, float(command["expires_at"]))
            absolute = min(now + limits.absolute_ttl, float(command["expires_at"]))
            values = dict(transfer_id=transfer_id, artifact_id=artifact_id, command_id=command_id, command_worker_id=worker_id,
                owner_id=owner_id, node_id=node_id, workspace_id=scope.workspace_id, workflow_id=scope.workflow_id,
                step_key=scope.step_key, generation=scope.generation, output_key=output_key, name=name, size=size,
                sha256=sha256, content_type=content_type, state="reserved", created_at=now, updated_at=now,
                expires_at=expires, absolute_expires_at=absolute, input_digest=input_digest,
                idempotency_key=hashlib.sha256((command_id + "\0" + output_key).encode()).hexdigest())
            connection.execute(f"INSERT INTO artifact_transfers({','.join(values)}) VALUES({','.join('?' for _ in values)})", tuple(values.values()))
            return self._manifest(connection.execute("SELECT * FROM artifact_transfers WHERE transfer_id=?", (transfer_id,)).fetchone())

    def reservation(self, transfer_id: str) -> dict[str, object] | None:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM artifact_transfers WHERE transfer_id=?", (transfer_id,)).fetchone()
            return dict(row) if row is not None else None

    def find_by_command(self, command_id: str, output_key: str) -> dict[str, object] | None:
        with self.transaction() as connection:
            row = connection.execute("SELECT transfer_id,artifact_id FROM artifact_transfers WHERE command_id=? AND output_key=?", (command_id, output_key)).fetchone()
            return dict(row) if row is not None else None

    def get(self, transfer_id: str, *, owner_id: str, node_id: str) -> tuple[TransferManifest, tuple[tuple[int, int, str], ...]] | None:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM artifact_transfers WHERE transfer_id=? AND owner_id=? AND node_id=?", (transfer_id, owner_id, node_id)).fetchone()
            if row is None:
                return None
            parts = connection.execute("SELECT offset,length,sha256 FROM artifact_transfer_parts WHERE transfer_id=? ORDER BY offset LIMIT 257", (transfer_id,)).fetchall()
            if len(parts) > 256:
                raise ArtifactTransferRepositoryError("transfer_corrupt")
            return self._manifest(row), tuple(tuple(item) for item in parts)

    def record_part(self, manifest: TransferManifest, *, worker_id: str, offset: int, length: int,
                    sha256: str, now: float, limits: TransferLimits) -> tuple[int, int, str]:
        with self.transaction(write=True) as connection:
            self._scope_command(connection, command_id=manifest.scope.command_id, owner_id=manifest.scope.owner_id,
                node_id=manifest.scope.node_id, worker_id=worker_id, output_key=manifest.scope.output_key, now=now)
            row = connection.execute("SELECT state,expires_at,absolute_expires_at FROM artifact_transfers WHERE transfer_id=?", (manifest.transfer_id,)).fetchone()
            if row["expires_at"] <= now or row["absolute_expires_at"] <= now:
                raise ArtifactTransferRepositoryError("reservation_expired")
            if row["state"] not in {"reserved", "uploading"}:
                raise ArtifactTransferRepositoryError("transfer_state_conflict")
            prior = connection.execute("SELECT offset,length,sha256 FROM artifact_transfer_parts WHERE transfer_id=? AND offset=?", (manifest.transfer_id, offset)).fetchone()
            if prior is not None and tuple(prior) != (offset, length, sha256):
                raise ArtifactTransferRepositoryError("part_conflict")
            connection.execute("INSERT OR IGNORE INTO artifact_transfer_parts VALUES(?,?,?,?,?)", (manifest.transfer_id, offset, length, sha256, now))
            expires = min(now + limits.reservation_ttl, row["absolute_expires_at"])
            connection.execute("UPDATE artifact_transfers SET state='uploading',expires_at=?,updated_at=? WHERE transfer_id=?", (expires, now, manifest.transfer_id))
            return offset, length, sha256

    def begin_verify(self, manifest: TransferManifest, *, now: float, limits: TransferLimits, legacy_ready_bytes: int) -> None:
        with self.transaction(write=True) as connection:
            row = connection.execute("SELECT state FROM artifact_transfers WHERE transfer_id=?", (manifest.transfer_id,)).fetchone()
            if row is None or row[0] not in {"reserved", "uploading", "verifying", "publishing"}:
                raise ArtifactTransferRepositoryError("transfer_state_conflict")
            connection.execute("UPDATE artifact_transfers SET state='verifying',publication_bytes=size,updated_at=? WHERE transfer_id=?", (now, manifest.transfer_id))
            if legacy_ready_bytes + self._charge(connection, owner_id=manifest.scope.owner_id, workspace_id=manifest.scope.workspace_id) > limits.workspace_quota:
                raise ArtifactTransferRepositoryError("workspace_quota_exceeded")

    def publishing(self, transfer_id: str, *, now: float) -> None:
        with self.transaction(write=True) as connection:
            cursor = connection.execute("UPDATE artifact_transfers SET state='publishing',updated_at=? WHERE transfer_id=? AND state IN ('verifying','publishing','unknown')", (now, transfer_id))
            if cursor.rowcount != 1:
                raise ArtifactTransferRepositoryError("transfer_state_conflict")

    def publish_ready(self, manifest: TransferManifest, *, now: float, credential_fingerprint: str | None = None) -> None:
        with self.transaction(write=True) as connection:
            row = connection.execute("SELECT * FROM artifact_transfers WHERE transfer_id=?", (manifest.transfer_id,)).fetchone()
            if row is None or row["state"] not in {"publishing", "ready"}:
                raise ArtifactTransferRepositoryError("transfer_state_conflict")
            if self._manifest(row).scope != manifest.scope:
                raise ArtifactTransferRepositoryError("transfer_conflict")
            ref_key = (manifest.scope.owner_id, manifest.scope.workflow_id, manifest.scope.step_key,
                       manifest.scope.generation, "output:" + manifest.scope.output_key)
            prior = connection.execute("SELECT artifact_id,sha256,size FROM workflow_artifact_refs WHERE owner_id=? AND workflow_id=? AND step_key=? AND generation=? AND output_key=?", ref_key).fetchone()
            if prior is not None and tuple(prior) != (manifest.artifact_id, manifest.sha256, manifest.size):
                raise ArtifactTransferRepositoryError("transfer_reference_conflict")
            if row["state"] == "ready" and prior is None:
                raise ArtifactTransferRepositoryError("transfer_reference_conflict")
            if row["state"] != "ready":
                if credential_fingerprint is not None and not node_credential_is_current(connection,
                        owner=manifest.scope.owner_id, node=manifest.scope.node_id, fingerprint=credential_fingerprint):
                    raise ArtifactTransferRepositoryError("export_credential_revoked")
                if min(row["expires_at"], row["absolute_expires_at"]) <= now:
                    raise ArtifactTransferRepositoryError("reservation_expired")
                self._scope_command(connection, command_id=manifest.scope.command_id, owner_id=manifest.scope.owner_id,
                    node_id=manifest.scope.node_id, worker_id=row["command_worker_id"], output_key=manifest.scope.output_key,
                    now=now, existing_artifact=manifest.artifact_id)
            connection.execute("INSERT OR IGNORE INTO workflow_artifact_refs VALUES(?,?,?,?,?,?,?,?,?)",
                (manifest.scope.owner_id, manifest.scope.workspace_id, manifest.scope.workflow_id, manifest.scope.step_key,
                 manifest.scope.generation, "output:" + manifest.scope.output_key, manifest.artifact_id, manifest.sha256, manifest.size))
            connection.execute("UPDATE artifact_transfers SET state='ready',failure_code=NULL,publication_bytes=0,updated_at=? WHERE transfer_id=?", (now, manifest.transfer_id))

    def reconcile_staging_bytes(self, transfer_id: str, amount: int, *, publication_bytes: int = 0, now: float) -> None:
        if type(amount) is not int or amount < 0 or type(publication_bytes) is not int or publication_bytes < 0:
            raise ArtifactTransferRepositoryError("invalid_staging_size")
        with self.transaction(write=True) as connection:
            connection.execute("UPDATE artifact_transfers SET staging_bytes=?,publication_bytes=?,updated_at=? WHERE transfer_id=?", (amount, publication_bytes, now, transfer_id))

    def state(self, transfer_id: str, state: str, *, code: str | None, now: float) -> None:
        if state not in TRANSFER_STATES - {"ready"}:
            raise ArtifactTransferRepositoryError("invalid_transfer_state")
        with self.transaction(write=True) as connection:
            allowed = TRANSFER_STATES if state == "quarantined" else TRANSFER_STATES - {"ready", "quarantined", "failed"}
            placeholders = ",".join("?" for _ in allowed)
            connection.execute(f"UPDATE artifact_transfers SET state=?,failure_code=?,updated_at=? WHERE transfer_id=? AND state IN ({placeholders})", (state, code, now, transfer_id, *allowed))

    def manifests(self, *, limit: int = MAX_TRANSFERS) -> tuple[TransferManifest, ...]:
        with self.transaction() as connection:
            rows = connection.execute("SELECT * FROM artifact_transfers ORDER BY transfer_id LIMIT ?", (limit + 1,)).fetchall()
            if len(rows) > limit:
                raise ArtifactTransferRepositoryError("transfer_scan_limit")
            return tuple(self._manifest(row) for row in rows)

    def staging_bytes(self, *, owner_id: str, workspace_id: str) -> int:
        with self.transaction() as connection:
            return connection.execute("SELECT COALESCE(sum(staging_bytes+publication_bytes),0) FROM artifact_transfers WHERE owner_id=? AND workspace_id=?", (owner_id, workspace_id)).fetchone()[0]

    def referenced_ids(self, artifact_ids: tuple[str, ...]) -> frozenset[str]:
        """Inspect references only for the bounded transfer cleanup candidates."""
        if len(artifact_ids) > MAX_TRANSFERS:
            raise ArtifactTransferRepositoryError("transfer_scan_limit")
        with self.transaction() as connection:
            from hub.infrastructure.browser_capture_gc import BrowserCaptureGcError, BrowserCaptureGcRepository
            try:
                gc = BrowserCaptureGcRepository(self.db_path, self.db_path.parent)
                surfaces = gc._reference_surfaces(connection)
                connection.execute("CREATE TEMP TABLE transfer_gc_candidates (artifact_id TEXT PRIMARY KEY) WITHOUT ROWID")
                connection.executemany("INSERT OR IGNORE INTO temp.transfer_gc_candidates VALUES(?)",
                                       ((artifact_id,) for artifact_id in artifact_ids))
                return frozenset(gc._referenced(connection, surfaces, "transfer_gc_candidates"))
            except BrowserCaptureGcError as exc:
                raise ArtifactTransferRepositoryError("transfer_reference_scan_failed") from exc

    def cleanup_inventory(self) -> tuple[dict[str, object], ...]:
        with self.transaction() as connection:
            rows = connection.execute("SELECT transfer_id,artifact_id,state,staging_bytes,publication_bytes FROM artifact_transfers WHERE state IN ('failed','quarantined','unknown') ORDER BY transfer_id LIMIT ?", (MAX_TRANSFERS + 1,)).fetchall()
            if len(rows) > MAX_TRANSFERS:
                raise ArtifactTransferRepositoryError("transfer_scan_limit")
            return tuple(dict(row) for row in rows)
