"""Conversations, messages and turn submission for ``PlatformRepository``."""
from __future__ import annotations

import math
import sqlite3
from collections.abc import Mapping
from typing import Any

from hub.infrastructure.execution_window_repository import ExecutionWindowRepository
from hub.infrastructure.platform_db_base import PlatformRepositoryError, _decode, _json
from platform_schema import PlatformValidationError, validate_id, validate_owner_id


class PlatformConversationMixin:
    @staticmethod
    def _row_conversation(row) -> dict[str, Any]:
        return {"conversation_id": row["conversation_id"], "owner_id": row["owner_id"],
                "title": row["title"], "workspace_id": row["workspace_id"],
                "overrides": _decode(row["overrides"]), "revision": int(row["revision"]),
                "archived_at": row["archived_at"]}

    @staticmethod
    def _row_message(row) -> dict[str, Any]:
        return {"message_id": row["message_id"], "conversation_id": row["conversation_id"],
                "owner_id": row["owner_id"], "role": row["role"], "content": row["content"],
                "client_token": row["client_token"], "created_at": row["created_at"],
                "turn_sequence": row["turn_sequence"]}

    def create_conversation(self, owner_id: str, conversation_id: str, *, title: str, workspace_id: str | None, overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        conversation_id = validate_id(conversation_id, "conversation_id")
        if overrides is not None and not isinstance(overrides, Mapping):
            raise PlatformRepositoryError("invalid_value")
        if workspace_id is not None:
            workspace_id = validate_id(workspace_id, "workspace_id")
        conn = None
        try:
            conn = self._connect()
            if workspace_id:
                row = conn.execute("SELECT enabled FROM workspaces WHERE owner_id=? AND workspace_id=?", (owner_id, workspace_id)).fetchone()
                if not row or not bool(row["enabled"]):
                    raise PlatformRepositoryError("reference_forbidden")
            conn.execute("INSERT INTO conversations(conversation_id,owner_id,title,workspace_id,overrides,revision) VALUES(?,?,?,?,?,0)", (conversation_id, owner_id, str(title or "")[:120], workspace_id, _json({} if overrides is None else overrides)))
            row = conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
            return self._row_conversation(row)
        except PlatformRepositoryError:
            raise
        except sqlite3.IntegrityError:
            raise PlatformRepositoryError("conversation_conflict") from None
        except (PlatformValidationError, sqlite3.Error):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def _conversation_messages(self, conn, owner_id, conversation_id, trigger_id=None):
        """Project user turns and durable assistant replies in turn order.

        Runs already persist their final answer atomically with completion.
        Reusing that record avoids a second, diverging copy of the reply.
        A worker's current trigger includes only its user message; later turns
        and the current Run's own reply can never enter its input.
        """
        rows = conn.execute(
            "SELECT m.*,r.run_id AS reply_run_id,r.state AS reply_state,"
            "r.result_text AS reply_text,r.updated_at AS reply_at "
            "FROM messages m LEFT JOIN runs r ON r.trigger_message_id=m.message_id "
            "AND r.owner_id=m.owner_id WHERE m.owner_id=? AND m.conversation_id=? "
            "AND (? IS NULL OR m.turn_sequence<=(SELECT turn_sequence FROM messages WHERE message_id=?)) "
            "ORDER BY m.turn_sequence",
            (owner_id, conversation_id, trigger_id, trigger_id),
        ).fetchall()
        result = []
        for row in rows:
            result.append(self._row_message(row))
            if (row["message_id"] != trigger_id and row["reply_state"] == "succeeded"
                    and row["reply_text"]):
                result.append({
                    "message_id": "reply_" + row["reply_run_id"],
                    "conversation_id": conversation_id, "owner_id": owner_id,
                    "role": "assistant", "content": row["reply_text"],
                    "client_token": None, "created_at": str(row["reply_at"]),
                    "turn_sequence": row["turn_sequence"],
                })
        return result

    def get_conversation(self, owner_id: str, conversation_id: str) -> dict[str, Any] | None:
        owner_id = validate_owner_id(owner_id)
        conversation_id = validate_id(conversation_id, "conversation_id")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN")
            row = conn.execute("SELECT * FROM conversations WHERE owner_id=? AND conversation_id=?", (owner_id, conversation_id)).fetchone()
            if not row:
                return None
            conversation = self._row_conversation(row)
            conversation["messages"] = self._conversation_messages(conn, owner_id, conversation_id)
            conversation["runs"] = [self._row_run(item) for item in conn.execute("SELECT r.* FROM runs r JOIN messages m ON m.message_id=r.trigger_message_id WHERE r.owner_id=? AND r.conversation_id=? ORDER BY m.turn_sequence", (owner_id, conversation_id)).fetchall()]
            self._attach_requests(conn, owner_id, conversation["runs"], conversation_id=conversation_id)
            return conversation
        except (PlatformValidationError, sqlite3.Error):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def list_conversations(self, owner_id: str, *, limit: int = 50,
                           archived: bool = False) -> list[dict[str, Any]]:
        """Return a bounded owner-scoped inbox without message credentials/tokens."""
        owner_id = validate_owner_id(owner_id)
        if not isinstance(archived, bool):
            raise PlatformRepositoryError("invalid_value")
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            raise PlatformRepositoryError("invalid_limit") from None
        conn = None
        try:
            conn = self._connect()
            archive_filter = "IS NOT NULL" if archived else "IS NULL"
            rows = conn.execute(
                f"""
                SELECT c.*,
                  (SELECT m.content FROM messages m
                   WHERE m.owner_id=c.owner_id AND m.conversation_id=c.conversation_id
                   ORDER BY CAST(m.created_at AS REAL) DESC, m.message_id DESC LIMIT 1) AS last_message,
                  (SELECT r.run_id FROM runs r
                   WHERE r.owner_id=c.owner_id AND r.conversation_id=c.conversation_id
                   ORDER BY r.updated_at DESC, r.run_id DESC LIMIT 1) AS latest_run_id,
                  (SELECT r.state FROM runs r
                   WHERE r.owner_id=c.owner_id AND r.conversation_id=c.conversation_id
                   ORDER BY r.updated_at DESC, r.run_id DESC LIMIT 1) AS latest_run_state,
                  (SELECT r.result_text FROM runs r
                   WHERE r.owner_id=c.owner_id AND r.conversation_id=c.conversation_id
                   ORDER BY r.updated_at DESC, r.run_id DESC LIMIT 1) AS latest_run_result,
                  (SELECT r.updated_at FROM runs r
                   WHERE r.owner_id=c.owner_id AND r.conversation_id=c.conversation_id
                   ORDER BY r.updated_at DESC, r.run_id DESC LIMIT 1) AS latest_run_at,
                  (SELECT MAX(CAST(m.created_at AS REAL)) FROM messages m
                   WHERE m.owner_id=c.owner_id AND m.conversation_id=c.conversation_id) AS latest_message_at
                FROM conversations c
                WHERE c.owner_id=? AND c.archived_at {archive_filter}
                ORDER BY COALESCE(latest_run_at, latest_message_at, 0) DESC, c.conversation_id DESC
                LIMIT ?
                """, (owner_id, limit)).fetchall()
            result = []
            for row in rows:
                item = self._row_conversation(row)
                item.pop("owner_id", None)
                item.pop("overrides", None)
                item["last_message_preview"] = str(row["last_message"] or "")[:240]
                if row["latest_run_id"]:
                    item["latest_run"] = {
                        "run_id": row["latest_run_id"],
                        "state": row["latest_run_state"],
                        "result_preview": str(row["latest_run_result"] or "")[:500],
                    }
                else:
                    item["latest_run"] = None
                result.append(item)
            return result
        except PlatformRepositoryError:
            raise
        except (sqlite3.Error, PlatformValidationError):
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def rename_conversation(self, owner_id: str, conversation_id: str,
                            title: str) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        conversation_id = validate_id(conversation_id, "conversation_id")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120:
            raise PlatformRepositoryError("invalid_conversation_title")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            result = conn.execute(
                "UPDATE conversations SET title=?, revision=revision+1 "
                "WHERE owner_id=? AND conversation_id=?",
                (title.strip(), owner_id, conversation_id),
            )
            if result.rowcount != 1:
                conn.execute("ROLLBACK")
                raise PlatformRepositoryError("conversation_not_found")
            row = conn.execute(
                "SELECT * FROM conversations WHERE owner_id=? AND conversation_id=?",
                (owner_id, conversation_id),
            ).fetchone()
            conn.execute("COMMIT")
            return self._row_conversation(row)
        except PlatformRepositoryError:
            raise
        except (sqlite3.Error, PlatformValidationError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def set_conversation_archived(self, owner_id: str, conversation_id: str,
                                  *, archived: bool, now: float) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        conversation_id = validate_id(conversation_id, "conversation_id")
        if not isinstance(archived, bool) or not math.isfinite(float(now)):
            raise PlatformRepositoryError("invalid_value")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT archived_at FROM conversations "
                "WHERE owner_id=? AND conversation_id=?",
                (owner_id, conversation_id),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise PlatformRepositoryError("conversation_not_found")
            if bool(row["archived_at"] is not None) != archived:
                conn.execute(
                    "UPDATE conversations SET archived_at=?, revision=revision+1 "
                    "WHERE owner_id=? AND conversation_id=?",
                    (float(now) if archived else None, owner_id, conversation_id),
                )
            result = conn.execute(
                "SELECT * FROM conversations WHERE owner_id=? AND conversation_id=?",
                (owner_id, conversation_id),
            ).fetchone()
            conn.execute("COMMIT")
            return self._row_conversation(result)
        except PlatformRepositoryError:
            raise
        except (sqlite3.Error, PlatformValidationError, TypeError, ValueError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def delete_archived_conversation(self, owner_id: str,
                                     conversation_id: str) -> None:
        """Delete one archived conversation and its relational transcript atomically."""
        owner_id = validate_owner_id(owner_id)
        conversation_id = validate_id(conversation_id, "conversation_id")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT archived_at FROM conversations "
                "WHERE owner_id=? AND conversation_id=?",
                (owner_id, conversation_id),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise PlatformRepositoryError("conversation_not_found")
            if row["archived_at"] is None:
                conn.execute("ROLLBACK")
                raise PlatformRepositoryError("conversation_not_archived")
            active = conn.execute(
                "SELECT 1 FROM runs WHERE owner_id=? AND conversation_id=? "
                "AND state NOT IN ('succeeded','failed','cancelled') LIMIT 1",
                (owner_id, conversation_id),
            ).fetchone()
            if active:
                conn.execute("ROLLBACK")
                raise PlatformRepositoryError("conversation_run_active")
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            run_scope = "SELECT run_id FROM runs WHERE owner_id=? AND conversation_id=?"
            if "platform_commands" in tables:
                unresolved_command = conn.execute(
                    "SELECT 1 FROM platform_commands WHERE (owner_id=? OR owner_id IS NULL) "
                    f"AND run_id IN ({run_scope}) "
                    "AND status NOT IN ('succeeded','failed','expired') LIMIT 1",
                    (owner_id, owner_id, conversation_id),
                ).fetchone()
                if unresolved_command:
                    conn.execute("ROLLBACK")
                    raise PlatformRepositoryError("conversation_command_unresolved")
            for table in ("browser_artifact_tickets", "browser_submit_approvals", "browser_sessions"):
                if table in tables:
                    conn.execute(
                        f"DELETE FROM {table} WHERE owner_id=? AND run_id IN ({run_scope})",
                        (owner_id, owner_id, conversation_id),
                    )
            if "execution_windows" in tables:
                window_scope = (
                    "SELECT window_id FROM execution_windows WHERE owner_id=? "
                    f"AND run_id IN ({run_scope})"
                )
                # 子表清单由 ExecutionWindowRepository 统一维护（含 frame_reads），
                # 避免手写列表漏表导致删除会话时触发 FK 约束失败。
                ExecutionWindowRepository.purge_windows(
                    conn, owner_id, window_scope,
                    (owner_id, owner_id, conversation_id), existing_tables=tables,
                )
            if "platform_commands" in tables:
                command_scope = (
                    "SELECT command_id FROM platform_commands "
                    "WHERE (owner_id=? OR owner_id IS NULL) "
                    f"AND run_id IN ({run_scope})"
                )
                command_params = (owner_id, owner_id, conversation_id)
                for table in (
                    "platform_command_reconciliations", "platform_command_postchecks",
                    "platform_service_postchecks",
                ):
                    if table in tables:
                        conn.execute(
                            f"DELETE FROM {table} WHERE command_id IN ({command_scope})",
                            command_params,
                        )
                conn.execute(
                    "DELETE FROM platform_command_outbox WHERE command_id IN "
                    f"({command_scope})", command_params,
                )
                conn.execute(
                    "DELETE FROM platform_commands WHERE (owner_id=? OR owner_id IS NULL) "
                    f"AND run_id IN ({run_scope})", (owner_id, owner_id, conversation_id),
                )
            conn.execute(
                "DELETE FROM run_events WHERE run_id IN "
                "(SELECT run_id FROM runs WHERE owner_id=? AND conversation_id=?)",
                (owner_id, conversation_id),
            )
            conn.execute(
                "DELETE FROM legacy_task_links WHERE owner_id=? AND run_id IN "
                "(SELECT run_id FROM runs WHERE owner_id=? AND conversation_id=?)",
                (owner_id, owner_id, conversation_id),
            )
            conn.execute("DELETE FROM runs WHERE owner_id=? AND conversation_id=?",
                         (owner_id, conversation_id))
            conn.execute("DELETE FROM messages WHERE owner_id=? AND conversation_id=?",
                         (owner_id, conversation_id))
            conn.execute("DELETE FROM conversations WHERE owner_id=? AND conversation_id=?",
                         (owner_id, conversation_id))
            conn.execute("COMMIT")
        except PlatformRepositoryError:
            raise
        except (sqlite3.Error, PlatformValidationError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()

    def append_turn(self, owner_id: str, conversation_id: str, message_id: str, run_id: str, *, text: str, client_token: str, config_snapshot: Mapping[str, Any], now: float) -> dict[str, Any]:
        owner_id = validate_owner_id(owner_id)
        conversation_id = validate_id(conversation_id, "conversation_id")
        message_id = validate_id(message_id, "message_id")
        run_id = validate_id(run_id, "run_id")
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            conversation = conn.execute("SELECT * FROM conversations WHERE owner_id=? AND conversation_id=?", (owner_id, conversation_id)).fetchone()
            if not conversation:
                conn.execute("ROLLBACK")
                raise PlatformRepositoryError("conversation_not_found")
            prior = conn.execute("SELECT * FROM messages WHERE owner_id=? AND conversation_id=? AND client_token=?", (owner_id, conversation_id, client_token)).fetchone()
            if prior:
                if prior["content"] != text:
                    conn.execute("ROLLBACK")
                    raise PlatformRepositoryError("idempotency_conflict")
                run = conn.execute("SELECT * FROM runs WHERE trigger_message_id=?", (prior["message_id"],)).fetchone()
                conn.execute("COMMIT")
                if not run:
                    raise PlatformRepositoryError("run_not_found")
                return {"created": False, "message": self._row_message(prior), "run": self._row_run(run)}
            if conversation["archived_at"] is not None:
                conn.execute("ROLLBACK")
                raise PlatformRepositoryError("conversation_archived")
            conn.execute("INSERT INTO messages(message_id,conversation_id,owner_id,role,content_ref,content,client_token,created_at) VALUES(?,?,?,?,?,?,?,?)", (message_id, conversation_id, owner_id, "user", "inline", text, client_token, str(now)))
            snapshot = _json(config_snapshot)
            conn.execute("INSERT INTO runs(run_id,conversation_id,owner_id,trigger_message_id,state,config_snapshot,cancel_requested,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (run_id, conversation_id, owner_id, message_id, "queued", snapshot, 0, float(now), float(now)))
            message = conn.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            conn.execute("COMMIT")
            return {"created": True, "message": self._row_message(message), "run": self._row_run(run)}
        except PlatformRepositoryError:
            raise
        except sqlite3.IntegrityError:
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise PlatformRepositoryError("idempotency_conflict") from None
        except (sqlite3.Error, TypeError, ValueError):
            if conn is not None:
                try: conn.execute("ROLLBACK")
                except sqlite3.Error: pass
            raise PlatformRepositoryError("platform_store") from None
        finally:
            if conn is not None: conn.close()
