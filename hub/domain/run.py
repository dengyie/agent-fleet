"""Run state machine and public state contract."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

RUN_STATES = frozenset({
    "queued", "running", "waiting_node", "waiting_approval", "waiting_task",
    "paused", "cancelling", "succeeded", "failed", "unknown", "cancelled",
})
TERMINAL_RUN_STATES = frozenset({"succeeded", "failed", "unknown", "cancelled"})


@dataclass(frozen=True)
class Run:
    run_id: str
    conversation_id: str
    owner_id: str
    trigger_message_id: str
    state: str
    config_snapshot: dict[str, Any]
    cancel_requested: bool = False
    result_text: str = ""
    usage: dict[str, int] | None = None

    def __post_init__(self):
        if self.state not in RUN_STATES:
            raise ValueError("invalid_run_state")

    def public(self) -> dict[str, Any]:
        config = {
            "model_profile_id": self.config_snapshot.get("model_profile_id"),
            "workspace_id": self.config_snapshot.get("workspace_id"),
            "execution_node_id": self.config_snapshot.get("execution_node_id"),
        }
        memory = self.config_snapshot.get("memory_context")
        if isinstance(memory, dict):
            config["memory_context"] = {
                "enabled": bool(memory.get("enabled")),
                "mode": memory.get("mode", "none"),
                "item_count": int(memory.get("item_count", 0) or 0),
                "bytes": int(memory.get("bytes", 0) or 0),
                "max_items": int(memory.get("max_items", 0) or 0),
                "max_bytes": int(memory.get("max_bytes", 0) or 0),
            }
        return {
            "run_id": self.run_id,
            "conversation_id": self.conversation_id,
            "state": self.state,
            "config": config,
            "cancel_requested": self.cancel_requested,
            "result_text": self.result_text[:32768],
            "usage": {
                "input_tokens": int((self.usage or {}).get("input_tokens", 0) or 0),
                "output_tokens": int((self.usage or {}).get("output_tokens", 0) or 0),
                "total_tokens": int((self.usage or {}).get("total_tokens", 0) or 0),
                "provider_requests": int((self.usage or {}).get("provider_requests", 0) or 0),
            },
        }


__all__ = ["RUN_STATES", "TERMINAL_RUN_STATES", "Run"]
