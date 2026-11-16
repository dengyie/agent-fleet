"""Route a main assistant's Hub controls separately from Node execution."""
from __future__ import annotations


class AssistantToolBroker:
    """Keep existing broker validation and receipts on both execution paths."""

    def __init__(self, hub, remote, *, hub_tools):
        self.hub = hub
        self.remote = remote
        self.hub_tools = frozenset(hub_tools)

    def execute(self, *, command_id, tool, arguments, owner_id, epoch):
        broker = self.hub if tool in self.hub_tools else self.remote
        return broker.execute(command_id=command_id, tool=tool, arguments=arguments,
                              owner_id=owner_id, epoch=epoch)

    def observe(self, *, command_id, tool):
        # Only remote execution can suspend a Run awaiting a Node receipt.
        return self.remote.observe(command_id=command_id, tool=tool)
