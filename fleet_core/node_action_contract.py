"""Node action names shared by Hub admission and execution Nodes."""
from __future__ import annotations

from .browser_policy import BROWSER_TOOLS
from .web_agent_contract import WEB_AGENT_TOOLS

BROWSER_SESSION_CAPABILITY = "browser.session"
NODE_TOOLS = frozenset({
    "workspace.list", "workspace.read", "workspace.write",
    "workspace.exec",
}) | BROWSER_TOOLS | WEB_AGENT_TOOLS
POSTCHECK_ACTION = "reconcile.workspace.digest"
SERVICE_POSTCHECK_ACTION = "reconcile.service.inspect"
SERVICE_LOGS_POSTCHECK_ACTION = "reconcile.service.logs"
POSTCHECK_ACTIONS = frozenset({
    POSTCHECK_ACTION,
    SERVICE_POSTCHECK_ACTION,
    SERVICE_LOGS_POSTCHECK_ACTION,
})
