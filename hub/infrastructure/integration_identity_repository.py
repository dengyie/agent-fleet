"""Persistent integration authority in the original Workflow transactions."""
from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import Literal

from hub.domain.integration_administration import IntegrationIdentityKey, IntegrationIdentityRecord, identity_page
from hub.domain.integration_identity import (
    IntegrationIdentity, IntegrationIdentityError, parse_integration_identity, token_digest,
)
from hub.domain.workflow_errors import WorkflowPersistenceError
from .integration_identity_schema import STATEMENTS, inspect_integration_schema
from .workflow_repository import WorkflowRepository, WorkflowRepositoryError, _id, _time
from .workflow_schema import inspect_schema

_COLUMNS = "owner_id,integration_id,token_digest,definition_snapshot,definition_digest,created_at,revoked_at,restore_disabled"


def _decode(row: sqlite3.Row) -> IntegrationIdentity:
    try:
        if type(row["definition_snapshot"]) is not bytes:
            raise IntegrationIdentityError("integration_snapshot_invalid")
        identity = parse_integration_identity(row["definition_snapshot"])
        if (identity.owner_id, identity.integration_id, identity.token_digest, identity.definition_digest) != (
            row["owner_id"], row["integration_id"], row["token_digest"], row["definition_digest"],
        ):
            raise IntegrationIdentityError("integration_snapshot_invalid")
        created = _time(row["created_at"])
        revoked = row["revoked_at"]
        if revoked is not None and _time(revoked) < created:
            raise IntegrationIdentityError("integration_snapshot_invalid")
        if type(row["restore_disabled"]) is not int or row["restore_disabled"] not in (0, 1):
            raise IntegrationIdentityError("integration_snapshot_invalid")
        return replace(identity, revoked=revoked is not None or row["restore_disabled"] == 1)
    except (IntegrationIdentityError, WorkflowPersistenceError, TypeError, ValueError, OverflowError) as exc:
        raise IntegrationIdentityError("integration_snapshot_invalid") from exc


