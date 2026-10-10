"""Application service for the gated session transcript search surface."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import sqlite3
from typing import Any

from hub.infrastructure.session_search_repository import (
    SessionSearchRepositoryError,
)
from hub.infrastructure.session_repository import SessionError
from hub.infrastructure.transcript_repository import TranscriptError


class SessionSearchServiceError(RuntimeError):
    """Transport-neutral bounded error for operator search."""

    _STATUS = {
        "search_disabled": 503,
        "search_unavailable": 503,
        "invalid_query": 400,
        "invalid_limit": 400,
        "invalid_cursor": 400,
        "invalid_filter": 400,
        "invalid_event": 400,
        "invalid_batch": 400,
    }

    def __init__(self, code: str):
        self.code = code
        self.status = self._STATUS.get(code, 503)
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


class SessionSearchService:
    """Stateless search use cases over a redacted-only repository."""

    def __init__(self, repository, *, task_repository=None):
        self.repository = repository
        self.task_repository = task_repository

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.repository, "enabled", False))

    @property
    def available(self) -> bool:
        return bool(getattr(self.repository, "available", False))

    def initialize(self) -> None:
        try:
            self.repository.init()
        except SessionSearchRepositoryError as exc:
            raise SessionSearchServiceError(exc.code) from exc
        except (sqlite3.Error, OSError) as exc:
            raise SessionSearchServiceError("search_unavailable") from exc

    def initialize_optional(self) -> str | None:
        try:
            self.initialize()
        except SessionSearchServiceError as exc:
            return exc.code
        return None

    def index_accepted(self, transcript_repository, session_repository,
                       session_id: str, event_ids: list[str]) -> dict[str, Any]:
        """Build the index exclusively from durably accepted redacted rows."""
        if not self.enabled:
            return {"indexed": 0, "code": "search_disabled"}
        try:
            rows = transcript_repository.read_redacted_events(session_id, event_ids)
            session = session_repository.get_session(session_id) or {}
            metadata = {}
            if self.task_repository is not None and session.get("attempt_id"):
                metadata = self.task_repository.get_session_search_metadata(
                    session["attempt_id"], session.get("machine_id", "")) or {}
            count = 0
            for index, row in enumerate(rows):
                result = self.index_event(
                    row, machine_id=session.get("machine_id"),
                    project_id=metadata.get("project"),
                    agent_family=metadata.get("agent_type") or session.get("agent_family"),
                )
                count += int(result["indexed"])
                if result["code"] is not None:
                    queue_pending = getattr(self.repository, "queue_pending_events", None)
                    if callable(queue_pending) and index + 1 < len(rows):
                        queue_pending(
                            rows[index + 1:], machine_id=session.get("machine_id"),
                            project_id=metadata.get("project"),
                            agent_family=metadata.get("agent_type") or session.get("agent_family"),
                        )
                    return {"indexed": count, "code": result["code"]}
            return {"indexed": count, "code": None}
        except (TranscriptError, SessionError, sqlite3.Error, OSError):
            return {"indexed": 0, "code": "search_unavailable"}

    def index_event(
        self,
        event: Mapping[str, Any],
        *,
        machine_id: str | None = None,
        project_id: str | None = None,
        agent_family: str | None = None,
    ) -> dict[str, Any]:
        """Best-effort optional index hook that never raises storage failures.

        Transcript persistence is authoritative.  A caller may invoke this
        after a successful transcript write; a disabled/broken search index is
        returned as a fixed diagnostic instead of being allowed to roll back
        that write.
        """

        try:
            indexed = self.repository.index_event(
                event,
                machine_id=machine_id,
                project_id=project_id,
                agent_family=agent_family,
            )
        except SessionSearchRepositoryError as exc:
            return {"indexed": False, "code": exc.code}
        except (sqlite3.Error, OSError):
            return {"indexed": False, "code": "search_unavailable"}
        return {"indexed": bool(indexed), "code": None}

    def index_events(self, events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        if (isinstance(events, (str, bytes)) or not isinstance(events, Sequence)
                or len(events) > 100):
            return {"indexed": 0, "code": "invalid_batch"}
        count = 0
        for event in events:
            result = self.index_event(event)
            if result["indexed"]:
                count += 1
            if result["code"] is not None:
                return {"indexed": count, "code": result["code"]}
        return {"indexed": count, "code": None}

    def search(self, query: str, **filters: Any) -> dict[str, Any]:
        aliases = {
            "machine": "machine_id",
            "project": "project_id",
            "agent": "agent_family",
            "from": "from_time",
            "to": "to_time",
        }
        normalized: dict[str, Any] = {}
        for key, value in filters.items():
            target = aliases.get(key, key)
            if target in normalized:
                raise SessionSearchServiceError("invalid_filter")
            normalized[target] = value
        allowed = {
            "machine_id", "project_id", "agent_family", "from_time",
            "to_time", "limit", "after",
        }
        if any(key not in allowed for key in normalized):
            raise SessionSearchServiceError("invalid_filter")
        try:
            return self.repository.search(query, **normalized)
        except SessionSearchRepositoryError as exc:
            raise SessionSearchServiceError(exc.code) from exc
        except (sqlite3.Error, OSError) as exc:
            raise SessionSearchServiceError("search_unavailable") from exc

    def purge_before(self, emitted_before: str) -> int:
        """Explicit redacted-index retention; raw retention is independent."""
        try:
            return self.repository.purge_before(emitted_before)
        except SessionSearchRepositoryError as exc:
            raise SessionSearchServiceError(exc.code) from exc

    def delete_session(self, session_id: str) -> int:
        try:
            return self.repository.delete_session(session_id)
        except SessionSearchRepositoryError as exc:
            raise SessionSearchServiceError(exc.code) from exc

    query = search


__all__ = ["SessionSearchService", "SessionSearchServiceError"]
