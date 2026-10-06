"""Explicit assembly for a disposable platform execution Node.

The Hub owns command authorization and the Node owns only the capabilities it
was configured to run.  This module deliberately has no daemon loop: tests and
future process supervisors call ``poll_once`` through the existing NodeClient.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .backends.sandbox import SandboxBackend
from .journal import NodeJournal
from .node_client import NodeClient
from .node_executor import (
    BROWSER_SESSION_CAPABILITY,
    BROWSER_TOOLS,
    NODE_TOOLS,
    POSTCHECK_ACTIONS,
    SERVICE_POSTCHECK_ACTION,
    SERVICE_LOGS_POSTCHECK_ACTION,
    NodeToolExecutor,
)
from .services.logs import FixedServiceLogReader
from .services.actions import ServiceActionExecutor


NODE_RUNTIME_CAPABILITIES = frozenset(NODE_TOOLS | POSTCHECK_ACTIONS | {BROWSER_SESSION_CAPABILITY})


def _bounded_identity(value: Any, field: str, limit: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"invalid {field}")
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in value):
        raise ValueError(f"invalid {field}")
    return value


@dataclass(frozen=True)
class NodeRuntimeConfig:
    node_id: str
    credential: str
    workspace_root: Path
    journal_path: Path
    hub_url: str
    public_key: bytes | None = None
    worker_id: str | None = None
    capabilities: frozenset[str] = field(default_factory=frozenset)
    log_path_prefix: tuple[Path, ...] = ()
    service_path_prefix: tuple[Path, ...] = ()
    sandbox_launcher: tuple[str, ...] = ()
    browser_enabled: bool = False
    browser_network_enabled: bool = False
    browser_submit_enabled: bool = False
    browser_allowed_origins: tuple[str, ...] = ()
    browser_driver_factory: Any | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if not isinstance(self.sandbox_launcher, (tuple, list)) or any(
                not isinstance(arg, str) or not arg or len(arg) > 4096 or "\x00" in arg
                for arg in self.sandbox_launcher):
            raise ValueError("invalid sandbox launcher")
        object.__setattr__(self, "sandbox_launcher", tuple(self.sandbox_launcher))
        if self.public_key is not None and (
                not isinstance(self.public_key, bytes) or len(self.public_key) != 32):
            raise ValueError("public_key must be raw Ed25519 bytes")
        node_id = _bounded_identity(self.node_id, "node_id")
        credential = _bounded_identity(self.credential, "credential", 4096)
        credential_node, separator, credential_secret = credential.partition(":")
        if (not separator or credential_node != node_id or not credential_secret
                or ":" in credential_secret):
            raise ValueError("credential must be node-scoped")
        worker_id = _bounded_identity(self.worker_id or node_id, "worker_id")
        workspace_root = Path(self.workspace_root).expanduser()
        journal_path = Path(self.journal_path).expanduser()
        if not str(workspace_root) or not str(journal_path):
            raise ValueError("workspace and journal paths are required")
        parsed = urlsplit(self.hub_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("hub_url must use http or https")
        capabilities = frozenset(self.capabilities or ())
        if any(capability not in NODE_RUNTIME_CAPABILITIES
               for capability in capabilities):
            raise ValueError("unknown capability")
        if self.browser_network_enabled and not self.browser_enabled:
            raise ValueError("browser network requires browser capability")
        if self.browser_submit_enabled and not self.browser_enabled:
            raise ValueError("browser submit requires browser capability")
        origins = tuple(self.browser_allowed_origins or ())
        if any(not isinstance(origin, str) or len(origin) > 512 for origin in origins):
            raise ValueError("invalid browser origins")
        if self.browser_enabled and BROWSER_SESSION_CAPABILITY not in capabilities:
            raise ValueError("browser capability required")
        prefixes: list[Path] = []
        for prefix in self.log_path_prefix or ():
            path = Path(prefix).expanduser()
            if not str(path) or "\x00" in str(path):
                raise ValueError("invalid log path prefix")
            prefixes.append(path)
        service_prefixes: list[Path] = []
        for prefix in self.service_path_prefix or ():
            path = Path(prefix).expanduser()
            if not str(path) or "\x00" in str(path):
                raise ValueError("invalid service path prefix")
            service_prefixes.append(path)
        object.__setattr__(self, "node_id", node_id)
        object.__setattr__(self, "credential", credential)
        object.__setattr__(self, "worker_id", worker_id)
        object.__setattr__(self, "workspace_root", workspace_root)
        object.__setattr__(self, "journal_path", journal_path)
        object.__setattr__(self, "hub_url", self.hub_url.rstrip("/"))
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "browser_allowed_origins", origins)
        object.__setattr__(self, "log_path_prefix", tuple(prefixes))
        object.__setattr__(self, "service_path_prefix", tuple(service_prefixes))


class NodeRuntime:
    """Build one explicit, local Node execution assembly."""

    def __init__(self, config: NodeRuntimeConfig, *, transport=None,
                 clock=None):
        if not isinstance(config, NodeRuntimeConfig):
            raise TypeError("config must be NodeRuntimeConfig")
        self.config = config
        self.backend = SandboxBackend(
            config.workspace_root, launcher=config.sandbox_launcher)
        self.journal = NodeJournal(config.journal_path)
        self.journal.init()
        self.log_reader = FixedServiceLogReader(path_prefix=config.log_path_prefix)
        browser_backend = None
        if config.browser_enabled:
            from .browser_backend import LocalBrowserBackend
            browser_backend = LocalBrowserBackend(
                config.browser_driver_factory,
                clock=clock or __import__("time").time,
                # No supported driver proves all-socket egress enforcement yet.
                network_enabled=False,
                allowed_origins=config.browser_allowed_origins,
                submit_enabled=config.browser_submit_enabled,
            )
        service_executor = (
            ServiceActionExecutor(path_prefix=config.service_path_prefix)
            if SERVICE_POSTCHECK_ACTION in config.capabilities else None
        )
        browser_available = bool(
            browser_backend is not None and browser_backend.available
        )
        allowed_tools = frozenset(
            capability for capability in NODE_TOOLS
            if (
                capability in config.capabilities and capability not in BROWSER_TOOLS
            ) or (
                browser_available
                and BROWSER_SESSION_CAPABILITY in config.capabilities
                and capability in BROWSER_TOOLS
                and (capability != "browser.submit" or config.browser_submit_enabled)
            )
        )
        allowed_postchecks = frozenset(
            capability for capability in POSTCHECK_ACTIONS
            if capability in config.capabilities
        )
        self.browser_backend = browser_backend
        self.executor = NodeToolExecutor(
            self.backend,
            allowed_tools=allowed_tools,
            allowed_postcheck_actions=allowed_postchecks,
            service_executor=service_executor,
            log_reader=self.log_reader if SERVICE_LOGS_POSTCHECK_ACTION in config.capabilities else None,
            browser_backend=browser_backend,
            browser_enabled=config.browser_enabled,
        )
        kwargs = {
            "executor": self.executor,
            "node_id": config.node_id,
            "credential": config.credential,
            "hub_url": config.hub_url,
            "transport": transport,
            "worker_id": config.worker_id,
            "public_key": config.public_key,
            "require_signature": True,
        }
        if clock is not None:
            kwargs["clock"] = clock
        self.client = NodeClient(self.journal, **kwargs)

    def manifest(self) -> dict[str, Any]:
        return {
            "node_id": self.config.node_id,
            "worker_id": self.config.worker_id,
            "capabilities": {
                name: True for name in sorted(self.config.capabilities)
                if name != BROWSER_SESSION_CAPABILITY
                or (self.browser_backend is not None and self.browser_backend.available)
            },
            **({"browser": {
                    "enabled": bool(self.browser_backend and self.browser_backend.available),
                    "backend": "cdp_local" if self.browser_backend and self.browser_backend.available else "unavailable",
                }} if self.config.browser_enabled else {}),
        }

    def poll_once(self, *, limit: int = 20, lease_s: float = 60.0):
        return self.client.poll_once(limit=limit, lease_s=lease_s)

    def close_all(self) -> None:
        """Close local browser sessions during orderly Node shutdown."""
        if self.browser_backend is not None:
            self.browser_backend.close_all()


__all__ = ["NODE_RUNTIME_CAPABILITIES", "NodeRuntime", "NodeRuntimeConfig"]
