"""Authorize immutable per-turn results independently from startup task results."""
from __future__ import annotations

from collections.abc import Mapping
import sqlite3

from fleet_core.continuation_result import validate_result
from hub.application.errors import ApplicationError
from hub.domain.continuation_results import ContinuationResultStore, ContinuationResultStoreError


class ContinuationResultService:
    def __init__(self, repository: ContinuationResultStore, sessions, supervisor, task_repository):
        self.repository, self.sessions = repository, sessions
        self.supervisor, self.tasks = supervisor, task_repository

    def publish(self, machine: str, raw: Mapping) -> dict:
        try:
            value = validate_result(raw)
        except (ValueError, TypeError):
            raise ApplicationError("invalid_continuation_result", "", 400) from None
        # receipt_status is a public bounded command view. It includes neither
        # prompt nor signed envelope. Immutable first-dispatch evidence remains
        # valid for delayed uploads after a lease expires or is superseded.
        command = self.supervisor.receipt_status(value["command_id"])
        session = self.sessions.get_session(value["session_id"])["session"]
        if (command.get("machine_id") != machine or session.get("machine_id") != machine
                or command.get("session_id") != value["session_id"]
                or command.get("action") != "append_user_turn"
                or not session.get("managed")
                or command.get("attempt_id") != session.get("attempt_id")):
            raise ApplicationError("continuation_result_binding", "", 403)
        if (not command.get('dispatched') or command.get("status") not in
                {"delivered", "accepted", "executing", "succeeded", "failed", "expired"}):
            raise ApplicationError("continuation_result_not_dispatched", "", 409)
        try:
            task = self.tasks.get_task_for_attempt(command.get("attempt_id"))
        except (sqlite3.Error, OSError):
            raise ApplicationError("continuation_result_unavailable", "", 503) from None
        if not task or task.get("task_id") != value["task_id"] or task.get("machine") != machine:
            raise ApplicationError("continuation_result_binding", "", 403)
        try:
            stored, first = self.repository.put(machine, value)
            return {"ok": True, "command_id": stored["command_id"], "stored": first}
        except ContinuationResultStoreError as exc:
            raise ApplicationError(exc.code, "", 409 if exc.code == "continuation_result_conflict" else 503) from exc

    def page(self, session_id, *, limit=20, before=None):
        self.sessions.get_session(session_id)
        if type(limit) is not int or not 1 <= limit <= 50 or (before is not None and (type(before) is not int or not 0 < before <= 2**63 - 1)):
            raise ApplicationError("invalid_cursor", "", 400)
        try:
            return {"ok": True, "session_id": session_id,
                    **self.repository.page(session_id, limit=limit, before=before)}
        except ContinuationResultStoreError as exc:
            raise ApplicationError(exc.code, "", 400 if exc.code == "invalid_cursor" else 503) from exc

    def detail(self, session_id, command_id):
        self.sessions.get_session(session_id)
        if not isinstance(command_id, str) or not 1 <= len(command_id) <= 128:
            raise ApplicationError("invalid_request", "", 400)
        try:
            result = self.repository.get(session_id, command_id)
        except ContinuationResultStoreError as exc:
            raise ApplicationError(exc.code, "", 503) from exc
        if result is None:
            raise ApplicationError("continuation_result_not_found", "", 404)
        return {"ok": True, "session_id": session_id, "result": result}
