"""Immutable MCP authority stored in a private Run configuration."""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_MCP_TOOL_PINS = 20
_NAME = re.compile(r"mcp\.[A-Za-z0-9_.:-]{1,128}\.[A-Za-z0-9_.:-]{1,128}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class MCPToolPinError(ValueError):
    def __init__(self) -> None:
        self.code = "invalid_mcp_tool_pins"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class MCPToolPin:
    public_name: str
    revision: int
    definition_digest: str

    def __post_init__(self) -> None:
        if (type(self.public_name) is not str or len(self.public_name) > 261
                or not _NAME.fullmatch(self.public_name)
                or type(self.revision) is not int or not 1 <= self.revision <= 9007199254740991
                or type(self.definition_digest) is not str or len(self.definition_digest) != 64
                or not _DIGEST.fullmatch(self.definition_digest)):
            raise MCPToolPinError()

    def snapshot(self) -> dict[str, str | int]:
        return {"revision": self.revision, "definition_digest": self.definition_digest}


def parse_mcp_tool_pins(value: object) -> tuple[MCPToolPin, ...]:
    if type(value) is not dict or len(value) > MAX_MCP_TOOL_PINS:
        raise MCPToolPinError()
    pins: list[MCPToolPin] = []
    for name, item in value.items():
        if type(item) is not dict or item.keys() != {"revision", "definition_digest"}:
            raise MCPToolPinError()
        pins.append(MCPToolPin(name, item["revision"], item["definition_digest"]))
    return tuple(pins)


__all__ = ["MAX_MCP_TOOL_PINS", "MCPToolPin", "MCPToolPinError", "parse_mcp_tool_pins"]
