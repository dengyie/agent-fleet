"""Create-only workspace/conversation preparation in the original platform store."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from hub.domain.workflow_identity import validate_reference
from hub.infrastructure.platform_db import MAX_READER_VERSION, PlatformRepository
from hub.infrastructure.platform_db_base import PlatformRepositoryError
from hub.infrastructure.sqlite import connect
from platform_schema import validate_id, validate_owner_id, validate_workspace


class WorkflowSetupError(ValueError):
    pass


def _inspect(connection, owner, workspace, conversations):
    saved = connection.execute("SELECT * FROM workspaces WHERE owner_id=? AND workspace_id=?",
        (owner, workspace["workspace_id"])).fetchone()
    if saved is not None:
        previous_root = Path(saved["root_path"]).expanduser()
        if (not saved["enabled"] or saved["backend"] != "directory" or not previous_root.is_absolute()
                or previous_root.resolve() != Path(workspace["root_path"])):
            raise WorkflowSetupError("workflow_setup_workspace_conflict")
    missing = []
    for phase, conversation_id in conversations:
        row = connection.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
        if row is None:
            missing.append((phase, conversation_id))
        elif (row["owner_id"] != owner or row["workspace_id"] != workspace["workspace_id"]
                or row["archived_at"] is not None):
            raise WorkflowSetupError("workflow_setup_conversation_conflict")
    return saved is None, missing


def _report(*, apply, workspace_missing, missing):
    return {"ok": True, "mode": "apply" if apply else "preview",
        "create": {"workspaces": int(workspace_missing), "conversations": len(missing)},
        "existing": {"workspaces": int(not workspace_missing), "conversations": 3 - len(missing)}}


def prepare_workflow_workspace(repository: PlatformRepository, *, owner_id: str, workspace_id: str,
                               root_path: Path, conversation_ids: tuple[str, str, str], apply: bool = False):
    owner = validate_owner_id(owner_id)
    validate_reference(owner)
    if type(conversation_ids) is not tuple or len(conversation_ids) != 3:
        raise WorkflowSetupError("workflow_setup_requires_three_conversations")
    for value in conversation_ids:
        validate_id(value, "conversation_id")
    if len(set(conversation_ids)) != 3:
        raise WorkflowSetupError("workflow_setup_requires_distinct_conversations")
    root = Path(root_path).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise WorkflowSetupError("workflow_setup_root_unavailable")
    workspace = validate_workspace({"workspace_id": workspace_id, "root_path": str(root.resolve(strict=True))})
    conversations = tuple(zip(("develop", "review", "test"), conversation_ids))
    if not apply and not repository.db_path.exists():
        return _report(apply=False, workspace_missing=True, missing=conversations)
    if apply:
        repository.init()
    try:
        connection = (repository._connect() if apply else connect(
            repository.db_path.resolve().as_uri() + "?mode=ro", uri=True, wal=False))
        with closing(connection):
            connection.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
            try:
                version = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                if version is None or int(version["value"]) > MAX_READER_VERSION:
                    raise WorkflowSetupError("workflow_setup_unsupported_store")
                workspace_missing, missing = _inspect(connection, owner, workspace, conversations)
                if apply:
                    if workspace_missing:
                        connection.execute("INSERT INTO workspaces(owner_id,workspace_id,name,backend,root_path,default_node_id,enabled) "
                            "VALUES(?,?,?,?,?,NULL,1)", (owner, workspace_id, workspace["name"], "directory", workspace["root_path"]))
                    for phase, conversation_id in missing:
                        connection.execute("INSERT INTO conversations(conversation_id,owner_id,title,workspace_id,overrides,revision) "
                            "VALUES(?,?,?,?,?,0)", (conversation_id, owner, "Workflow " + phase, workspace_id, "{}"))
                report = _report(apply=apply, workspace_missing=workspace_missing, missing=missing)
                connection.execute("COMMIT" if apply else "ROLLBACK")
                return report
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
    except sqlite3.Error as exc:
        raise PlatformRepositoryError("workflow_setup_store") from exc
