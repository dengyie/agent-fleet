"""Explicit assembly for a disposable platform execution Node.

The Hub owns command authorization and the Node owns only the capabilities it
was configured to run.  This module deliberately has no daemon loop: tests and
future process supervisors call ``poll_once`` through the existing NodeClient.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from collections.abc import Mapping
from pathlib import Path
import threading
import time
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from fleet_core.backends.sandbox import SandboxBackend
from fleet_core.web_agent_contract import WEB_AGENT_TOOLS
from tools.browser_bridge.contract import WebAgentExecutor
from tools.browser_bridge.muse import MuseBridge
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
    POSTCHECK_ACTION,
)
from .services.logs import FixedServiceLogReader
from .services.actions import ServiceActionExecutor
from .artifact_transfer import ArtifactReceiver, InputDirectory
from .artifact_export import ArtifactExporter, OutputFile
from .node_capabilities import (
    CapabilityObserver, NodeCapabilityConfig, NodeCapabilityReceipt, NodeCapabilityReportError,
    NodeCapabilityReporter, NodeCapabilityService, observe_local_capabilities,
)
from fleet_core.remote_workflow_test import REMOTE_TEST_ACTION
from .workflow_remote_test import NodeRemoteTestExecutor


NODE_RUNTIME_CAPABILITIES = frozenset(NODE_TOOLS | POSTCHECK_ACTIONS | {
    BROWSER_SESSION_CAPABILITY, "artifact.receive", "artifact.export",
    REMOTE_TEST_ACTION,
})


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
    muse_bridge_enabled: bool = False
    muse_workspace_ids: tuple[str, ...] = ()
    muse_bridge_state_path: Path | None = None
    browser_enabled: bool = False
    browser_network_enabled: bool = False
    browser_submit_enabled: bool = False
    browser_allowed_origins: tuple[str, ...] = ()
    browser_driver_factory: Any | None = field(default=None, repr=False, compare=False)
    plugins: dict | None = None
    workspace_id: str | None = None
    workspace_roots: Mapping[str, Path] = field(default_factory=dict)
    artifact_inputs: tuple[tuple[str, InputDirectory], ...] = ()
    artifact_journal_path: Path | None = None
    artifact_outputs: tuple[tuple[str, str, OutputFile], ...] = ()
    artifact_export_journal_path: Path | None = None
    artifact_max_bytes: int = 64 * 1024 * 1024
    capability_config: NodeCapabilityConfig | None = None
    workflow_test_enabled: bool = False

    def __post_init__(self):
        if self.plugins is not None and not isinstance(self.plugins, dict):
            raise ValueError('plugins must be an object')
        if not isinstance(self.sandbox_launcher, (tuple, list)) or any(
                not isinstance(arg, str) or not arg or len(arg) > 4096 or "\x00" in arg
                for arg in self.sandbox_launcher):
            raise ValueError("invalid sandbox launcher")
        object.__setattr__(self, "sandbox_launcher", tuple(self.sandbox_launcher))
        if self.public_key is not None and (
                not isinstance(self.public_key, bytes) or len(self.public_key) != 32):
            raise ValueError("public_key must be raw Ed25519 bytes")
        if self.capability_config is not None and (type(self.capability_config) is not NodeCapabilityConfig or not self.public_key):
            raise ValueError("capability reporting requires explicit configuration and command signing")
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
        if type(self.workflow_test_enabled) is not bool or (self.workflow_test_enabled and (
                REMOTE_TEST_ACTION not in capabilities or "artifact.receive" not in capabilities
                or self.capability_config is None or not self.sandbox_launcher)):
            raise ValueError("remote fixed tests require signed receive, capability registration and sandbox launcher")
        if any(capability not in NODE_RUNTIME_CAPABILITIES
               for capability in capabilities):
            raise ValueError("unknown capability")
        if "artifact.receive" in capabilities and (not self.public_key or not self.artifact_inputs):
            raise ValueError("artifact receive requires signed input aliases")
        if "artifact.export" in capabilities and (not self.public_key or not self.artifact_outputs):
            raise ValueError("artifact export requires signed output aliases")
        if (len({(workspace, key) for workspace, key, output in self.artifact_outputs}) != len(self.artifact_outputs)
                or any(not isinstance(output, OutputFile) for workspace, key, output in self.artifact_outputs)):
            raise ValueError("invalid output aliases")
        if len(dict(self.artifact_inputs)) != len(self.artifact_inputs):
            raise ValueError("duplicate input aliases")
        if self.artifact_journal_path is not None and Path(self.artifact_journal_path).resolve() == journal_path.resolve():
            raise ValueError("artifact journal must be independent")
        receive_journal = Path(self.artifact_journal_path or journal_path.with_suffix(".receives.db")).resolve()
        export_journal = Path(self.artifact_export_journal_path or journal_path.with_suffix(".exports.db")).resolve()
        if export_journal in {journal_path.resolve(), receive_journal}:
            raise ValueError("artifact export journal must be independent")
        object.__setattr__(self, "artifact_inputs", tuple(self.artifact_inputs))
        object.__setattr__(self, "artifact_outputs", tuple(self.artifact_outputs))
        if not isinstance(self.workspace_roots, Mapping):
            raise ValueError("workspace_roots must be an object")
        roots = {}
        for resource_id, root in self.workspace_roots.items():
            _bounded_identity(resource_id, "workspace_id")
            if not isinstance(root, (str, Path)) or not str(root) or "\x00" in str(root):
                raise ValueError("invalid workspace root")
            roots[resource_id] = Path(root).expanduser().resolve()
        if self.workspace_id is not None:
            _bounded_identity(self.workspace_id, "workspace_id")
            if self.workspace_id in roots and roots[self.workspace_id] != workspace_root.resolve():
                raise ValueError("duplicate workspace binding")
            roots[self.workspace_id] = workspace_root.resolve()
        workspace_capabilities = {"workspace.list", "workspace.read", "workspace.write", "workspace.exec", POSTCHECK_ACTION}
        if capabilities & workspace_capabilities and not roots:
            raise ValueError("explicit workspace binding required")
        if self.browser_network_enabled and not self.browser_enabled:
            raise ValueError("browser network requires browser capability")
        if self.browser_submit_enabled and not (self.browser_enabled and self.browser_network_enabled):
            raise ValueError("browser submit requires browser capability and network gate")
        if self.muse_bridge_enabled:
            if not self.muse_workspace_ids or self.muse_bridge_state_path is None:
                raise ValueError("Muse state path and workspace allowlist required")
            if not self.public_key or not (capabilities & WEB_AGENT_TOOLS):
                raise ValueError("Muse signed capabilities required")
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
        object.__setattr__(self, "workspace_roots", MappingProxyType(roots))
        object.__setattr__(self, "journal_path", journal_path)
        object.__setattr__(self, "hub_url", self.hub_url.rstrip("/"))
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "browser_allowed_origins", origins)
        object.__setattr__(self, "log_path_prefix", tuple(prefixes))
        object.__setattr__(self, "service_path_prefix", tuple(service_prefixes))


class NodeRuntime:
    """Build one explicit, local Node execution assembly."""

    def __init__(self, config: NodeRuntimeConfig, *, transport=None,
                 clock=None, capability_observer: CapabilityObserver | None = None):
        if not isinstance(config, NodeRuntimeConfig):
            raise TypeError("config must be NodeRuntimeConfig")
        self.config = config
        self.clock = clock or time.time
        self._stopped = threading.Event()
        self.workspace_backends = {
            resource_id: SandboxBackend(root, launcher=config.sandbox_launcher)
            for resource_id, root in config.workspace_roots.items()
        }
        # Keep the established single-root inspection attribute while all
        # command execution resolves through the explicit resource mapping.
        self.backend = self.workspace_backends.get(config.workspace_id)
        self.journal = NodeJournal(config.journal_path)
        self.journal.init()
        self.plugin_executor = None
        if config.plugins is not None:
            from tools.plugins.runtime import PluginExecutor
            self.plugin_executor = PluginExecutor(config.journal_path.parent / 'plugins', config.plugins)
        self.log_reader = FixedServiceLogReader(path_prefix=config.log_path_prefix)
        browser_backend = None
        if config.browser_enabled:
            from .browser_backend import LocalBrowserBackend
            # External browser egress is unavailable until a driver proves the
            # T3.7 three-layer boundary.  Keep submit disabled with it: a
            # submit capability without an enforced network boundary would
            # advertise a side-effecting tool that cannot safely execute.
            browser_network_enabled = False
            browser_backend = LocalBrowserBackend(
                config.browser_driver_factory,
                clock=clock or __import__("time").time,
                # No supported driver proves all-socket egress enforcement yet.
                network_enabled=browser_network_enabled,
                allowed_origins=config.browser_allowed_origins,
                submit_enabled=config.browser_submit_enabled and browser_network_enabled,
            )
        service_executor = (
            ServiceActionExecutor(path_prefix=config.service_path_prefix)
            if SERVICE_POSTCHECK_ACTION in config.capabilities else None
        )
        browser_available = bool(
            browser_backend is not None and browser_backend.available
        )
        browser_submit_available = bool(
            browser_backend is not None
            and browser_backend.network_enabled
            and browser_backend.submit_enabled
        )
        allowed_tools = frozenset(
            capability for capability in NODE_TOOLS
            if (
                capability in config.capabilities and capability not in BROWSER_TOOLS
            ) or (
                browser_available
                and BROWSER_SESSION_CAPABILITY in config.capabilities
                and capability in BROWSER_TOOLS
                and (capability != "browser.submit" or browser_submit_available)
            )
        )
        allowed_postchecks = frozenset(
            capability for capability in POSTCHECK_ACTIONS
            if capability in config.capabilities
        )
        web_agent_executor = None
        if config.muse_bridge_enabled:
            web_agent_executor = WebAgentExecutor(
                MuseBridge(config.muse_bridge_state_path), workspace_ids=config.muse_workspace_ids)
        if web_agent_executor is None:
            allowed_tools = allowed_tools - WEB_AGENT_TOOLS
        self.web_agent_executor = web_agent_executor
        self.browser_backend = browser_backend
        self.artifact_receiver = None
        if "artifact.receive" in config.capabilities:
            self.artifact_receiver = ArtifactReceiver(node_id=config.node_id, credential=config.credential,
                worker_id=config.worker_id, hub_url=config.hub_url, public_key=config.public_key,
                inputs=dict(config.artifact_inputs), journal_path=config.artifact_journal_path or config.journal_path.with_suffix(".receives.db"),
                slot_path=config.journal_path, transport=transport, max_bytes=config.artifact_max_bytes,
                should_cancel=self._stopped.is_set, **({"clock": clock} if clock is not None else {}))
        self.artifact_exporter = None
        if "artifact.export" in config.capabilities:
            self.artifact_exporter = ArtifactExporter(node_id=config.node_id, credential=config.credential,
                worker_id=config.worker_id, hub_url=config.hub_url, public_key=config.public_key,
                outputs={(workspace, key): output for workspace, key, output in config.artifact_outputs},
                journal_path=config.artifact_export_journal_path or config.journal_path.with_suffix(".exports.db"),
                slot_path=config.journal_path, transport=transport, max_bytes=config.artifact_max_bytes,
                should_cancel=self._stopped.is_set, **({"clock": clock} if clock is not None else {}))
        self.workflow_test_executor = (NodeRemoteTestExecutor(self.artifact_receiver, launcher=config.sandbox_launcher,
            clock=self.clock, should_cancel=self._stopped.is_set) if config.workflow_test_enabled else None)
        self.executor = NodeToolExecutor(
            self.backend,
            workspace_backends=self.workspace_backends,
            allowed_tools=allowed_tools,
            allowed_postcheck_actions=allowed_postchecks,
            service_executor=service_executor,
            log_reader=self.log_reader if SERVICE_LOGS_POSTCHECK_ACTION in config.capabilities else None,
            browser_backend=browser_backend,
            browser_enabled=config.browser_enabled,
            web_agent_executor=web_agent_executor,
            plugin_executor=self.plugin_executor,
            artifact_receiver=self.artifact_receiver,
            artifact_exporter=self.artifact_exporter,
            workflow_test_executor=self.workflow_test_executor,
        )
        self.capability_service = None
        self.capability_reporter = None
        if config.capability_config is not None:
            if transport is None:
                from tools.transport import Transport
                transport = Transport()
            self.capability_service = NodeCapabilityService(config.capability_config, node_id=config.node_id,
                observer=capability_observer or self._observe_capabilities,
                available_actions=self._capability_actions)
            self.capability_reporter = NodeCapabilityReporter(self.capability_service, hub_url=config.hub_url,
                credential=config.credential, transport=transport, clock=self.clock, should_cancel=self._stopped.is_set)
            self.executor.capability_prepare = lambda command: self.capability_service.prepare(command, now=self.clock())
        kwargs = {
            "executor": self.executor,
            "node_id": config.node_id,
            "credential": config.credential,
            "hub_url": config.hub_url,
            "transport": transport,
            "worker_id": config.worker_id,
            "public_key": config.public_key,
            "require_signature": True,
            "should_cancel": self._stopped.is_set,
        }
        if clock is not None:
            kwargs["clock"] = clock
        self.client = NodeClient(self.journal, **kwargs)

    def _observe_capabilities(self):
        roots = tuple(self.config.workspace_roots.values()) + tuple(
            directory.root for _, directory in self.config.artifact_inputs) + tuple(
            output.root for _, _, output in self.config.artifact_outputs)
        observations = tuple(observe_local_capabilities(root) for root in roots or (self.config.journal_path.parent,))
        return replace(observations[0], disk_bytes=min(item.disk_bytes for item in observations))

    def _capability_actions(self) -> frozenset[str]:
        browser_available = self.browser_backend is not None and self.browser_backend.available
        exec_available = bool(self.workspace_backends) and all(backend.launcher for backend in self.workspace_backends.values())
        actions = {"tool." + tool for tool in self.executor.allowed_tools
            if (tool != "workspace.exec" or exec_available)
            and (tool not in BROWSER_TOOLS or browser_available)
            and (tool not in WEB_AGENT_TOOLS or self.web_agent_executor is not None)}
        actions.update(self.executor.allowed_postcheck_actions)
        if self.workflow_test_executor is not None and self.workflow_test_executor.available:
            actions.add(REMOTE_TEST_ACTION)
        return frozenset(actions)

    def report_capabilities(self) -> NodeCapabilityReceipt:
        if self.capability_reporter is None:
            raise NodeCapabilityReportError("capability_reporting_disabled")
        return self.capability_reporter.report()

    def manifest(self) -> dict[str, Any]:
        return {
            "node_id": self.config.node_id,
            "worker_id": self.config.worker_id,
            **({'plugins': self.plugin_executor.manifest()} if self.plugin_executor else {}),
            "capabilities": {
                **{name: True for name in sorted(self.config.capabilities)
                   if (name != BROWSER_SESSION_CAPABILITY
                       or (self.browser_backend is not None and self.browser_backend.available))
                   and (name not in WEB_AGENT_TOOLS or self.web_agent_executor is not None)
                   and (name != "artifact.receive" or (self.artifact_receiver is not None and self.artifact_receiver.available))
                   and (name != "artifact.export" or (self.artifact_exporter is not None and self.artifact_exporter.available))
                   and (name != REMOTE_TEST_ACTION or (self.workflow_test_executor is not None and self.workflow_test_executor.available))},
                **({"browser.submit": True} if "browser.submit" in self.executor.allowed_tools else {}),
            },
            **({"browser": {
                    "enabled": bool(self.browser_backend and self.browser_backend.available),
                    "backend": "cdp_local" if self.browser_backend and self.browser_backend.available else "unavailable",
                }} if self.config.browser_enabled else {}),
        }

    def poll_once(self, *, limit: int = 20, lease_s: float = 60.0):
        """Execute at most ``limit`` freshly claimed commands sequentially."""
        if self._stopped.is_set():
            return {'ok': True, 'commands': 0, 'receipts': 0}
        report_error = None
        if self.capability_reporter is not None:
            try:
                self.report_capabilities()
            except NodeCapabilityReportError as exc:
                report_error = exc.code
        result = self.client.poll_once(limit=limit, lease_s=lease_s)
        return {**result, "capability_report_error": report_error} if report_error else result

    def request_stop(self) -> None:
        """Notify owned in-flight work without closing resources in a signal handler."""
        self._stopped.set()

    def close_all(self) -> None:
        """Close local browser sessions during orderly Node shutdown."""
        self.request_stop()
        if self.browser_backend is not None:
            self.browser_backend.close_all()
        if self.plugin_executor is not None:
            self.plugin_executor.close()


__all__ = ["NODE_RUNTIME_CAPABILITIES", "NodeRuntime", "NodeRuntimeConfig"]