class IntegrationIdentityRepository:
    def __init__(self, workflows: WorkflowRepository) -> None:
        self.workflows = workflows

    def init(self) -> None:
        with self.workflows.transaction() as connection:
            if inspect_schema(connection) is None:
                raise WorkflowRepositoryError("workflow_schema_unavailable")
            if not inspect_integration_schema(connection):
                for statement in STATEMENTS:
                    connection.execute(statement)
                connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=max(minimum_reader_version,4) WHERE singleton=1")
        # Validate history under a WAL read snapshot, without holding the one
        # SQLite writer for work proportional to the number of identities.
        with self.workflows.transaction(write=False) as connection:
            self._require_schema(connection)
            self.validate_snapshot(connection)

    def register(self, identity: IntegrationIdentity, *, now: float) -> IntegrationIdentity:
        if type(identity) is not IntegrationIdentity:
            raise IntegrationIdentityError("invalid_integration_identity")
        now = _time(now)
        with self.workflows.transaction() as connection:
            self._require_schema(connection)
            previous = connection.execute(f"SELECT {_COLUMNS} FROM workflow_integration_identities WHERE owner_id=? AND integration_id=?",
                (identity.owner_id, identity.integration_id)).fetchone()
            if previous is not None:
                saved = _decode(previous)
                if saved.snapshot != identity.snapshot:
                    raise IntegrationIdentityError("integration_registration_conflict")
                return saved
            if connection.execute("SELECT 1 FROM workflow_integration_identities WHERE token_digest=?", (identity.token_digest,)).fetchone() is not None:
                raise IntegrationIdentityError("integration_token_conflict")
            connection.execute("INSERT INTO workflow_integration_identities(" + _COLUMNS + ") VALUES(?,?,?,?,?,?,?,0)",
                (identity.owner_id, identity.integration_id, identity.token_digest, identity.snapshot, identity.definition_digest,
                 now, now if identity.revoked else None))
            return identity

    def list_page(self, after: IntegrationIdentityKey | None = None, limit: int = 8) -> tuple[tuple[IntegrationIdentityRecord, ...], IntegrationIdentityKey | None]:
        identity_page(after, limit)
        clause = "" if after is None else "WHERE (owner_id,integration_id)>(?,?)"
        parameters: tuple[object, ...] = () if after is None else (after.owner_id, after.integration_id)
        with self.workflows.transaction(write=False) as connection:
            self._require_schema(connection)
            rows = connection.execute(f"SELECT {_COLUMNS} FROM workflow_integration_identities {clause} "
                "ORDER BY owner_id,integration_id LIMIT ?", (*parameters, limit + 1)).fetchall()
            records = tuple(self._record(row) for row in rows)
        cursor = IntegrationIdentityKey(records[limit - 1].identity.owner_id,
            records[limit - 1].identity.integration_id) if len(records) > limit else None
        return records[:limit], cursor

    def get_record(self, owner: str, integration_id: str) -> IntegrationIdentityRecord:
        IntegrationIdentityKey(owner, integration_id)
        with self.workflows.transaction(write=False) as connection:
            self._require_schema(connection)
            row = connection.execute(f"SELECT {_COLUMNS} FROM workflow_integration_identities WHERE owner_id=? AND integration_id=?",
                (owner, integration_id)).fetchone()
            if row is None:
                raise IntegrationIdentityError("integration_not_found")
            return self._record(row)

    @staticmethod
    def _record(row: sqlite3.Row) -> IntegrationIdentityRecord:
        return IntegrationIdentityRecord(_decode(row), row["created_at"], row["revoked_at"], row["restore_disabled"] == 1)

    def revoke(self, owner: str, integration_id: str, *, expected_digest: str, now: float,
               expected_state: Literal["active"] | None = None) -> IntegrationIdentity:
        _id(owner)
        _id(integration_id)
        now = _time(now)
        if expected_state is not None and (type(expected_state) is not str or expected_state != "active"):
            raise IntegrationIdentityError("invalid_identity_command")
        with self.workflows.transaction() as connection:
            self._require_schema(connection)
            row = connection.execute(f"SELECT {_COLUMNS} FROM workflow_integration_identities WHERE owner_id=? AND integration_id=?",
                (owner, integration_id)).fetchone()
            if row is None:
                raise IntegrationIdentityError("integration_not_found")
            identity = _decode(row)
            if type(expected_digest) is not str or identity.definition_digest != expected_digest:
                raise IntegrationIdentityError("integration_revision_conflict")
            if expected_state is not None and identity.revoked:
                raise IntegrationIdentityError("integration_state_changed")
            if now < row["created_at"]:
                raise IntegrationIdentityError("invalid_time")
            connection.execute("UPDATE workflow_integration_identities SET revoked_at=coalesce(revoked_at,?) WHERE owner_id=? AND integration_id=?",
                (now, owner, integration_id))
            return replace(identity, revoked=True)

    def authenticate(self, token: str) -> IntegrationIdentity | None:
        try:
            digest = token_digest(token)
        except IntegrationIdentityError:
            return None
        try:
            with self.workflows.transaction(write=False) as connection:
                self._require_schema(connection)
                row = connection.execute(f"SELECT {_COLUMNS} FROM workflow_integration_identities WHERE token_digest=?", (digest,)).fetchone()
                if row is None:
                    return None
                identity = _decode(row)
                return identity if identity.authenticate(token) else None
        except WorkflowPersistenceError as exc:
            raise IntegrationIdentityError("integration_store_unavailable") from exc

    @staticmethod
    def _require_schema(connection: sqlite3.Connection) -> None:
        if inspect_schema(connection) is None or not inspect_integration_schema(connection):
            raise WorkflowRepositoryError("integration_schema_unavailable")

    @staticmethod
    def require_active(connection: sqlite3.Connection, identity: IntegrationIdentity, *, scope: str) -> IntegrationIdentity:
        IntegrationIdentityRepository._require_schema(connection)
        if type(identity) is not IntegrationIdentity:
            raise IntegrationIdentityError("invalid_integration_identity")
        row = connection.execute(f"SELECT {_COLUMNS} FROM workflow_integration_identities WHERE owner_id=? AND integration_id=?",
            (identity.owner_id, identity.integration_id)).fetchone()
        if row is None:
            raise IntegrationIdentityError("integration_revoked")
        current = _decode(row)
        current.require(scope)
        if identity.revoked or current.snapshot != identity.snapshot:
            raise IntegrationIdentityError("integration_revoked")
        return current

    @staticmethod
    def validate_snapshot(connection: sqlite3.Connection) -> None:
        if not inspect_integration_schema(connection):
            return
        connection.row_factory = sqlite3.Row
        after: int | None = None
        while True:
            clause, parameters = ("", ()) if after is None else ("WHERE rowid>?", (after,))
            rows = connection.execute(f"SELECT rowid AS identity_rowid,{_COLUMNS} FROM workflow_integration_identities "
                f"{clause} ORDER BY rowid LIMIT 128", parameters).fetchall()
            if not rows:
                return
            for row in rows:
                _decode(row)
            after = rows[-1]["identity_rowid"]
