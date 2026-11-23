"""Application-owned storage contracts for session ingress and queries.

SQLite adapters implement these structural ports. Errors and ingest results are
independent of persistence so alternate stores and in-memory fixtures do not
need to import an infrastructure implementation. Existing adapter imports are
kept as aliases for compatibility.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

class SessionError(RuntimeError):
    """Bounded session repository error: only a stable short code is exposed.

    ``str(err)`` and ``code`` carry the bounded code only - never paths,
    exception text, raw input, or key material.
    """

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


class TranscriptError(RuntimeError):
    """Bounded transcript repository error: only a stable short code.

    ``str(err)`` and ``code`` carry the bounded code only - never paths,
    exception text, raw input, or key material.
    """

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True)
class IngestResult:
    """Result of a single :meth:`TranscriptRepository.ingest` call.

    Fields are JSON-safe and carry no paths, exception text, or raw payloads:

    - ``status`` - ``accepted`` | ``duplicate`` | ``rejected`` | ``gap``
    - ``event_id`` - the event identifier (or ``None``)
    - ``reason``  - a stable bounded code, or ``None`` on a clean accept
    - ``raw_written`` - whether the raw encrypted row was written
    """

    status: str
    event_id: str | None = None
    reason: str | None = None
    raw_written: bool = False

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"


class AdoptionRepositoryError(RuntimeError):
    """Bounded adoption repository error: a stable short code only.

    ``str(err)`` and ``code`` carry the bounded code only  - never paths,
    exception text, raw input, or key material.
    """

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code



@runtime_checkable
class SessionStore(Protocol):
    """Durable metadata, authoritative projection and interaction admission."""
    management_available: bool

    def upsert_session(self, spec: Mapping[str, Any]) -> dict[str, Any]: ...
    def get_session(self, session_id: str) -> dict[str, Any] | None: ...
    def list_sessions(self, machine_id: str | None = None, limit: int = 100,
                      active_only: bool = False) -> list[dict[str, Any]]: ...
    def list_sessions_page(self, machine_id: str | None = None, *, limit: int = 100,
                           snapshot_cursor: int | None = None,
                           before_cursor: int | None = None) -> dict[str, Any]: ...
    def init_stream(self, session_id: str) -> None: ...
    def note_event(self, session_id: str, event_id: str, sequence: int, kind: str) -> None: ...
    def ack_cursor(self, session_id: str) -> int: ...
    def acknowledge(self, session_id: str, n: int) -> None: ...
    def get_capabilities(self, session_id: str) -> list: ...
    def get_capabilities_many(self, session_ids: list[str]) -> dict: ...
    def set_capabilities(self, session_id: str, capabilities: list, *, sequence: int | None = None) -> None: ...
    def update_agent_state(self, session_id: str, state: str, source: str,
                           confidence: str, sequence: int) -> dict[str, Any] | None: ...
    def reopen_session(self, session_id: str, sequence: int) -> None: ...
    def close_session(self, session_id: str, status: str, finished_at: str, *,
                      sequence: int | None = None) -> dict[str, Any] | None: ...
    def update_session_metadata(self, session_id: str, cursor: int,
                                payload: Mapping[str, Any]) -> None: ...
    def update_title_preview(self, session_id: str, cursor: int, text: str) -> None: ...
    def observe_events(self, session_id: str, events: list[Mapping]) -> None: ...
    def project_interaction(self, session_id: str, sequence: int, kind: str, payload: dict) -> None: ...
    def get_interaction(self, session_id: str, request_id: str) -> dict | None: ...
    def reserve_interaction(self, session_id: str, request_id: str, client_token: str, answer: dict) -> dict | None: ...
    def finish_interaction(self, session_id: str, request_id: str, command_id: str | None) -> None: ...
    def interaction_command(self, session_id: str, request_id: str) -> str | None: ...


@runtime_checkable
class TranscriptStore(Protocol):
    """Ordered redacted event stream and contiguous durable acknowledgement."""
    def ingest(self, event: Mapping[str, Any]) -> IngestResult: ...
    def contiguous_sequence(self, session_id: str, start: int = 0) -> int: ...
    def read_redacted_events(self, session_id: str, event_ids: list[str]) -> list[dict]: ...
    def event_cursor_before_sequence(self, session_id: str, sequence: int) -> int: ...
    def read_redacted(self, session_id: str, *, limit: int = 100,
                      after_cursor: int = 0, before_cursor: int | None = None,
                      tail: bool = False, include_source: bool = True) -> list[dict]: ...
    def read_policy_signals(self, session_id: str) -> list[dict]: ...
