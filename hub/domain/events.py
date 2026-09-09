"""Domain contract for persisted hub events.

The event shape remains a plain dictionary for compatibility with the legacy
HTTP and SSE payloads. Persistence is expressed as a protocol so application
code does not depend on JSONL or Flask.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class EventRepository(Protocol):
    """Persistent event log used by the application event publisher."""

    def append(self, event: dict) -> None:
        """Persist one event."""
        ...

    def read_recent(self, limit: int = 50) -> list[dict]:
        """Return the newest events in chronological order."""
        ...

    def read_since(self, ts: float, limit: int = 200) -> list[dict]:
        """Return events with timestamps strictly greater than ``ts``."""
        ...
