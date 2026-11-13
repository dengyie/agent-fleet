"""Execution-window control-plane state and bounded public contracts."""
from __future__ import annotations

WINDOW_STATES = frozenset({"pending", "ready", "attached", "closing", "closed", "expired"})
TERMINAL_WINDOW_STATES = frozenset({"closed", "expired"})

# T3.3 is an event transport contract, not a process/PTY protocol.  These
# kinds describe bounded control-plane facts and leave host execution to a
# future adapter with its own capability contract.
WINDOW_EVENT_KINDS = frozenset({
    "status", "text", "notice", "input_ack", "output", "browser.frame",
})

WINDOW_EVENT_FIELDS = {
    "status": frozenset({"state", "detail", "reason", "healthy"}),
    "text": frozenset({"text", "format"}),
    "notice": frozenset({"level", "message", "code"}),
    "input_ack": frozenset({"client_event_id", "accepted", "reason"}),
    "output": frozenset({"text", "stream", "format"}),
    "browser.frame": frozenset({
        "artifact_id", "sha256", "content_type", "width", "height",
        "captured_at", "frame_seq",
    }),
}

BROWSER_MUTATION_ACTIONS = frozenset({
    "tool.browser.open", "tool.browser.navigate", "tool.browser.click",
    "tool.browser.type", "tool.browser.scroll", "tool.browser.back",
    "tool.browser.submit", "tool.browser.close",
})


def public_window(row: dict) -> dict:
    """Return only metadata safe for an operator response."""
    return {key: row.get(key) for key in (
        "window_id", "run_id", "state",
        "created_at", "expires_at", "attached_at", "closed_at",
        "updated_at",
    )}


__all__ = [
    "WINDOW_STATES", "TERMINAL_WINDOW_STATES", "WINDOW_EVENT_KINDS",
    "WINDOW_EVENT_FIELDS",
    "BROWSER_MUTATION_ACTIONS",
    "public_window",
]
