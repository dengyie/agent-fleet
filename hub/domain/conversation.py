"""Conversation and user-turn domain values."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from platform_schema import PlatformValidationError, validate_client_token, validate_id, validate_message_text, validate_owner_id


@dataclass(frozen=True)
class Conversation:
    conversation_id: str
    owner_id: str
    title: str
    workspace_id: str | None
    overrides: dict[str, Any]
    revision: int = 0


@dataclass(frozen=True)
class UserTurn:
    text: str
    client_token: str

    @classmethod
    def from_input(cls, text: Any, client_token: Any) -> "UserTurn":
        return cls(validate_message_text(text), validate_client_token(client_token))


def validate_conversation_id(value: Any) -> str:
    return validate_id(value, "conversation_id")


def validate_owner(value: Any) -> str:
    return validate_owner_id(value)


__all__ = ["Conversation", "UserTurn", "validate_conversation_id", "validate_owner"]
