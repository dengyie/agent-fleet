"""Model profiles, workspaces, nodes and owner defaults for ``PlatformRepository``."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from hub.infrastructure.platform_db_base import PlatformRepositoryError, _decode, _json
from platform_schema import (
    PlatformValidationError,
    validate_id,
    validate_model_profile,
    validate_node,
    validate_owner_id,
    validate_workspace,
)


def catalog_digest(row):
    if row is None:
        return ""
    value = {key: item for key, item in row.items() if key not in {"owner_id", "last_seen_at", "status", "digest"}}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


class PlatformCatalogMixin:
    @staticmethod
    def _row_model(row) -> dict[str, Any]:
        return {"profile_id": row["profile_id"], "provider": row["provider"],
                "model": row["model"], "secret_ref": row["secret_ref"],
                "capabilities": _decode(row["capabilities"]),
                "provider_config": _decode(row["provider_config"]),
                "enabled": bool(row["enabled"]), "owner_id": row["owner_id"]}

    @staticmethod
    def _row_workspace(row) -> dict[str, Any]:
        return {"workspace_id": row["workspace_id"], "owner_id": row["owner_id"],
                "name": row["name"], "backend": row["backend"],
                "root_path": row["root_path"], "default_node_id": row["default_node_id"],
                "enabled": bool(row["enabled"])}

    @staticmethod
    def _row_node(row) -> dict[str, Any]:
        return {"node_id": row["node_id"], "owner_id": row["owner_id"],
                "label": row["label"], "capabilities": _decode(row["capabilities"]),
                "enabled": bool(row["enabled"]),
                "last_seen_at": row["last_seen_at"],
                "status": row["status"]}

    def list_models(self, owner_id: str) -> list[dict[str, Any]]:
        owner_id = validate_owner_id(owner_id)
        conn = None
        try:
            conn = self._connect()
            return [self._row_model(row) for row in conn.execute("SELECT * FROM model_profiles WHERE owner_id=? ORDER BY profile_id", (owner_id,))]
        except PlatformValidationError:
            raise
        except sqlite3.Error:
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def get_model(self, owner_id: str, profile_id: str) -> dict[str, Any] | None:
        owner_id = validate_owner_id(owner_id)
        profile_id = validate_id(profile_id, "profile_id")
        conn = None
        try:
            conn = self._connect()
            row = conn.execute("SELECT * FROM model_profiles WHERE owner_id=? AND profile_id=?", (owner_id, profile_id)).fetchone()
            return self._row_model(row) if row else None
        except (PlatformValidationError, sqlite3.Error):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def list_workspaces(self, owner_id: str) -> list[dict[str, Any]]:
        owner_id = validate_owner_id(owner_id)
        conn = None
        try:
            conn = self._connect()
            return [self._row_workspace(row) for row in conn.execute("SELECT * FROM workspaces WHERE owner_id=? ORDER BY workspace_id", (owner_id,))]
        except PlatformValidationError:
            raise
        except sqlite3.Error:
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def list_nodes(self, owner_id: str) -> list[dict[str, Any]]:
        owner_id = validate_owner_id(owner_id)
        conn = None
        try:
            conn = self._connect()
            return [self._row_node(row) for row in conn.execute("SELECT * FROM nodes WHERE owner_id=? ORDER BY node_id", (owner_id,))]
        except PlatformValidationError:
            raise
        except sqlite3.Error:
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def upsert_model(self, owner_id: str, data: Mapping[str, Any], *, expected_digest: str | None = None) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        normalized = validate_model_profile(data)
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            if expected_digest is not None:
                previous = conn.execute("SELECT * FROM model_profiles WHERE owner_id=? AND profile_id=?", (owner_id, normalized["profile_id"])).fetchone()
                if catalog_digest(self._row_model(previous) if previous else None) != expected_digest:
                    raise PlatformRepositoryError("revision_conflict")
            conn.execute("INSERT INTO model_profiles(owner_id,profile_id,provider,model,secret_ref,capabilities,provider_config,enabled) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(owner_id,profile_id) DO UPDATE SET provider=excluded.provider,model=excluded.model,secret_ref=excluded.secret_ref,capabilities=excluded.capabilities,provider_config=excluded.provider_config,enabled=excluded.enabled,updated_at=CURRENT_TIMESTAMP", (owner_id, normalized["profile_id"], normalized["provider"], normalized["model"], normalized["secret_ref"], _json(normalized["capabilities"]), _json(normalized["provider_config"]), int(normalized["enabled"])))
            if (normalized["secret_ref"] or "").startswith("managed://"):
                # Old workers only understand env://. Fence them in the same
                # transaction that publishes the new credential binding.
                if conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "4":
                    conn.execute("INSERT OR REPLACE INTO meta VALUES ('mcp_catalog_initialized','1')")
                conn.execute("UPDATE meta SET value=CAST(MAX(CAST(value AS INTEGER),5) AS TEXT) WHERE key='schema_version'")
            conn.execute("COMMIT")
            return normalized | {"owner_id": owner_id}
        except (PlatformValidationError, PlatformRepositoryError):
            raise
        except sqlite3.Error:
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def upsert_workspace(self, owner_id: str, data: Mapping[str, Any], *, expected_digest: str | None = None) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        normalized = validate_workspace(data)
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            if expected_digest is not None:
                previous = conn.execute("SELECT * FROM workspaces WHERE owner_id=? AND workspace_id=?", (owner_id, normalized["workspace_id"])).fetchone()
                if catalog_digest(self._row_workspace(previous) if previous else None) != expected_digest:
                    raise PlatformRepositoryError("revision_conflict")
            conn.execute("INSERT INTO workspaces(owner_id,workspace_id,name,backend,root_path,default_node_id,enabled) VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner_id,workspace_id) DO UPDATE SET name=excluded.name,backend=excluded.backend,root_path=excluded.root_path,default_node_id=excluded.default_node_id,enabled=excluded.enabled", (owner_id, normalized["workspace_id"], normalized["name"], normalized["backend"], normalized["root_path"], normalized["default_node_id"], int(normalized["enabled"])))
            conn.execute("COMMIT")
            return normalized | {"owner_id": owner_id}
        except (PlatformValidationError, PlatformRepositoryError):
            raise
        except sqlite3.Error:
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def upsert_node(self, owner_id: str, data: Mapping[str, Any], *, expected_digest: str | None = None) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        normalized = validate_node(data)
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            if expected_digest is not None:
                previous = conn.execute("SELECT * FROM nodes WHERE owner_id=? AND node_id=?", (owner_id, normalized["node_id"])).fetchone()
                if catalog_digest(self._row_node(previous) if previous else None) != expected_digest:
                    raise PlatformRepositoryError("revision_conflict")
            conn.execute("INSERT INTO nodes(owner_id,node_id,label,capabilities,enabled) VALUES(?,?,?,?,?) ON CONFLICT(owner_id,node_id) DO UPDATE SET label=excluded.label,capabilities=excluded.capabilities,enabled=excluded.enabled", (owner_id, normalized["node_id"], normalized["label"], _json(normalized["capabilities"]), int(normalized["enabled"])))
            conn.execute("COMMIT")
            return normalized | {"owner_id": owner_id}
        except (PlatformValidationError, PlatformRepositoryError):
            raise
        except sqlite3.Error:
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def get_node(self, owner_id: str, node_id: str) -> dict[str, Any] | None:
        owner_id = validate_owner_id(owner_id)
        node_id = validate_id(node_id, "node_id")
        conn = None
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM nodes WHERE owner_id=? AND node_id=?",
                (owner_id, node_id)).fetchone()
            return self._row_node(row) if row else None
        except (PlatformValidationError, sqlite3.Error, KeyError):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def get_defaults(self, owner_id: str) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        conn = None
        try:
            conn = self._connect()
            row = conn.execute("SELECT * FROM owner_defaults WHERE owner_id=?", (owner_id,)).fetchone()
            return {"owner_id": owner_id, "revision": int(row["revision"]) if row else 0,
                    "model_profile_id": row["model_profile_id"] if row else None,
                    "workspace_id": row["workspace_id"] if row else None,
                    "execution_node_id": row["execution_node_id"] if row else None}
        except (PlatformValidationError, sqlite3.Error):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def update_defaults(self, owner_id: str, values: Mapping[str, Any], expected_revision: int) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise PlatformRepositoryError("invalid_revision")
        fields = {key: values.get(key) for key in ("model_profile_id", "workspace_id", "execution_node_id")}
        for key, value in fields.items():
            if value is not None:
                fields[key] = validate_id(value, key)
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT revision FROM owner_defaults WHERE owner_id=?", (owner_id,)).fetchone()
            revision = int(current["revision"]) if current else 0
            if revision != expected_revision:
                conn.execute("ROLLBACK")
                raise PlatformRepositoryError("revision_conflict")
            for key, table, column in (("model_profile_id", "model_profiles", "profile_id"), ("workspace_id", "workspaces", "workspace_id"), ("execution_node_id", "nodes", "node_id")):
                value = fields[key]
                if value is not None:
                    row = conn.execute(f"SELECT owner_id, enabled FROM {table} WHERE owner_id=? AND {column}=?", (owner_id, value)).fetchone()
                    if not row or row["owner_id"] != owner_id or not bool(row["enabled"]):
                        conn.execute("ROLLBACK")
                        raise PlatformRepositoryError("reference_forbidden")
            new_revision = revision + 1
            conn.execute("INSERT INTO owner_defaults(owner_id,model_profile_id,workspace_id,execution_node_id,revision) VALUES(?,?,?,?,?) ON CONFLICT(owner_id) DO UPDATE SET model_profile_id=excluded.model_profile_id,workspace_id=excluded.workspace_id,execution_node_id=excluded.execution_node_id,revision=excluded.revision", (owner_id, fields["model_profile_id"], fields["workspace_id"], fields["execution_node_id"], new_revision))
            conn.execute("COMMIT")
            return {"owner_id": owner_id, "revision": new_revision, **fields}
        except PlatformRepositoryError:
            raise
        except (sqlite3.Error, ValueError, TypeError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def get_workspace(self, owner_id: str, workspace_id: str) -> dict[str, Any] | None:
        owner_id = validate_owner_id(owner_id)
        workspace_id = validate_id(workspace_id, "workspace_id")
        conn = None
        try:
            conn = self._connect()
            row = conn.execute("SELECT * FROM workspaces WHERE owner_id=? AND workspace_id=?", (owner_id, workspace_id)).fetchone()
            return self._row_workspace(row) if row else None
        except (PlatformValidationError, sqlite3.Error):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()
